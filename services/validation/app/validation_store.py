"""Persistence helpers for validation execution records."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Literal
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Mapping

import psycopg

from shared.determinism.input_hash import compute_canonical_hash
from services.validation.app.config import ValidationConfig


class ValidationStoreError(RuntimeError):
    """Represent deterministic validation-store failures."""

    def __init__(self, *, error_code: str, message: str, reason: str, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class ValidationExecutionRecord:
    """Represent one persisted validation execution record."""

    validation_id: UUID
    return_id: str
    tax_domain: str
    mode: str
    validation_status: str
    request_fingerprint: str
    issues: tuple[dict[str, object], ...]
    rule_results: tuple[dict[str, object], ...]
    correlation_id: str
    trace_id: str


class ValidationStore(Protocol):
    """Represent the required validation-store interface."""

    def record_execution(self, record: ValidationExecutionRecord) -> None:
        """Persist one validation execution deterministically."""
        ...

    def get_record(self, validation_id: UUID) -> ValidationExecutionRecord | None:
        """Load one validation execution record."""
        ...

    def readiness(self) -> tuple[bool, Literal["in_memory", "persistent", "unavailable"]]:
        """Return store readiness status and mode."""
        ...


class InMemoryValidationStore:
    """Use deterministic in-memory storage for development and tests."""

    def __init__(self) -> None:
        self._records: dict[UUID, ValidationExecutionRecord] = {}

    def record_execution(self, record: ValidationExecutionRecord) -> None:
        self._records[record.validation_id] = record

    def get_record(self, validation_id: UUID) -> ValidationExecutionRecord | None:
        return self._records.get(validation_id)

    def readiness(self) -> tuple[bool, Literal["in_memory"]]:
        return True, "in_memory"


class UnavailableValidationStore:
    """Fail closed when production persistence is unavailable."""

    def __init__(self, message: str) -> None:
        self._message = message

    def record_execution(self, record: ValidationExecutionRecord) -> None:
        _ = record
        raise ValidationStoreError(
            error_code="validation_persistence_unavailable",
            message=self._message,
            reason="validation_persistence_unavailable",
            status_code=503,
        )

    def get_record(self, validation_id: UUID) -> ValidationExecutionRecord | None:
        _ = validation_id
        return None

    def readiness(self) -> tuple[bool, Literal["unavailable"]]:
        return False, "unavailable"


class PersistentValidationStore:
    """Persist validation execution records to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def record_execution(self, record: ValidationExecutionRecord) -> None:
        try:
            with psycopg.connect(
                self._database_url, connect_timeout=5, autocommit=True
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO validation_executions (
                            validation_id,
                            return_id,
                            tax_domain,
                            validation_mode,
                            validation_status,
                            request_fingerprint,
                            issues_json,
                            rule_results_json,
                            correlation_id,
                            trace_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                        ON CONFLICT (validation_id) DO UPDATE SET
                            return_id = EXCLUDED.return_id,
                            tax_domain = EXCLUDED.tax_domain,
                            validation_mode = EXCLUDED.validation_mode,
                            validation_status = EXCLUDED.validation_status,
                            request_fingerprint = EXCLUDED.request_fingerprint,
                            issues_json = EXCLUDED.issues_json,
                            rule_results_json = EXCLUDED.rule_results_json,
                            correlation_id = EXCLUDED.correlation_id,
                            trace_id = EXCLUDED.trace_id
                        """,
                        (
                            record.validation_id,
                            record.return_id,
                            record.tax_domain,
                            record.mode,
                            record.validation_status,
                            record.request_fingerprint,
                            _to_json(record.issues),
                            _to_json(record.rule_results),
                            record.correlation_id,
                            record.trace_id,
                        ),
                    )
        except psycopg.Error as error:
            raise ValidationStoreError(
                error_code="validation_persistence_unavailable",
                message="Validation persistence is unavailable.",
                reason="validation_persistence_unavailable",
                status_code=503,
            ) from error

    def get_record(self, validation_id: UUID) -> ValidationExecutionRecord | None:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            validation_id,
                            return_id,
                            tax_domain,
                            validation_mode,
                            validation_status,
                            request_fingerprint,
                            issues_json,
                            rule_results_json,
                            correlation_id,
                            trace_id
                        FROM validation_executions
                        WHERE validation_id = %s
                        """,
                        (validation_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise ValidationStoreError(
                error_code="validation_persistence_unavailable",
                message="Validation persistence is unavailable.",
                reason="validation_persistence_unavailable",
                status_code=503,
            ) from error
        if row is None:
            return None
        return ValidationExecutionRecord(
            validation_id=row[0],
            return_id=row[1],
            tax_domain=row[2],
            mode=row[3],
            validation_status=row[4],
            request_fingerprint=row[5],
            issues=tuple(row[6]),
            rule_results=tuple(row[7]),
            correlation_id=row[8],
            trace_id=row[9],
        )

    def readiness(self) -> tuple[bool, Literal["persistent"]]:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT to_regclass('public.validation_executions')")
                    row = cursor.fetchone()
        except psycopg.Error:
            return False, "persistent"
        return bool(row and row[0] is not None), "persistent"


def build_validation_execution_record(
    *,
    return_id: str,
    tax_domain: str,
    mode: str,
    fields: Mapping[str, object],
    validation_status: str,
    issues: tuple[dict[str, object], ...],
    rule_results: tuple[dict[str, object], ...],
    correlation_id: str,
    trace_id: str,
) -> ValidationExecutionRecord:
    """Build one deterministic validation execution record."""

    canonical_request = {
        "return_id": return_id,
        "tax_domain": tax_domain,
        "mode": mode,
        "fields": fields,
    }
    fingerprint = compute_canonical_hash(canonical_request).sha256_hex
    validation_id = uuid5(NAMESPACE_URL, f"validation-execution:{fingerprint}")
    return ValidationExecutionRecord(
        validation_id=validation_id,
        return_id=return_id,
        tax_domain=tax_domain,
        mode=mode,
        validation_status=validation_status,
        request_fingerprint=fingerprint,
        issues=issues,
        rule_results=rule_results,
        correlation_id=correlation_id,
        trace_id=trace_id,
    )


def build_default_validation_store(config: ValidationConfig) -> ValidationStore:
    """Build the default validation store for the configured runtime."""

    if config.runtime_mode != "production":
        return InMemoryValidationStore()
    if config.database_url is None or not config.database_url.strip():
        return UnavailableValidationStore("Validation persistence is unavailable.")
    store = PersistentValidationStore(database_url=config.database_url)
    ready, _ = store.readiness()
    if ready:
        return store
    return UnavailableValidationStore("Validation persistence is unavailable.")


def _to_json(value: tuple[dict[str, object], ...]) -> str:
    from shared.determinism.input_hash import canonical_json_dumps

    return canonical_json_dumps(list(value))
