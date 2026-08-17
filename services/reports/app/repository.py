"""Deterministic reports repository aligned to existing `reports` DB table."""

from __future__ import annotations

import os
import json
from uuid import UUID
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from dataclasses import asdict
from dataclasses import dataclass

import psycopg

from services.reports.app.authz import ReportAccessContext
from services.reports.app.errors import INVALID_LINEAGE_REFERENCE
from services.reports.app.errors import REPORT_STORAGE_UNAVAILABLE
from services.reports.app.models import ReportGenerationResponseModel
from shared.determinism.input_hash import canonical_json_dumps

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"
DEFAULT_REPORT_TTL_SECONDS = 900
REPORT_RETENTION_DAYS: dict[str, int] = {
    "tax_summary": 2555,
    "worksheet": 2555,
    "comparative_view": 2555,
    "audit_package": 3650,
    "export_bundle": 365,
}
DEFAULT_REPORT_RETENTION_CLASS = "export_bundle"
DEFAULT_REPORT_CLEANUP_STATUS = "active"


@dataclass(frozen=True)
class FinalizedLineageReference:
    """Represent one supported finalized lineage reference record."""

    computation_id: str
    form_id: str
    historical_version_id: str
    supported_lane_id: str
    tax_year: int
    tax_type: str
    policy_anchor_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class StoredReportRecord:
    """Represent one persisted report record with access boundaries."""

    report_payload: dict[str, object]
    owner_user_id: str
    tenant_id: str
    created_at: str

    def to_snapshot_payload(self) -> dict[str, object]:
        """Return deterministic payload snapshot used by tests."""

        return {
            **self.report_payload,
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
        }


