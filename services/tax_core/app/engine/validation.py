"""Deterministic computation-bound validation hook."""

from __future__ import annotations

from uuid import UUID

import psycopg

from services.tax_core.app.config import load_tax_core_persistence_config
from services.tax_core.app.engine.execution_contract import ValidationFinding
from services.tax_core.app.engine.execution_contract import PersistedValidationSource
from services.tax_core.app.engine.execution_contract import ComputationValidationResult
from services.tax_core.app.engine.execution_contract import ComputationValidationContext
from services.tax_core.app.engine.execution_contract import ComputationValidationRequest
from services.tax_core.app.persistence.materialization import MaterializationError
from services.tax_core.app.persistence.materialization import insert_validation_row
from services.tax_core.app.persistence.materialization import build_validation_result
from services.tax_core.app.persistence.materialization import load_persisted_validation_source
from services.tax_core.app.rules.income_tax.validation_catalog import (
    derive_income_tax_validation_findings,
)
from services.tax_core.app.rules.health_contribution.validation_catalog import (
    derive_health_contribution_validation_findings,
)

DEFAULT_VALIDATION_CONTEXT = "deterministic_post_computation_validation"


class ValidationError(RuntimeError):
    """Represent deterministic validation hook failures."""

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
        """Return deterministic details payload for validation failure mapping."""

        details: dict[str, object] = {"reason": self.reason}
        if self._details is not None:
            details.update(self._details)
        return details


def validate_persisted_computation(
    validation_request: ComputationValidationRequest,
    validation_context: ComputationValidationContext,
    connection: psycopg.Connection | None = None,
) -> ComputationValidationResult:
    """Run deterministic validation against one persisted computation."""

    if connection is None:
        try:
            config = load_tax_core_persistence_config()
        except RuntimeError as error:
            raise ValidationError(
                reason="invalid_persistence_configuration",
                message=str(error),
                status_code=500,
            ) from error

        with psycopg.connect(config.database_url) as owned_connection:
            return _validate_with_connection(
                validation_request=validation_request,
                validation_context=validation_context,
                connection=owned_connection,
            )

    return _validate_with_connection(
        validation_request=validation_request,
        validation_context=validation_context,
        connection=connection,
    )


def _validate_with_connection(
    validation_request: ComputationValidationRequest,
    validation_context: ComputationValidationContext,
    connection: psycopg.Connection,
) -> ComputationValidationResult:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                persisted_source = load_persisted_validation_source(
                    cursor=cursor,
                    computation_id=validation_request.computation_id,
                )
                _enforce_validation_principal_lineage(
                    persisted_source=persisted_source,
                    principal_user_id=validation_context.user_id,
                )
                findings = _derive_validation_findings(persisted_source)
                validation_id = insert_validation_row(
                    cursor=cursor,
                    persisted_source=persisted_source,
                    context=validation_context,
                    validation_context=DEFAULT_VALIDATION_CONTEXT,
                    findings=findings,
                )
                return build_validation_result(
                    validation_id=validation_id,
                    persisted_source=persisted_source,
                    context=validation_context,
                    validation_context=DEFAULT_VALIDATION_CONTEXT,
                    findings=findings,
                )
    except ValidationError:
        raise
    except MaterializationError as error:
        if error.reason == "computation_not_found":
            raise ValidationError(
                reason="computation_not_found",
                message="Persisted computation was not found for validation.",
                status_code=404,
                details=error.details(),
            ) from error
        raise ValidationError(
            reason="validation_failed",
            message="Failed to persist computation-bound validation findings.",
            status_code=500,
            details=error.details(),
        ) from error
    except psycopg.Error as error:
        raise ValidationError(
            reason="validation_failed",
            message="Database error while persisting computation validation findings.",
            status_code=500,
            details={"db_error_type": error.__class__.__name__},
        ) from error


def _derive_validation_findings(
    persisted_source: PersistedValidationSource,
) -> list[ValidationFinding]:
    """Derive canonical deterministic findings from persisted computation context."""

    findings = [
        ValidationFinding(
            code="computation_lineage_bound",
            severity="info",
            message="Validation findings are bound to persisted deterministic computation lineage.",
            details={
                "tax_type": persisted_source.tax_type,
                "regime_type": persisted_source.regime_type,
                "tax_year": persisted_source.tax_year,
                "rule_version": persisted_source.rule_version,
                "input_hash": persisted_source.input_hash,
            },
        )
    ]
    if (
        persisted_source.tax_type == "health_contribution"
        and persisted_source.regime_type == "health_contribution"
    ):
        findings.extend(derive_health_contribution_validation_findings(persisted_source))
    else:
        findings.extend(derive_income_tax_validation_findings(persisted_source))
    return findings


def _enforce_validation_principal_lineage(
    persisted_source: PersistedValidationSource,
    principal_user_id: UUID,
) -> None:
    if persisted_source.user_id != principal_user_id:
        raise ValidationError(
            reason="computation_not_found",
            message="Persisted computation was not found for validation.",
            status_code=404,
            details={"computation_id": str(persisted_source.computation_id)},
        )
