"""Deterministic replay verification for persisted tax-core computations."""

from __future__ import annotations

from uuid import UUID

import psycopg

from services.tax_core.app.config import load_tax_core_persistence_config
from shared.determinism.input_hash import InputHashError
from shared.determinism.input_hash import canonical_json_dumps
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.execution_contract import ReplayVerificationResult
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.persistence.materialization import MaterializationError
from services.tax_core.app.persistence.materialization import append_replay_audit_event
from services.tax_core.app.persistence.materialization import load_persisted_replay_source


class ReplayVerificationError(RuntimeError):
    """Represent deterministic replay verification failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        status_code: int,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code
        self._details = details

    def details(self) -> dict[str, object]:
        """Return deterministic details payload for replay failure mapping."""

        details: dict[str, object] = {"reason": self.reason}
        if self._details is not None:
            details.update(self._details)
        return details


def verify_persisted_computation_replay(
    replay_request: ReplayVerificationRequest,
    replay_context: ReplayVerificationContext,
    connection: psycopg.Connection | None = None,
) -> ReplayVerificationResult:
    """Recompute persisted computation deterministically and verify stored result consistency."""

    if connection is None:
        try:
            config = load_tax_core_persistence_config()
        except RuntimeError as error:
            raise ReplayVerificationError(
                reason="invalid_persistence_configuration",
                message=str(error),
                status_code=500,
            ) from error

        with psycopg.connect(config.database_url) as owned_connection:
            return _verify_with_connection(
                replay_request=replay_request,
                replay_context=replay_context,
                connection=owned_connection,
            )

    return _verify_with_connection(
        replay_request=replay_request,
        replay_context=replay_context,
        connection=connection,
    )


def _verify_with_connection(
    replay_request: ReplayVerificationRequest,
    replay_context: ReplayVerificationContext,
    connection: psycopg.Connection,
) -> ReplayVerificationResult:
    try:
        replay_success_result: ReplayVerificationResult | None = None
        replay_mismatch_error: ReplayVerificationError | None = None
        with connection.transaction():
            with connection.cursor() as cursor:
                persisted_source = load_persisted_replay_source(
                    cursor=cursor,
                    computation_id=replay_request.computation_id,
                )
                _enforce_replay_principal_lineage(
                    persisted_user_id=persisted_source.user_id,
                    principal_user_id=replay_context.user_id,
                    computation_id=persisted_source.computation_id,
                )

                replay_execution_request = ComputationExecutionRequest(
                    tax_type=persisted_source.tax_type,
                    regime_type=persisted_source.regime_type,
                    regime_identifier=persisted_source.regime_identifier,
                    tax_year=persisted_source.tax_year,
                    rule_version=persisted_source.rule_version,
                    input_payload=persisted_source.persisted_input_payload,
                )
                try:
                    replay_execution_result = execute_computation(replay_execution_request)
                except RuleBindingError as error:
                    raise ReplayVerificationError(
                        reason=error.reason,
                        message=error.message,
                        status_code=409,
                        details={
                            **error.details(),
                            "computation_id": str(persisted_source.computation_id),
                        },
                    ) from error
                except InputHashError as error:
                    raise ReplayVerificationError(
                        reason=error.reason,
                        message=error.message,
                        status_code=409,
                        details={
                            "path": error.path,
                            "computation_id": str(persisted_source.computation_id),
                        },
                    ) from error

                if replay_execution_result.input_hash != persisted_source.input_hash:
                    mismatch_audit_event_id = append_replay_audit_event(
                        cursor=cursor,
                        persisted_source=persisted_source,
                        replay_context=replay_context,
                        verification_outcome="mismatch",
                        replay_result_payload=replay_execution_result.result_payload,
                        mismatch_reason="replay_input_hash_mismatch",
                    )
                    replay_mismatch_error = ReplayVerificationError(
                        reason="replay_input_hash_mismatch",
                        message="Replay input hash does not match persisted computation hash.",
                        status_code=409,
                        details={
                            "computation_id": str(persisted_source.computation_id),
                            "replay_audit_event_id": str(mismatch_audit_event_id),
                        },
                    )
                else:
                    stored_result_json = canonical_json_dumps(
                        persisted_source.stored_result_payload
                    )
                    replay_result_json = canonical_json_dumps(
                        replay_execution_result.result_payload
                    )
                    if stored_result_json != replay_result_json:
                        mismatch_audit_event_id = append_replay_audit_event(
                            cursor=cursor,
                            persisted_source=persisted_source,
                            replay_context=replay_context,
                            verification_outcome="mismatch",
                            replay_result_payload=replay_execution_result.result_payload,
                            mismatch_reason="replay_result_mismatch",
                        )
                        replay_mismatch_error = ReplayVerificationError(
                            reason="replay_result_mismatch",
                            message=(
                                "Replay result payload does not match persisted result payload."
                            ),
                            status_code=409,
                            details={
                                "computation_id": str(persisted_source.computation_id),
                                "replay_audit_event_id": str(mismatch_audit_event_id),
                            },
                        )
                    else:
                        success_audit_event_id = append_replay_audit_event(
                            cursor=cursor,
                            persisted_source=persisted_source,
                            replay_context=replay_context,
                            verification_outcome="matched",
                            replay_result_payload=replay_execution_result.result_payload,
                        )
                        replay_success_result = ReplayVerificationResult(
                            status="ok",
                            verification_status="matched",
                            computation_id=persisted_source.computation_id,
                            replay_audit_event_id=success_audit_event_id,
                            correlation_id=replay_context.correlation_id,
                            idempotency_key=replay_context.idempotency_key,
                            tax_type=persisted_source.tax_type,
                            regime_type=persisted_source.regime_type,
                            tax_year=persisted_source.tax_year,
                            rule_version=persisted_source.rule_version,
                            input_hash=persisted_source.input_hash,
                        )
        if replay_mismatch_error is not None:
            raise replay_mismatch_error
        if replay_success_result is None:
            raise ReplayVerificationError(
                reason="replay_verification_failed",
                message="Replay verification completed without success or mismatch outcome.",
                status_code=500,
            )
        return replay_success_result
    except ReplayVerificationError:
        raise
    except MaterializationError as error:
        if error.reason == "computation_not_found":
            raise ReplayVerificationError(
                reason="computation_not_found",
                message="Persisted computation was not found for replay verification.",
                status_code=404,
                details=error.details(),
            ) from error

        raise ReplayVerificationError(
            reason="replay_verification_failed",
            message="Failed to verify deterministic replay against persisted computation.",
            status_code=500,
            details=error.details(),
        ) from error
    except psycopg.Error as error:
        raise ReplayVerificationError(
            reason="replay_verification_failed",
            message="Database error while verifying deterministic replay.",
            status_code=500,
            details={"db_error_type": error.__class__.__name__},
        ) from error


def _enforce_replay_principal_lineage(
    persisted_user_id: UUID,
    principal_user_id: UUID,
    computation_id: UUID,
) -> None:
    if persisted_user_id != principal_user_id:
        raise ReplayVerificationError(
            reason="computation_not_found",
            message="Persisted computation was not found for replay verification.",
            status_code=404,
            details={"computation_id": str(computation_id)},
        )
