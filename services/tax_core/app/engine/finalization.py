"""Deterministic computation finalization service."""

from __future__ import annotations

import psycopg

from services.tax_core.app.config import DEFAULT_RETENTION_DAYS
from services.tax_core.app.config import load_tax_core_persistence_config
from services.tax_core.app.engine.execution_contract import ComputationFinalizationResult
from services.tax_core.app.engine.execution_contract import ComputationFinalizationContext
from services.tax_core.app.engine.execution_contract import ComputationFinalizationRequest
from services.tax_core.app.persistence.materialization import MaterializationError
from services.tax_core.app.persistence.materialization import build_finalization_result
from services.tax_core.app.persistence.materialization import append_finalization_audit_event
from services.tax_core.app.persistence.materialization import set_computation_finalization_fields
from services.tax_core.app.persistence.materialization import (
    load_computation_finalization_state_for_update,
)


class FinalizationError(RuntimeError):
    """Represent deterministic computation finalization failures."""

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
        """Return deterministic details payload for finalization failure mapping."""

        details: dict[str, object] = {"reason": self.reason}
        if self._details is not None:
            details.update(self._details)
        return details


def finalize_computation(
    finalization_request: ComputationFinalizationRequest,
    finalization_context: ComputationFinalizationContext,
    connection: psycopg.Connection | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> ComputationFinalizationResult:
    """Finalize one persisted computation and return idempotent finalized state."""

    if retention_days <= 0:
        raise FinalizationError(
            reason="invalid_retention_days",
            message="Retention window days must be positive.",
            status_code=500,
        )

    if connection is None:
        try:
            config = load_tax_core_persistence_config()
        except RuntimeError as error:
            raise FinalizationError(
                reason="invalid_persistence_configuration",
                message=str(error),
                status_code=500,
            ) from error
        with psycopg.connect(config.database_url) as owned_connection:
            return _finalize_with_connection(
                finalization_request=finalization_request,
                finalization_context=finalization_context,
                connection=owned_connection,
                retention_days=config.retention_days,
            )

    return _finalize_with_connection(
        finalization_request=finalization_request,
        finalization_context=finalization_context,
        connection=connection,
        retention_days=retention_days,
    )


def _finalize_with_connection(
    finalization_request: ComputationFinalizationRequest,
    finalization_context: ComputationFinalizationContext,
    connection: psycopg.Connection,
    retention_days: int,
) -> ComputationFinalizationResult:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                finalization_state = load_computation_finalization_state_for_update(
                    cursor=cursor,
                    computation_id=finalization_request.computation_id,
                )
                if finalization_state is None:
                    raise FinalizationError(
                        reason="computation_not_found",
                        message="Persisted computation was not found for finalization.",
                        status_code=404,
                        details={
                            "computation_id": str(finalization_request.computation_id),
                        },
                    )
                if finalization_state.user_id != finalization_context.user_id:
                    raise FinalizationError(
                        reason="computation_not_found",
                        message="Persisted computation was not found for finalization.",
                        status_code=404,
                        details={
                            "computation_id": str(finalization_request.computation_id),
                        },
                    )

                if finalization_state.finalized_at is not None:
                    if finalization_state.finalized_audit_event_id is None:
                        raise FinalizationError(
                            reason="invalid_finalization_state",
                            message=("Persisted computation has invalid finalization metadata."),
                            status_code=500,
                            details={
                                "computation_id": str(finalization_request.computation_id),
                            },
                        )
                    return build_finalization_result(
                        computation_id=finalization_state.computation_id,
                        finalized_at=finalization_state.finalized_at,
                        finalized_audit_event_id=finalization_state.finalized_audit_event_id,
                        context=finalization_context,
                    )

                finalized_audit_event_id = append_finalization_audit_event(
                    cursor=cursor,
                    computation_id=finalization_state.computation_id,
                    context=finalization_context,
                    retention_days=retention_days,
                )
                finalized_at, persisted_audit_event_id = set_computation_finalization_fields(
                    cursor=cursor,
                    computation_id=finalization_state.computation_id,
                    finalized_audit_event_id=finalized_audit_event_id,
                )
                return build_finalization_result(
                    computation_id=finalization_state.computation_id,
                    finalized_at=finalized_at,
                    finalized_audit_event_id=persisted_audit_event_id,
                    context=finalization_context,
                )
    except FinalizationError:
        raise
    except MaterializationError as error:
        raise FinalizationError(
            reason="finalization_failed",
            message="Failed to finalize persisted deterministic computation.",
            status_code=500,
            details=error.details(),
        ) from error
    except psycopg.Error as error:
        raise FinalizationError(
            reason="finalization_failed",
            message="Database error while finalizing deterministic computation.",
            status_code=500,
            details={"db_error_type": error.__class__.__name__},
        ) from error
