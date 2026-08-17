"""Define deterministic tax-core execution contract models and interfaces."""

from __future__ import annotations

from uuid import UUID
from typing import Literal
from typing import Protocol
from datetime import date
from datetime import datetime

from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import field_validator


class ComputationExecutionRequest(BaseModel):
    """Represent the canonical tax-core computation execution request."""

    model_config = ConfigDict(extra="forbid")

    tax_type: str
    regime_type: str
    regime_identifier: str | None = None
    tax_year: int = Field(ge=2000, le=2100)
    rule_version: str
    input_payload: dict[str, object]

    @field_validator("rule_version")
    @classmethod
    def validate_rule_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule_version must be non-empty.")
        return value


class PreparedExecutionInput(BaseModel):
    """Represent deterministic prepared input separated from rule execution."""

    model_config = ConfigDict(extra="forbid")

    tax_type: str
    regime_type: str
    regime_identifier: str | None = None
    tax_year: int
    rule_version: str
    primary_effective_date: date | None = None
    historical_version_id: str | None = None
    resident_status_assertion: str | None = None
    income_category_signature: str | None = None
    canonical_input_payload: dict[str, object]
    canonical_input_json: str
    input_hash: str


class RuleSelectionKey(BaseModel):
    """Represent deterministic key used for tax-rule binding."""

    model_config = ConfigDict(extra="forbid")

    tax_type: str
    regime_type: str
    regime_identifier: str | None = None
    tax_year: int
    rule_version: str
    primary_effective_date: date | None = None
    historical_version_id: str | None = None
    resident_status_assertion: str | None = None
    income_category_signature: str | None = None


class BoundRule(BaseModel):
    """Represent one deterministic resolved rule binding."""

    model_config = ConfigDict(extra="forbid")

    binding_id: str
    selection_key: RuleSelectionKey


class ComputationExecutionResult(BaseModel):
    """Represent the canonical deterministic computation result envelope."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    input_hash: str
    result_payload: dict[str, object]


class MaterializationContext(BaseModel):
    """Represent deterministic persistence context for execution materialization."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_at_time: str
    correlation_id: str
    idempotency_key: str
    session_id: UUID | None = None


class MaterializedComputationExecutionResult(BaseModel):
    """Represent persisted execution response with linked materialized records."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    computation_id: UUID
    computation_result_id: UUID
    audit_event_id: UUID
    idempotency_key: str
    correlation_id: str
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    input_hash: str
    result_payload: dict[str, object]


class ReplayVerificationRequest(BaseModel):
    """Represent deterministic replay verification request payload."""

    model_config = ConfigDict(extra="forbid")

    computation_id: UUID


class ReplayVerificationContext(BaseModel):
    """Represent deterministic replay context from request metadata."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_at_time: str
    correlation_id: str
    idempotency_key: str


class PersistedReplaySource(BaseModel):
    """Represent persisted governed fields required for deterministic replay."""

    model_config = ConfigDict(extra="forbid")

    computation_id: UUID
    user_id: UUID
    tax_type: str
    regime_type: str
    regime_identifier: str | None = None
    tax_year: int
    rule_version: str
    input_hash: str
    persisted_input_payload: dict[str, object]
    stored_result_payload: dict[str, object]


class ReplayVerificationResult(BaseModel):
    """Represent deterministic replay verification success response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    verification_status: Literal["matched"] = "matched"
    computation_id: UUID
    replay_audit_event_id: UUID
    correlation_id: str
    idempotency_key: str
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    input_hash: str


class ComputationFinalizationRequest(BaseModel):
    """Represent deterministic computation finalization request payload."""

    model_config = ConfigDict(extra="forbid")

    computation_id: UUID


class ComputationFinalizationContext(BaseModel):
    """Represent deterministic finalization context from request metadata."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_at_time: str
    correlation_id: str
    idempotency_key: str


class ComputationFinalizationResult(BaseModel):
    """Represent deterministic computation finalization response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    finalization_status: Literal["finalized"] = "finalized"
    computation_id: UUID
    finalized_at: datetime
    finalized_audit_event_id: UUID
    correlation_id: str
    idempotency_key: str


class ValidationFinding(BaseModel):
    """Represent one canonical structured validation finding."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    details: dict[str, object]


class ComputationValidationRequest(BaseModel):
    """Represent deterministic computation validation request payload."""

    model_config = ConfigDict(extra="forbid")

    computation_id: UUID


class ComputationValidationContext(BaseModel):
    """Represent deterministic validation context from request metadata."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_at_time: str
    correlation_id: str
    idempotency_key: str


class PersistedValidationSource(BaseModel):
    """Represent persisted computation lineage required for deterministic validation."""

    model_config = ConfigDict(extra="forbid")

    computation_id: UUID
    user_id: UUID
    tax_type: str
    regime_type: str
    regime_identifier: str | None = None
    tax_year: int
    rule_version: str
    input_hash: str
    stored_result_payload: dict[str, object]


class ComputationValidationResult(BaseModel):
    """Represent deterministic persisted validation response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    validation_id: UUID
    computation_id: UUID
    validation_context: str
    correlation_id: str
    idempotency_key: str
    tax_year: int
    rule_version: str
    findings: list[ValidationFinding]


class ComputationLifecycleAuditDetails(BaseModel):
    """Represent normalized computation lifecycle audit evidence payload."""

    model_config = ConfigDict(extra="forbid")

    lifecycle_stage: Literal["execution", "validation", "finalization", "replay"]
    computation_id: UUID
    correlation_id: str
    idempotency_key: str
    tax_year: int = Field(ge=2000, le=2100)
    rule_version: str
    input_hash: str
    outcome: Literal["succeeded", "finalized", "matched", "mismatch"]
    tax_type: str | None = None
    regime_type: str | None = None
    result_sha256: str | None = None
    validation_id: UUID | None = None
    validation_context: str | None = None
    finding_count: int | None = None
    finding_severities: list[Literal["info", "warning", "error"]] | None = None
    finalization_status: str | None = None
    verification_outcome: Literal["matched", "mismatch"] | None = None
    mismatch_reason: str | None = None
    stored_result_sha256: str | None = None
    replay_result_sha256: str | None = None


class RuleExecutor(Protocol):
    """Define callable boundary for deterministic rule execution."""

    def __call__(
        self,
        prepared_input: PreparedExecutionInput,
        bound_rule: BoundRule,
    ) -> dict[str, object]:
        """Execute deterministic rule logic for prepared input."""

        ...