class ReportRepositoryError(RuntimeError):
    """Represent canonical repository-level failures for reports persistence."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.context = context or {}


class ReportsRepository:
    """Provide deterministic DB-backed create/get behavior for reports records."""

    def __init__(self, *, database_url: str | None = None) -> None:
        self._lock = Lock()
        self._database_url = _load_database_url() if database_url is None else database_url
        self._lineage_references = _default_lineage_references()
        self._persisted_reports: dict[str, StoredReportRecord] = {}
        self._persist_order: list[str] = []

    def resolve_finalized_lineage(
        self,
        *,
        computation_id: str,
        form_id: str,
        historical_version_id: str,
        supported_lane_id: str,
        tax_year: int,
    ) -> FinalizedLineageReference | None:
        """Resolve deterministic finalized lineage reference from governed in-memory index."""

        key = (
            computation_id,
            form_id,
            historical_version_id,
            supported_lane_id,
            tax_year,
        )
        return self._lineage_references.get(key)

    def register_finalized_lineage_reference(
        self,
        *,
        reference: FinalizedLineageReference,
    ) -> None:
        """Register finalized lineage reference for deterministic test/runtime setup."""

        key = (
            reference.computation_id,
            reference.form_id,
            reference.historical_version_id,
            reference.supported_lane_id,
            reference.tax_year,
        )
        with self._lock:
            self._lineage_references[key] = reference

    def create_report_record(
        self,
        *,
        report: ReportGenerationResponseModel,
        access_context: ReportAccessContext,
    ) -> StoredReportRecord:
        """Create one report record with deterministic duplicate-handling semantics."""

        if not self._database_url:
            return self._create_report_record_in_memory(
                report=report,
                access_context=access_context,
            )

        now_utc = datetime.now(UTC)
        report_payload = _serialize_report_response(report=report)
        report_retention_metadata = _build_report_retention_metadata(
            report_type=str(report_payload["report_type"]),
            created_at=now_utc,
        )
        report_payload["retention_metadata"] = report_retention_metadata
        metadata_payload = _build_report_type_metadata(
            report_payload=report_payload,
            tenant_id=access_context.tenant_id,
        )
        serialized_report_type = canonical_json_dumps(metadata_payload)
        lineage_payload = cast(dict[str, object], report_payload["lineage_reference"])
        report_id = _parse_uuid(cast_value=report_payload["report_id"], field_name="report_id")
        owner_user_id = _parse_uuid(
            cast_value=access_context.owner_user_id,
            field_name="owner_user_id",
        )
        computation_id = _parse_uuid(
            cast_value=lineage_payload["computation_id"],
            field_name="computation_id",
        )
        form_id = _parse_uuid(
            cast_value=lineage_payload["form_id"],
            field_name="form_id",
        )
        expires_at = now_utc + timedelta(seconds=DEFAULT_REPORT_TTL_SECONDS)

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._assert_fk_lineage_exists(
                        cursor=cursor,
                        user_id=owner_user_id,
                        computation_id=computation_id,
                        form_id=form_id,
                    )
                    cursor.execute(
                        """
                        INSERT INTO reports (
                            id,
                            user_id,
                            computation_id,
                            form_id,
                            report_type,
                            generated_at,
                            download_expires_at,
                            created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            report_id,
                            owner_user_id,
                            computation_id,
                            form_id,
                            serialized_report_type,
                            now_utc,
                            expires_at,
                            now_utc,
                        ),
                    )
                connection.commit()
        except ReportRepositoryError:
            raise
        except psycopg.Error as error:
            raise ReportRepositoryError(
                reason_code=REPORT_STORAGE_UNAVAILABLE,
                message="Report persistence storage is unavailable.",
                context={"operation": "create_report_record", "error": str(error)},
            ) from error

        created_record = self.get_report_by_id(report_id=str(report_id))
        if created_record is None:
            raise ReportRepositoryError(
                reason_code=REPORT_STORAGE_UNAVAILABLE,
                message="Report persistence verification failed after create.",
                context={"report_id": str(report_id)},
            )
        return created_record

    def get_report_by_id(
        self,
        *,
        report_id: str,
    ) -> StoredReportRecord | None:
        """Return one stored report record by report_id deterministically."""

        if not self._database_url:
            with self._lock:
                return self._persisted_reports.get(report_id)

        try:
            report_uuid = _parse_uuid(cast_value=report_id, field_name="report_id")
        except ReportRepositoryError as error:
            if error.reason_code == INVALID_LINEAGE_REFERENCE:
                return None
            raise

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            r.id::text,
                            r.user_id::text,
                            r.computation_id::text,
                            r.form_id::text,
                            r.report_type,
                            r.created_at,
                            c.tax_type,
                            c.tax_year
                        FROM reports AS r
                        JOIN computations AS c ON c.id = r.computation_id
                        WHERE r.id = %s
                        """,
                        (report_uuid,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise ReportRepositoryError(
                reason_code=REPORT_STORAGE_UNAVAILABLE,
                message="Report persistence storage is unavailable.",
                context={"operation": "get_report_by_id", "error": str(error)},
            ) from error

        if row is None:
            return None
        report_id_value = str(row[0])
        metadata_payload = _parse_report_type_metadata(raw_value=str(row[4]))
        report_payload = _build_payload_from_row(
            report_id=report_id_value,
            computation_id=str(row[2]),
            form_id=str(row[3]),
            tax_type=str(row[6]),
            tax_year=int(row[7]),
            metadata_payload=metadata_payload,
        )
        stored_record = StoredReportRecord(
            report_payload=report_payload,
            owner_user_id=str(row[1]),
            tenant_id=str(metadata_payload["tenant_id"]),
            created_at=str(row[5].isoformat()),
        )
        with self._lock:
            if report_id_value not in self._persisted_reports:
                self._persist_order.append(report_id_value)
            self._persisted_reports[report_id_value] = stored_record
        return stored_record

    def persist_generated_report(
        self,
        *,
        report: ReportGenerationResponseModel,
        access_context: ReportAccessContext,
    ) -> None:
        """Backward-compatible wrapper for generation flow integration."""

        self.create_report_record(report=report, access_context=access_context)

    def get_persisted_report_by_id(
        self,
        *,
        report_id: str,
    ) -> StoredReportRecord | None:
        """Backward-compatible wrapper for retrieval flow integration."""

        return self.get_report_by_id(report_id=report_id)

    def snapshot_persisted_reports(self) -> tuple[dict[str, object], ...]:
        """Return deterministic immutable snapshot of persisted reports."""

        with self._lock:
            return tuple(
                self._persisted_reports[report_id].to_snapshot_payload()
                for report_id in self._persist_order
                if report_id in self._persisted_reports
            )

    def reset(self) -> None:
        """Reset in-memory deterministic caches used by tests."""

        with self._lock:
            self._persisted_reports.clear()
            self._persist_order.clear()

    def _create_report_record_in_memory(
        self,
        *,
        report: ReportGenerationResponseModel,
        access_context: ReportAccessContext,
    ) -> StoredReportRecord:
        report_payload = _serialize_report_response(report=report)
        created_at = _parse_datetime("2026-01-01T00:00:00+00:00")
        report_payload["retention_metadata"] = _build_report_retention_metadata(
            report_type=str(report_payload["report_type"]),
            created_at=created_at,
        )
        report_id = str(report_payload["report_id"])
        stored_record = StoredReportRecord(
            report_payload=report_payload,
            owner_user_id=access_context.owner_user_id,
            tenant_id=access_context.tenant_id,
            created_at=created_at.isoformat(),
        )
        with self._lock:
            if report_id not in self._persisted_reports:
                self._persist_order.append(report_id)
            self._persisted_reports[report_id] = stored_record
        return stored_record

    def _assert_fk_lineage_exists(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        computation_id: UUID,
        form_id: UUID,
    ) -> None:
        cursor.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
        if cursor.fetchone() is None:
            raise ReportRepositoryError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Report persistence requires existing owner user lineage.",
                context={"user_id": str(user_id)},
            )

        cursor.execute(
            "SELECT user_id::text FROM computations WHERE id = %s",
            (computation_id,),
        )
        computation_row = cursor.fetchone()
        if computation_row is None:
            raise ReportRepositoryError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Report persistence requires existing computation lineage.",
                context={"computation_id": str(computation_id)},
            )
        if str(computation_row[0]) != str(user_id):
            raise ReportRepositoryError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Report persistence requires owner to match computation lineage owner.",
                context={"computation_id": str(computation_id), "user_id": str(user_id)},
            )

        cursor.execute(
            """
            SELECT computation_id::text
            FROM forms
            WHERE id = %s
            """,
            (form_id,),
        )
        form_row = cursor.fetchone()
        if form_row is None:
            raise ReportRepositoryError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Report persistence requires existing form lineage.",
                context={"form_id": str(form_id)},
            )
        if str(form_row[0]) != str(computation_id):
            raise ReportRepositoryError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Report persistence requires form lineage to match computation lineage.",
                context={"form_id": str(form_id), "computation_id": str(computation_id)},
            )


_default_reports_repository: ReportsRepository | None = None


def get_default_reports_repository() -> ReportsRepository:
    """Return singleton deterministic default reports repository."""

    global _default_reports_repository
    if _default_reports_repository is None:
        _default_reports_repository = ReportsRepository()
    return _default_reports_repository


def _default_lineage_references() -> (
    dict[
        tuple[str, str, str, str, int],
        FinalizedLineageReference,
    ]
):
    references = (
        FinalizedLineageReference(
            computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
            form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
            historical_version_id="KIT-VER-20230701-A",
            supported_lane_id="resident_employment_income_2023_07_01",
            tax_year=2023,
            tax_type="income_tax",
            policy_anchor_ids=("POL-001",),
            source_anchor_ids=("SRC-001",),
        ),
        FinalizedLineageReference(
            computation_id="3b1d40a8-c1c5-566d-8d4a-a17d14551697",
            form_id="0a7cb837-55dd-58cb-9d00-8bd33cf0532c",
            historical_version_id="HCH-VER-20100716-A",
            supported_lane_id="health_contribution_nhif_legacy_v1_2010_07_16",
            tax_year=2012,
            tax_type="health_contribution",
            policy_anchor_ids=("HCP-POL-106",),
            source_anchor_ids=("HC-NHIF-CONTRIB-REG-2010-07-16",),
        ),
        FinalizedLineageReference(
            computation_id="bf80513f-f7dd-5257-9f4d-656eebc2c2f5",
            form_id="85bfa98d-e3e9-5829-aad6-047e7dc97f8c",
            historical_version_id="HCH-VER-20241001-A",
            supported_lane_id="health_contribution_sha_shif_v1_2024_10_01",
            tax_year=2024,
            tax_type="health_contribution",
            policy_anchor_ids=("HCP-POL-204",),
            source_anchor_ids=("HC-SHI-REG-2024-09-20",),
        ),
    )
    return {
        (
            reference.computation_id,
            reference.form_id,
            reference.historical_version_id,
            reference.supported_lane_id,
            reference.tax_year,
        ): reference
        for reference in references
    }


def _serialize_report_response(
    *,
    report: ReportGenerationResponseModel,
) -> dict[str, object]:
    report_payload = cast(dict[str, object], asdict(report))
    lineage = report_payload.get("lineage_reference")
    if isinstance(lineage, dict):
        lineage_object = cast(dict[str, object], lineage)
        normalized_lineage = dict(sorted(lineage_object.items()))
        report_payload["lineage_reference"] = normalized_lineage
    return report_payload


def _build_report_type_metadata(
    *,
    report_payload: dict[str, object],
    tenant_id: str,
) -> dict[str, object]:
    lineage = cast(dict[str, object], report_payload["lineage_reference"])
    return {
        "report_type": report_payload["report_type"],
        "report_version_id": report_payload["report_version_id"],
        "historical_version_id": lineage["historical_version_id"],
        "supported_lane_id": lineage["supported_lane_id"],
        "policy_anchor_ids": list(cast(tuple[str, ...], lineage["policy_anchor_ids"])),
        "source_anchor_ids": list(cast(tuple[str, ...], lineage["source_anchor_ids"])),
        "tenant_id": tenant_id,
        "retention_metadata": report_payload["retention_metadata"],
    }


def _parse_report_type_metadata(raw_value: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ReportRepositoryError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Persisted report metadata is invalid.",
            context={"report_type": raw_value},
        ) from error
    if not isinstance(parsed, dict):
        raise ReportRepositoryError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Persisted report metadata payload has invalid shape.",
            context={"report_type": raw_value},
        )
    parsed_object = cast(dict[str, object], parsed)
    required = {
        "report_type",
        "report_version_id",
        "historical_version_id",
        "supported_lane_id",
        "policy_anchor_ids",
        "source_anchor_ids",
        "tenant_id",
        "retention_metadata",
    }
    missing = sorted(required - set(parsed_object))
    if missing:
        raise ReportRepositoryError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Persisted report metadata payload is missing required fields.",
            context={"missing_fields": missing},
        )
    return parsed_object


def _build_payload_from_row(
    *,
    report_id: str,
    computation_id: str,
    form_id: str,
    tax_type: str,
    tax_year: int,
    metadata_payload: dict[str, object],
) -> dict[str, object]:
    report_version_id = str(metadata_payload["report_version_id"])
    lineage_reference = {
        "computation_id": computation_id,
        "form_id": form_id,
        "report_id": report_id,
        "report_version_id": report_version_id,
        "historical_version_id": str(metadata_payload["historical_version_id"]),
        "supported_lane_id": str(metadata_payload["supported_lane_id"]),
        "tax_type": tax_type,
        "tax_year": tax_year,
        "policy_anchor_ids": tuple(
            str(value) for value in cast(list[object], metadata_payload["policy_anchor_ids"])
        ),
        "source_anchor_ids": tuple(
            str(value) for value in cast(list[object], metadata_payload["source_anchor_ids"])
        ),
    }
    return {
        "status": "generated",
        "report_id": report_id,
        "report_type": str(metadata_payload["report_type"]),
        "tax_year": tax_year,
        "report_version_id": report_version_id,
        "lineage_reference": lineage_reference,
        "retention_metadata": _as_retention_metadata(metadata_payload["retention_metadata"]),
    }


def _as_retention_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {
            "retention_class": DEFAULT_REPORT_RETENTION_CLASS,
            "retention_expires_at": "",
            "cleanup_status": DEFAULT_REPORT_CLEANUP_STATUS,
        }
    retention_payload = cast(dict[str, object], value)
    return {
        "retention_class": str(
            retention_payload.get("retention_class", DEFAULT_REPORT_RETENTION_CLASS)
        ),
        "retention_expires_at": str(retention_payload.get("retention_expires_at", "")),
        "cleanup_status": str(
            retention_payload.get("cleanup_status", DEFAULT_REPORT_CLEANUP_STATUS)
        ),
    }


def _build_report_retention_metadata(
    *,
    report_type: str,
    created_at: datetime,
) -> dict[str, object]:
    retention_class = _retention_class_for_report_type(report_type=report_type)
    retention_days = REPORT_RETENTION_DAYS[retention_class]
    retention_expires_at = created_at + timedelta(days=retention_days)
    return {
        "retention_class": retention_class,
        "retention_expires_at": retention_expires_at.isoformat(),
        "cleanup_status": DEFAULT_REPORT_CLEANUP_STATUS,
    }


def _retention_class_for_report_type(*, report_type: str) -> str:
    normalized = report_type.strip().lower()
    if normalized in {"income_tax_summary", "health_contribution_summary"}:
        return "tax_summary"
    if normalized == "income_tax_worksheet":
        return "worksheet"
    if normalized == "income_tax_year_over_year":
        return "comparative_view"
    if normalized == "income_tax_audit_package_manifest":
        return "audit_package"
    return DEFAULT_REPORT_RETENTION_CLASS


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value.strip()

    env_values = _read_env_file_values()
    raw_database_url = env_values.get(DATABASE_URL_ENV_VAR)
    if raw_database_url:
        return raw_database_url

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def _read_env_file_values() -> dict[str, str]:
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        values[normalized_key] = value.strip().strip("\"'")
    return values


def _parse_uuid(*, cast_value: object, field_name: str) -> UUID:
    if not isinstance(cast_value, str) or not cast_value.strip():
        raise ReportRepositoryError(
            reason_code=INVALID_LINEAGE_REFERENCE,
            message=f"Report persistence requires a valid UUID `{field_name}`.",
            context={"field_name": field_name},
        )
    try:
        return UUID(cast_value)
    except ValueError as error:
        raise ReportRepositoryError(
            reason_code=INVALID_LINEAGE_REFERENCE,
            message=f"Report persistence requires a valid UUID `{field_name}`.",
            context={"field_name": field_name, "value": cast_value},
        ) from error
