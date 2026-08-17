"""Materialize deterministic computation execution records atomically."""

from __future__ import annotations

from uuid import UUID
from typing import cast
from typing import Literal
import hashlib
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Mapping

import psycopg

from services.tax_core.app.config import DEFAULT_RETENTION_DAYS
from services.tax_core.app.config import DEFAULT_COMPLIANCE_LOCK_DAYS
from services.tax_core.app.config import load_tax_core_persistence_config
from shared.determinism.input_hash import canonical_json_dumps
from shared.determinism.input_hash import canonicalize_for_hash
from services.tax_core.app.engine.execution_contract import ValidationFinding
from services.tax_core.app.engine.execution_contract import PersistedReplaySource
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import PersistedValidationSource
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationResult
from services.tax_core.app.engine.execution_contract import ComputationValidationContext
from services.tax_core.app.engine.execution_contract import ComputationFinalizationResult
from services.tax_core.app.engine.execution_contract import ComputationFinalizationContext
from services.tax_core.app.engine.execution_contract import ComputationLifecycleAuditDetails
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult

COMPUTATION_EXECUTED_EVENT_TYPE = "computation.executed"
COMPUTATION_VALIDATED_EVENT_TYPE = "computation.validated"
COMPUTATION_FINALIZED_EVENT_TYPE = "computation.finalized"
COMPUTATION_REPLAY_VERIFIED_EVENT_TYPE = "computation.replay_verified"
COMPUTATION_REPLAY_MISMATCH_EVENT_TYPE = "computation.replay_mismatch"
COMPUTATION_RESOURCE_TYPE = "computation"
INTERNAL_REPLAY_CONTEXT_KEY = "_kodi_replay_context"


@dataclass(frozen=True)
class _ExistingComputation:
    """Represent persisted computation identity looked up by idempotency key."""

    computation_id: UUID
    user_id: UUID
    input_hash: str


@dataclass(frozen=True)
class _ComputationFinalizationState:
    """Represent persisted computation finalization fields for update locking."""

    computation_id: UUID
    user_id: UUID
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    input_hash: str
    finalized_at: datetime | None
    finalized_audit_event_id: UUID | None


