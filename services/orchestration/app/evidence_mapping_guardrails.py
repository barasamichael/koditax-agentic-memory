"""Deterministic guardrails for extraction-to-tax evidence mapping scope."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from typing import NotRequired
from collections.abc import Mapping

from shared.validation.income_tax_capability_manifest import assert_supported_lane
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest

_SUPPORTED_TAX_DOMAIN = "income_tax"
_MIXED_LANE_ID = "resident_employment_plus_qualifying_interest_2023_07_01"
_ALLOWED_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "taxpayer_name",
        "taxpayer_pin",
        "resident_status_assertion",
        "document_tax_year",
        "employment",
        "qualifying_interest",
    }
)
_ALLOWED_EMPLOYMENT_FIELDS: frozenset[str] = frozenset(
    {
        "gross_employment_income_kes",
        "paye_withheld_kes",
        "employer_tax_pin",
    }
)
_ALLOWED_QUALIFYING_INTEREST_FIELDS: frozenset[str] = frozenset(
    {
        "gross_interest_income_kes",
        "withholding_applied_kes",
        "interest_classification",
    }
)


class EvidenceMappingRejectedContext(TypedDict):
    """Represent deterministic rejected context for mapping-scope guardrails."""

    lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    field_path: NotRequired[str]
    tax_domain: NotRequired[str]


class EvidenceMappingGuardDecision(TypedDict):
    """Represent deterministic allow decision for evidence mapping scope."""

    guard_status: str
    lane_id: str
    historical_version_id: str
    tax_year: int


class EvidenceMappingGuardrailError(RuntimeError):
    """Represent deterministic unsupported evidence mapping scope rejection."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
        rejected_context: EvidenceMappingRejectedContext,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.rejected_context = rejected_context

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic guardrail rejection payload."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "rejected_context": self.rejected_context,
        }


def enforce_income_tax_evidence_mapping_scope(
    *,
    projection: Mapping[str, object],
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
    tax_domain: str = _SUPPORTED_TAX_DOMAIN,
) -> EvidenceMappingGuardDecision:
    """Enforce deterministic supported evidence-mapping scope for pilot runtime."""

    _ensure_tax_domain_supported(
        tax_domain=tax_domain,
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    _ensure_lane_context_supported(
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    _ensure_projection_matches_lane(
        projection=projection,
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    _ensure_supported_field_scope(
        projection=projection,
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    return {
        "guard_status": "allowed",
        "lane_id": lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }


def _ensure_tax_domain_supported(
    *,
    tax_domain: str,
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    if tax_domain == _SUPPORTED_TAX_DOMAIN:
        return
    raise _guardrail_error(
        reason="unsupported_tax_domain_projection",
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        tax_domain=tax_domain,
    )


def _ensure_lane_context_supported(
    *,
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    manifest = load_income_tax_vertical_slice_manifest()
    try:
        assert_supported_lane(
            manifest,
            supported_lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
    except CapabilityManifestError as error:
        raise _guardrail_error(
            reason=error.reason,
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        ) from error


def _ensure_projection_matches_lane(
    *,
    projection: Mapping[str, object],
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    projected_lane = projection.get("supported_lane_id")
    if projected_lane is None:
        return
    if projected_lane == lane_id:
        return
    raise _guardrail_error(
        reason="projection_lane_mismatch",
        lane_id=lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        field_path="supported_lane_id",
    )


def _ensure_supported_field_scope(
    *,
    projection: Mapping[str, object],
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    mapped_fields = projection.get("mapped_evidence_fields")
    if not isinstance(mapped_fields, Mapping):
        raise _guardrail_error(
            reason="invalid_projection_fields",
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            field_path="mapped_evidence_fields",
        )
    mapped_evidence_fields = cast(Mapping[str, object], mapped_fields)

    for field_name in sorted(mapped_evidence_fields.keys()):
        if field_name not in _ALLOWED_TOP_LEVEL_FIELDS:
            raise _guardrail_error(
                reason="unsupported_field_mapping_scope",
                lane_id=lane_id,
                historical_version_id=historical_version_id,
                tax_year=tax_year,
                field_path=f"mapped_evidence_fields.{field_name}",
            )

    employment = mapped_evidence_fields.get("employment")
    if employment is not None:
        _ensure_nested_field_scope(
            value=employment,
            allowed_fields=_ALLOWED_EMPLOYMENT_FIELDS,
            path_prefix="mapped_evidence_fields.employment",
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    qualifying_interest = mapped_evidence_fields.get("qualifying_interest")
    if qualifying_interest is not None:
        if lane_id != _MIXED_LANE_ID:
            raise _guardrail_error(
                reason="unsupported_field_mapping_scope",
                lane_id=lane_id,
                historical_version_id=historical_version_id,
                tax_year=tax_year,
                field_path="mapped_evidence_fields.qualifying_interest",
            )
        _ensure_nested_field_scope(
            value=qualifying_interest,
            allowed_fields=_ALLOWED_QUALIFYING_INTEREST_FIELDS,
            path_prefix="mapped_evidence_fields.qualifying_interest",
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )


def _ensure_nested_field_scope(
    *,
    value: object,
    allowed_fields: frozenset[str],
    path_prefix: str,
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    if not isinstance(value, Mapping):
        raise _guardrail_error(
            reason="invalid_projection_fields",
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            field_path=path_prefix,
        )
    nested = cast(Mapping[str, object], value)
    for nested_key in sorted(nested.keys()):
        if nested_key in allowed_fields:
            continue
        raise _guardrail_error(
            reason="unsupported_field_mapping_scope",
            lane_id=lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            field_path=f"{path_prefix}.{nested_key}",
        )


def _guardrail_error(
    *,
    reason: str,
    lane_id: str,
    historical_version_id: str,
    tax_year: int,
    field_path: str | None = None,
    tax_domain: str | None = None,
) -> EvidenceMappingGuardrailError:
    rejected_context: EvidenceMappingRejectedContext = {
        "lane_id": lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }
    if field_path is not None:
        rejected_context["field_path"] = field_path
    if tax_domain is not None:
        rejected_context["tax_domain"] = tax_domain
    return EvidenceMappingGuardrailError(
        error_code="unsupported_evidence_mapping_scope",
        message=(
            "Evidence mapping scope is not supported by governed income-tax pilot capability."
        ),
        reason=reason,
        rejected_context=rejected_context,
    )