class MaterializationError(RuntimeError):
    """Represent deterministic execution materialization failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details

    def details(self) -> dict[str, object]:
        """Return deterministic error details."""

        details: dict[str, object] = {"reason": self.reason}
        if self._details is not None:
            details.update(self._details)
        return details


class IdempotencyConflictError(MaterializationError):
    """Represent hard failure for idempotency-key reuse with changed logical input."""


def materialize_execution_result(
    execution_request: ComputationExecutionRequest,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
    connection: psycopg.Connection | None = None,
    retention_days: int | None = None,
    compliance_lock_days: int | None = None,
) -> MaterializedComputationExecutionResult:
    """Persist computation/result/audit records in one deterministic transaction."""

    resolved_retention_days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
    resolved_compliance_lock_days = (
        DEFAULT_COMPLIANCE_LOCK_DAYS if compliance_lock_days is None else compliance_lock_days
    )

    if connection is None:
        try:
            config = load_tax_core_persistence_config()
        except RuntimeError as error:
            raise MaterializationError(
                reason="invalid_persistence_configuration",
                message=str(error),
            ) from error
        resolved_retention_days = config.retention_days
        resolved_compliance_lock_days = config.compliance_lock_days
        with psycopg.connect(config.database_url) as owned_connection:
            return _materialize_with_connection(
                connection=owned_connection,
                execution_request=execution_request,
                execution_result=execution_result,
                context=context,
                retention_days=resolved_retention_days,
                compliance_lock_days=resolved_compliance_lock_days,
            )

    return _materialize_with_connection(
        connection=connection,
        execution_request=execution_request,
        execution_result=execution_result,
        context=context,
        retention_days=resolved_retention_days,
        compliance_lock_days=resolved_compliance_lock_days,
    )


def _materialize_with_connection(
    connection: psycopg.Connection,
    execution_request: ComputationExecutionRequest,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
    retention_days: int,
    compliance_lock_days: int,
) -> MaterializedComputationExecutionResult:
    if retention_days <= 0:
        raise MaterializationError(
            reason="invalid_retention_days",
            message="Retention window days must be positive.",
        )
    if compliance_lock_days <= 0:
        raise MaterializationError(
            reason="invalid_compliance_lock_days",
            message="Compliance lock window days must be positive.",
        )

    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                existing_computation = _find_existing_computation(
                    cursor=cursor,
                    idempotency_key=context.idempotency_key,
                )
                if existing_computation is not None:
                    _validate_idempotent_reuse(
                        existing_computation=existing_computation,
                        execution_result=execution_result,
                        context=context,
                    )
                    return _load_materialized_response(
                        cursor=cursor,
                        computation_id=existing_computation.computation_id,
                    )

                computation_id = _insert_computation_row(
                    cursor=cursor,
                    execution_request=execution_request,
                    execution_result=execution_result,
                    context=context,
                    retention_days=retention_days,
                    compliance_lock_days=compliance_lock_days,
                )
                _insert_computation_result_row(
                    cursor=cursor,
                    computation_id=computation_id,
                    user_id=context.user_id,
                    normalized_input_payload=execution_request.input_payload,
                    result_payload=execution_result.result_payload,
                )
                _insert_audit_event_row(
                    cursor=cursor,
                    computation_id=computation_id,
                    execution_result=execution_result,
                    context=context,
                    retention_days=retention_days,
                )
                return _load_materialized_response(
                    cursor=cursor,
                    computation_id=computation_id,
                )
    except IdempotencyConflictError:
        raise
    except MaterializationError:
        raise
    except psycopg.Error as error:
        raise MaterializationError(
            reason="database_error",
            message="Database materialization failed for deterministic computation.",
            details={"db_error_type": error.__class__.__name__},
        ) from error
    except Exception as error:
        raise MaterializationError(
            reason="unexpected_materialization_error",
            message="Unexpected failure while materializing deterministic computation.",
            details={"error_type": error.__class__.__name__},
        ) from error


def _find_existing_computation(
    cursor: psycopg.Cursor[tuple[object, ...]],
    idempotency_key: str,
) -> _ExistingComputation | None:
    cursor.execute(
        """
        SELECT id, user_id, input_hash
        FROM computations
        WHERE idempotency_key = %s
        FOR UPDATE
        """,
        (idempotency_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    return _ExistingComputation(
        computation_id=cast(UUID, row[0]),
        user_id=cast(UUID, row[1]),
        input_hash=cast(str, row[2]),
    )


def _validate_idempotent_reuse(
    existing_computation: _ExistingComputation,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
) -> None:
    if existing_computation.user_id != context.user_id:
        raise IdempotencyConflictError(
            reason="idempotency_key_user_mismatch",
            message="Idempotency key cannot be reused across different users.",
            details={"idempotency_key": context.idempotency_key},
        )
    if existing_computation.input_hash != execution_result.input_hash:
        raise IdempotencyConflictError(
            reason="idempotency_key_input_hash_mismatch",
            message="Idempotency key reuse requires identical logical input hash.",
            details={"idempotency_key": context.idempotency_key},
        )


def _insert_computation_row(
    cursor: psycopg.Cursor[tuple[object, ...]],
    execution_request: ComputationExecutionRequest,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
    retention_days: int,
    compliance_lock_days: int,
) -> UUID:
    cursor.execute(
        """
        INSERT INTO computations (
            user_id,
            session_id,
            tax_type,
            regime_type,
            regime_identifier,
            tax_year,
            rule_version,
            input_hash,
            idempotency_key,
            correlation_id,
            retention_expires_at,
            compliance_lock_until
        ) VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            now() + make_interval(days => %s),
            now() + make_interval(days => %s)
        )
        RETURNING id
        """,
        (
            context.user_id,
            context.session_id,
            execution_request.tax_type,
            execution_request.regime_type,
            execution_request.regime_identifier,
            execution_request.tax_year,
            execution_request.rule_version,
            execution_result.input_hash,
            context.idempotency_key,
            context.correlation_id,
            retention_days,
            compliance_lock_days,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_computation_id",
            message="Computation insert did not return an identifier.",
        )
    return cast(UUID, row[0])


def _insert_computation_result_row(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
    user_id: UUID,
    normalized_input_payload: dict[str, object],
    result_payload: dict[str, object],
) -> None:
    cursor.execute(
        """
        INSERT INTO computation_results (
            computation_id,
            user_id,
            result_payload
        ) VALUES (%s, %s, %s::jsonb)
        """,
        (
            computation_id,
            user_id,
            canonical_json_dumps(
                _build_persisted_result_payload(
                    normalized_input_payload=normalized_input_payload,
                    result_payload=result_payload,
                )
            ),
        ),
    )


def _insert_audit_event_row(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
    retention_days: int,
) -> UUID:
    previous_event_hash = _get_latest_audit_event_hash(
        cursor=cursor,
        user_id=context.user_id,
        resource_type=COMPUTATION_RESOURCE_TYPE,
        resource_id=computation_id,
    )

    cursor.execute(
        """
        INSERT INTO audit_events (
            user_id,
            role_at_time,
            event_type,
            resource_type,
            resource_id,
            correlation_id,
            idempotency_key,
            details,
            previous_event_hash,
            event_hash,
            retention_expires_at
        ) VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            now() + make_interval(days => %s)
        )
        RETURNING id
        """,
        (
            context.user_id,
            context.role_at_time,
            COMPUTATION_EXECUTED_EVENT_TYPE,
            COMPUTATION_RESOURCE_TYPE,
            computation_id,
            context.correlation_id,
            context.idempotency_key,
            canonical_json_dumps(
                _build_lifecycle_audit_details(
                    lifecycle_stage="execution",
                    computation_id=computation_id,
                    correlation_id=context.correlation_id,
                    idempotency_key=context.idempotency_key,
                    tax_year=execution_result.tax_year,
                    rule_version=execution_result.rule_version,
                    input_hash=execution_result.input_hash,
                    outcome="succeeded",
                    tax_type=execution_result.tax_type,
                    regime_type=execution_result.regime_type,
                    result_sha256=_sha256_hex(
                        canonical_json_dumps(execution_result.result_payload)
                    ),
                )
            ),
            previous_event_hash,
            None,
            retention_days,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_audit_event_id",
            message="Audit event insert did not return an identifier.",
        )
    return cast(UUID, row[0])


def _load_materialized_response(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
) -> MaterializedComputationExecutionResult:
    cursor.execute(
        """
        SELECT
            computations.id,
            computations.idempotency_key,
            computations.correlation_id,
            computations.tax_type,
            computations.regime_type,
            computations.tax_year,
            computations.rule_version,
            computations.input_hash,
            computation_results.computation_id,
            computation_results.result_payload,
            audit_events.id
        FROM computations
        INNER JOIN computation_results
            ON computation_results.computation_id = computations.id
        INNER JOIN audit_events
            ON audit_events.idempotency_key = computations.idempotency_key
        WHERE computations.id = %s
        ORDER BY audit_events.created_at ASC, audit_events.id ASC
        LIMIT 1
        """,
        (computation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="incomplete_materialization",
            message="Materialized computation record is incomplete.",
            details={"computation_id": str(computation_id)},
        )

    result_payload = _extract_public_result_payload(_normalize_object_payload(row[9]))
    return MaterializedComputationExecutionResult(
        status="ok",
        computation_id=cast(UUID, row[0]),
        computation_result_id=cast(UUID, row[8]),
        audit_event_id=cast(UUID, row[10]),
        idempotency_key=cast(str, row[1]),
        correlation_id=cast(str, row[2]),
        tax_type=cast(str, row[3]),
        regime_type=cast(str, row[4]),
        tax_year=cast(int, row[5]),
        rule_version=cast(str, row[6]),
        input_hash=cast(str, row[7]),
        result_payload=result_payload,
    )


def load_persisted_replay_source(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
) -> PersistedReplaySource:
    """Load persisted governed replay fields for one computation."""

    cursor.execute(
        """
        SELECT
            computations.id,
            computations.user_id,
            computations.tax_type,
            computations.regime_type,
            computations.regime_identifier,
            computations.tax_year,
            computations.rule_version,
            computations.input_hash,
            computation_results.result_payload
        FROM computations
        INNER JOIN computation_results
            ON computation_results.computation_id = computations.id
        WHERE computations.id = %s
        FOR SHARE
        """,
        (computation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="computation_not_found",
            message="Persisted computation was not found for replay verification.",
            details={"computation_id": str(computation_id)},
        )

    persisted_result_payload = _normalize_object_payload(row[8])
    persisted_input_payload = _extract_persisted_replay_input_payload(
        persisted_result_payload=persisted_result_payload,
        computation_id=computation_id,
    )
    stored_result_payload = _extract_public_result_payload(persisted_result_payload)

    return PersistedReplaySource(
        computation_id=cast(UUID, row[0]),
        user_id=cast(UUID, row[1]),
        tax_type=cast(str, row[2]),
        regime_type=cast(str, row[3]),
        regime_identifier=cast(str | None, row[4]),
        tax_year=cast(int, row[5]),
        rule_version=cast(str, row[6]),
        input_hash=cast(str, row[7]),
        persisted_input_payload=persisted_input_payload,
        stored_result_payload=stored_result_payload,
    )


def append_replay_audit_event(
    cursor: psycopg.Cursor[tuple[object, ...]],
    persisted_source: PersistedReplaySource,
    replay_context: ReplayVerificationContext,
    verification_outcome: str,
    replay_result_payload: dict[str, object],
    mismatch_reason: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> UUID:
    """Append replay verification audit evidence for matched or mismatched replay."""

    if retention_days <= 0:
        raise MaterializationError(
            reason="invalid_retention_days",
            message="Retention window days must be positive.",
        )

    if verification_outcome == "matched":
        event_type = COMPUTATION_REPLAY_VERIFIED_EVENT_TYPE
    elif verification_outcome == "mismatch":
        event_type = COMPUTATION_REPLAY_MISMATCH_EVENT_TYPE
    else:
        raise MaterializationError(
            reason="invalid_replay_outcome",
            message="Replay verification outcome must be 'matched' or 'mismatch'.",
            details={"verification_outcome": verification_outcome},
        )

    previous_event_hash = _get_latest_audit_event_hash(
        cursor=cursor,
        user_id=replay_context.user_id,
        resource_type=COMPUTATION_RESOURCE_TYPE,
        resource_id=persisted_source.computation_id,
    )

    stored_result_json = canonical_json_dumps(persisted_source.stored_result_payload)
    replay_result_json = canonical_json_dumps(replay_result_payload)
    audit_details = _build_lifecycle_audit_details(
        lifecycle_stage="replay",
        computation_id=persisted_source.computation_id,
        correlation_id=replay_context.correlation_id,
        idempotency_key=replay_context.idempotency_key,
        tax_year=persisted_source.tax_year,
        rule_version=persisted_source.rule_version,
        input_hash=persisted_source.input_hash,
        outcome=verification_outcome,
        tax_type=persisted_source.tax_type,
        regime_type=persisted_source.regime_type,
        verification_outcome=verification_outcome,
        mismatch_reason=mismatch_reason,
        stored_result_sha256=_sha256_hex(stored_result_json),
        replay_result_sha256=_sha256_hex(replay_result_json),
    )

    cursor.execute(
        """
        INSERT INTO audit_events (
            user_id,
            role_at_time,
            event_type,
            resource_type,
            resource_id,
            correlation_id,
            idempotency_key,
            details,
            previous_event_hash,
            event_hash,
            retention_expires_at
        ) VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            now() + make_interval(days => %s)
        )
        RETURNING id
        """,
        (
            replay_context.user_id,
            replay_context.role_at_time,
            event_type,
            COMPUTATION_RESOURCE_TYPE,
            persisted_source.computation_id,
            replay_context.correlation_id,
            replay_context.idempotency_key,
            canonical_json_dumps(audit_details),
            previous_event_hash,
            None,
            retention_days,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_replay_audit_event_id",
            message="Replay audit insert did not return an identifier.",
            details={"computation_id": str(persisted_source.computation_id)},
        )
    return cast(UUID, row[0])


def load_computation_finalization_state_for_update(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
) -> _ComputationFinalizationState | None:
    """Load persisted finalization state for one computation under row-level lock."""

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            tax_type,
            regime_type,
            tax_year,
            rule_version,
            input_hash,
            finalized_at,
            finalized_audit_event_id
        FROM computations
        WHERE id = %s
        FOR UPDATE
        """,
        (computation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    return _ComputationFinalizationState(
        computation_id=cast(UUID, row[0]),
        user_id=cast(UUID, row[1]),
        tax_type=cast(str, row[2]),
        regime_type=cast(str, row[3]),
        tax_year=cast(int, row[4]),
        rule_version=cast(str, row[5]),
        input_hash=cast(str, row[6]),
        finalized_at=cast(datetime | None, row[7]),
        finalized_audit_event_id=cast(UUID | None, row[8]),
    )


def append_finalization_audit_event(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
    context: ComputationFinalizationContext,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> UUID:
    """Append deterministic computation finalization audit evidence."""

    if retention_days <= 0:
        raise MaterializationError(
            reason="invalid_retention_days",
            message="Retention window days must be positive.",
        )

    finalization_state = load_computation_finalization_state_for_update(
        cursor=cursor,
        computation_id=computation_id,
    )
    if finalization_state is None:
        raise MaterializationError(
            reason="computation_not_found",
            message="Persisted computation was not found for finalization.",
            details={"computation_id": str(computation_id)},
        )

    previous_event_hash = _get_latest_audit_event_hash(
        cursor=cursor,
        user_id=context.user_id,
        resource_type=COMPUTATION_RESOURCE_TYPE,
        resource_id=finalization_state.computation_id,
    )

    cursor.execute(
        """
        INSERT INTO audit_events (
            user_id,
            role_at_time,
            event_type,
            resource_type,
            resource_id,
            correlation_id,
            idempotency_key,
            details,
            previous_event_hash,
            event_hash,
            retention_expires_at
        ) VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            now() + make_interval(days => %s)
        )
        RETURNING id
        """,
        (
            context.user_id,
            context.role_at_time,
            COMPUTATION_FINALIZED_EVENT_TYPE,
            COMPUTATION_RESOURCE_TYPE,
            finalization_state.computation_id,
            context.correlation_id,
            context.idempotency_key,
            canonical_json_dumps(
                _build_lifecycle_audit_details(
                    lifecycle_stage="finalization",
                    computation_id=finalization_state.computation_id,
                    correlation_id=context.correlation_id,
                    idempotency_key=context.idempotency_key,
                    tax_year=finalization_state.tax_year,
                    rule_version=finalization_state.rule_version,
                    input_hash=finalization_state.input_hash,
                    outcome="finalized",
                    tax_type=finalization_state.tax_type,
                    regime_type=finalization_state.regime_type,
                    finalization_status="finalized",
                )
            ),
            previous_event_hash,
            None,
            retention_days,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_finalization_audit_event_id",
            message="Finalization audit insert did not return an identifier.",
            details={"computation_id": str(finalization_state.computation_id)},
        )
    return cast(UUID, row[0])


def set_computation_finalization_fields(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
    finalized_audit_event_id: UUID,
) -> tuple[datetime, UUID]:
    """Mark one computation finalized and return persisted finalization fields."""

    cursor.execute(
        """
        UPDATE computations
        SET finalized_at = now(),
            finalized_audit_event_id = %s
        WHERE id = %s
        RETURNING finalized_at, finalized_audit_event_id
        """,
        (finalized_audit_event_id, computation_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="computation_not_found",
            message="Persisted computation was not found for finalization.",
            details={"computation_id": str(computation_id)},
        )

    persisted_finalized_at = cast(datetime | None, row[0])
    persisted_audit_event_id = cast(UUID | None, row[1])
    if persisted_finalized_at is None or persisted_audit_event_id is None:
        raise MaterializationError(
            reason="invalid_finalization_state",
            message="Computation finalization fields were not persisted atomically.",
            details={"computation_id": str(computation_id)},
        )
    return persisted_finalized_at, persisted_audit_event_id


def build_finalization_result(
    computation_id: UUID,
    finalized_at: datetime,
    finalized_audit_event_id: UUID,
    context: ComputationFinalizationContext,
) -> ComputationFinalizationResult:
    """Build canonical finalization response envelope."""

    return ComputationFinalizationResult(
        status="ok",
        finalization_status="finalized",
        computation_id=computation_id,
        finalized_at=finalized_at,
        finalized_audit_event_id=finalized_audit_event_id,
        correlation_id=context.correlation_id,
        idempotency_key=context.idempotency_key,
    )


def load_persisted_validation_source(
    cursor: psycopg.Cursor[tuple[object, ...]],
    computation_id: UUID,
) -> PersistedValidationSource:
    """Load persisted computation lineage required for deterministic validation."""

    cursor.execute(
        """
        SELECT
            computations.id,
            computations.user_id,
            computations.tax_type,
            computations.regime_type,
            computations.regime_identifier,
            computations.tax_year,
            computations.rule_version,
            computations.input_hash,
            computation_results.result_payload
        FROM computations
        INNER JOIN computation_results
            ON computation_results.computation_id = computations.id
        WHERE computations.id = %s
        FOR SHARE
        """,
        (computation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="computation_not_found",
            message="Persisted computation was not found for validation.",
            details={"computation_id": str(computation_id)},
        )

    return PersistedValidationSource(
        computation_id=cast(UUID, row[0]),
        user_id=cast(UUID, row[1]),
        tax_type=cast(str, row[2]),
        regime_type=cast(str, row[3]),
        regime_identifier=cast(str | None, row[4]),
        tax_year=cast(int, row[5]),
        rule_version=cast(str, row[6]),
        input_hash=cast(str, row[7]),
        stored_result_payload=_extract_public_result_payload(_normalize_object_payload(row[8])),
    )


def insert_validation_row(
    cursor: psycopg.Cursor[tuple[object, ...]],
    persisted_source: PersistedValidationSource,
    context: ComputationValidationContext,
    validation_context: str,
    findings: list[ValidationFinding],
) -> UUID:
    """Persist one computation-bound validation row with canonical findings payload."""

    findings_payload = {
        "tax_year": persisted_source.tax_year,
        "rule_version": persisted_source.rule_version,
        "input_hash": persisted_source.input_hash,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    cursor.execute(
        """
        INSERT INTO validations (
            computation_id,
            user_id,
            validation_context,
            findings
        ) VALUES (%s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (
            persisted_source.computation_id,
            context.user_id,
            validation_context,
            canonical_json_dumps(findings_payload),
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_validation_id",
            message="Validation insert did not return an identifier.",
            details={"computation_id": str(persisted_source.computation_id)},
        )
    validation_id = cast(UUID, row[0])
    append_validation_audit_event(
        cursor=cursor,
        persisted_source=persisted_source,
        context=context,
        validation_id=validation_id,
        validation_context=validation_context,
        findings=findings,
    )
    return validation_id


def append_validation_audit_event(
    cursor: psycopg.Cursor[tuple[object, ...]],
    persisted_source: PersistedValidationSource,
    context: ComputationValidationContext,
    validation_id: UUID,
    validation_context: str,
    findings: list[ValidationFinding],
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> UUID:
    """Append deterministic computation validation audit evidence."""

    if retention_days <= 0:
        raise MaterializationError(
            reason="invalid_retention_days",
            message="Retention window days must be positive.",
        )

    previous_event_hash = _get_latest_audit_event_hash(
        cursor=cursor,
        user_id=context.user_id,
        resource_type=COMPUTATION_RESOURCE_TYPE,
        resource_id=persisted_source.computation_id,
    )
    finding_severities = sorted({finding.severity for finding in findings})
    cursor.execute(
        """
        INSERT INTO audit_events (
            user_id,
            role_at_time,
            event_type,
            resource_type,
            resource_id,
            correlation_id,
            idempotency_key,
            details,
            previous_event_hash,
            event_hash,
            retention_expires_at
        ) VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            now() + make_interval(days => %s)
        )
        RETURNING id
        """,
        (
            context.user_id,
            context.role_at_time,
            COMPUTATION_VALIDATED_EVENT_TYPE,
            COMPUTATION_RESOURCE_TYPE,
            persisted_source.computation_id,
            context.correlation_id,
            context.idempotency_key,
            canonical_json_dumps(
                _build_lifecycle_audit_details(
                    lifecycle_stage="validation",
                    computation_id=persisted_source.computation_id,
                    correlation_id=context.correlation_id,
                    idempotency_key=context.idempotency_key,
                    tax_year=persisted_source.tax_year,
                    rule_version=persisted_source.rule_version,
                    input_hash=persisted_source.input_hash,
                    outcome="succeeded",
                    tax_type=persisted_source.tax_type,
                    regime_type=persisted_source.regime_type,
                    validation_id=validation_id,
                    validation_context=validation_context,
                    finding_count=len(findings),
                    finding_severities=finding_severities,
                )
            ),
            previous_event_hash,
            None,
            retention_days,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise MaterializationError(
            reason="missing_validation_audit_event_id",
            message="Validation audit insert did not return an identifier.",
            details={"computation_id": str(persisted_source.computation_id)},
        )
    return cast(UUID, row[0])


def build_validation_result(
    validation_id: UUID,
    persisted_source: PersistedValidationSource,
    context: ComputationValidationContext,
    validation_context: str,
    findings: list[ValidationFinding],
) -> ComputationValidationResult:
    """Build canonical persisted validation response envelope."""

    return ComputationValidationResult(
        status="ok",
        validation_id=validation_id,
        computation_id=persisted_source.computation_id,
        validation_context=validation_context,
        correlation_id=context.correlation_id,
        idempotency_key=context.idempotency_key,
        tax_year=persisted_source.tax_year,
        rule_version=persisted_source.rule_version,
        findings=findings,
    )


def _build_lifecycle_audit_details(
    lifecycle_stage: str,
    computation_id: UUID,
    correlation_id: str,
    idempotency_key: str,
    tax_year: int,
    rule_version: str,
    input_hash: str,
    outcome: str,
    tax_type: str | None = None,
    regime_type: str | None = None,
    result_sha256: str | None = None,
    validation_id: UUID | None = None,
    validation_context: str | None = None,
    finding_count: int | None = None,
    finding_severities: list[str] | None = None,
    finalization_status: str | None = None,
    verification_outcome: str | None = None,
    mismatch_reason: str | None = None,
    stored_result_sha256: str | None = None,
    replay_result_sha256: str | None = None,
) -> dict[str, object]:
    audit_details = ComputationLifecycleAuditDetails(
        lifecycle_stage=cast(
            Literal["execution", "validation", "finalization", "replay"],
            lifecycle_stage,
        ),
        computation_id=computation_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        tax_year=tax_year,
        rule_version=rule_version,
        input_hash=input_hash,
        outcome=cast(
            Literal["succeeded", "finalized", "matched", "mismatch"],
            outcome,
        ),
        tax_type=tax_type,
        regime_type=regime_type,
        result_sha256=result_sha256,
        validation_id=validation_id,
        validation_context=validation_context,
        finding_count=finding_count,
        finding_severities=cast(
            list[Literal["info", "warning", "error"]] | None,
            finding_severities,
        ),
        finalization_status=finalization_status,
        verification_outcome=cast(
            Literal["matched", "mismatch"] | None,
            verification_outcome,
        ),
        mismatch_reason=mismatch_reason,
        stored_result_sha256=stored_result_sha256,
        replay_result_sha256=replay_result_sha256,
    )
    return cast(
        dict[str, object],
        audit_details.model_dump(mode="json", exclude_none=True),
    )


def _normalize_object_payload(value: object) -> dict[str, object]:
    canonical_value = canonicalize_for_hash(value)
    if not isinstance(canonical_value, Mapping):
        raise MaterializationError(
            reason="invalid_persisted_result_payload",
            message="Persisted result payload must be a JSON object.",
        )

    canonical_mapping = cast(Mapping[object, object], canonical_value)
    return {str(key): canonical_mapping[key] for key in sorted(canonical_mapping, key=str)}


def _build_persisted_result_payload(
    normalized_input_payload: dict[str, object],
    result_payload: dict[str, object],
) -> dict[str, object]:
    persisted_payload = dict(result_payload)
    persisted_payload[INTERNAL_REPLAY_CONTEXT_KEY] = {
        "normalized_input": normalized_input_payload,
    }
    return persisted_payload


def _extract_public_result_payload(
    persisted_result_payload: dict[str, object],
) -> dict[str, object]:
    public_payload = dict(persisted_result_payload)
    public_payload.pop(INTERNAL_REPLAY_CONTEXT_KEY, None)
    return public_payload


def _extract_persisted_replay_input_payload(
    persisted_result_payload: dict[str, object],
    computation_id: UUID,
) -> dict[str, object]:
    replay_context_value = persisted_result_payload.get(INTERNAL_REPLAY_CONTEXT_KEY)
    if isinstance(replay_context_value, Mapping):
        replay_context = _normalize_object_payload(cast(object, replay_context_value))
        normalized_input_value = replay_context.get("normalized_input")
        if normalized_input_value is not None:
            return _normalize_object_payload(normalized_input_value)

    persisted_input_value = persisted_result_payload.get("normalized_input")
    if persisted_input_value is not None:
        return _normalize_object_payload(persisted_input_value)

    raise MaterializationError(
        reason="missing_persisted_input_context",
        message="Persisted computation result is missing normalized input context.",
        details={"computation_id": str(computation_id)},
    )


def _get_latest_audit_event_hash(
    cursor: psycopg.Cursor[tuple[object, ...]],
    user_id: UUID,
    resource_type: str,
    resource_id: UUID,
) -> str | None:
    cursor.execute(
        """
        SELECT event_hash
        FROM audit_events
        WHERE user_id = %s
          AND resource_type = %s
          AND resource_id = %s
        ORDER BY event_timestamp DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (
            user_id,
            resource_type,
            resource_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return cast(str, row[0])


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
