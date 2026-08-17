"""Map finalized governed health-contribution outputs into downstream form-ready structures."""

from __future__ import annotations

from typing import cast
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

FORM_TYPE = "health_contribution_summary"
FORM_VERSION = "health_contribution_vertical_slice_v1"

SUPPORTED_HEALTH_FORM_LANES: dict[tuple[str, str], str] = {
    ("HCH-VER-20100716-A", "nhif_legacy"): "health_contribution_nhif_legacy_v1_2010_07_16",
    ("HCH-VER-20150401-A", "nhif_legacy"): "health_contribution_nhif_legacy_v1_2015_04_01",
    ("HCH-VER-20210528-A", "nhif_legacy"): "health_contribution_nhif_legacy_v1_2021_05_28",
    (
        "HCH-VER-20221231-REG",
        "nhif_legacy",
    ): "health_contribution_nhif_legacy_v1_2022_12_31_reg",
    ("HCH-VER-20241001-A", "sha_shif"): "health_contribution_sha_shif_v1_2024_10_01",
    ("HCH-VER-20250228-PIT", "sha_shif"): "health_contribution_sha_shif_v1_2025_02_28_pit",
}


class HealthContributionFormMappingError(RuntimeError):
    """Represent deterministic form-mapping failures for governed health lanes."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured error details."""

        return {"reason": self.reason, **self._details}


def map_finalized_health_contribution_output_to_form_ready(
    finalized_output: Mapping[str, object],
) -> dict[str, object]:
    """Map one finalized governed health-contribution output into a form-ready structure."""

    source = _as_object(finalized_output, reason="invalid_mapping_input")
    tax_type = _require_string(source, "tax_type")
    regime_type = _require_string(source, "regime_type")
    tax_year = _require_int(source, "tax_year")
    rule_version = _require_string(source, "rule_version")
    input_hash = _require_string(source, "input_hash")
    computation_id = _require_string(source, "computation_id")
    finalization_status = _require_string(source, "finalization_status")
    finalized_at = _require_string(source, "finalized_at")
    finalized_audit_event_id = _require_string(source, "finalized_audit_event_id")

    if tax_type != "health_contribution":
        raise HealthContributionFormMappingError(
            reason="unsupported_tax_type",
            message="Form mapping supports governed health-contribution outputs only.",
            details={"tax_type": tax_type},
        )
    if regime_type != "health_contribution":
        raise HealthContributionFormMappingError(
            reason="unsupported_regime_type",
            message="Form mapping supports governed health-contribution regime outputs only.",
            details={"regime_type": regime_type},
        )
    if finalization_status != "finalized":
        raise HealthContributionFormMappingError(
            reason="computation_not_finalized",
            message="Form mapping requires a finalized computation output.",
            details={"finalization_status": finalization_status},
        )

    result_payload = _require_object(source, "result_payload")
    unsupported_or_unresolved = _require_list(result_payload, "unsupported_or_unresolved")
    if unsupported_or_unresolved:
        raise HealthContributionFormMappingError(
            reason="unsupported_result_scope",
            message="Form mapping does not accept unresolved or unsupported health outputs.",
            details={"unsupported_or_unresolved": unsupported_or_unresolved},
        )

    version_identity = _require_object(result_payload, "version_identity")
    contribution_summary = _require_object(result_payload, "contribution_summary")
    contributor_outcome = _require_object(result_payload, "contributor_outcome")
    domain_outcomes = _require_object(result_payload, "domain_outcomes")
    traceability = _require_object(result_payload, "traceability")

    historical_version_id = _require_string(version_identity, "historical_version_id")
    regime_family = _require_string(contributor_outcome, "regime_family")
    supported_lane_id = SUPPORTED_HEALTH_FORM_LANES.get((historical_version_id, regime_family))
    if supported_lane_id is None:
        raise HealthContributionFormMappingError(
            reason="unsupported_historical_version",
            message="Form mapping supports governed health implementation-ready windows only.",
            details={
                "historical_version_id": historical_version_id,
                "regime_family": regime_family,
            },
        )

    coverage_status = _require_string(contribution_summary, "coverage_status")
    if coverage_status != "implementation_ready":
        raise HealthContributionFormMappingError(
            reason="unsupported_result_scope",
            message="Form mapping requires an implementation-ready health contribution result.",
            details={"coverage_status": coverage_status},
        )
    summary_status = _require_string(contribution_summary, "summary_status")
    if summary_status != "computed":
        raise HealthContributionFormMappingError(
            reason="unsupported_result_scope",
            message="Form mapping requires a computed health contribution summary.",
            details={"summary_status": summary_status},
        )

    source_anchor_ids = _list_of_strings(version_identity, "source_anchor_ids")
    applied_policy_ids = _list_of_strings(traceability, "applied_policy_ids")
    validation_focus_domains = _list_of_strings(traceability, "validation_focus_domains")
    governing_change_ids = _list_of_strings(version_identity, "governing_change_ids")
    resolved_domain_path = _require_string(contributor_outcome, "resolved_domain_path")
    classification_outcome = _require_string(contributor_outcome, "classification_outcome")
    contributor_kind = _require_string(contributor_outcome, "contributor_kind")

    mapped_output: dict[str, object] = {
        "mapping_status": "ok",
        "form_type": FORM_TYPE,
        "form_version": FORM_VERSION,
        "supported_lane_id": supported_lane_id,
        "computation_identity": {
            "computation_id": computation_id,
            "tax_type": tax_type,
            "regime_type": regime_type,
            "tax_year": tax_year,
            "rule_version": rule_version,
            "input_hash": input_hash,
            "finalization_status": finalization_status,
            "finalized_at": finalized_at,
            "finalized_audit_event_id": finalized_audit_event_id,
        },
        "version_identity": {
            "historical_version_id": historical_version_id,
            "effective_start": _require_string(version_identity, "effective_start"),
            "effective_end": _optional_string(version_identity, "effective_end"),
            "version_selection_basis": _require_string(version_identity, "version_selection_basis"),
            "regime_identifier": _require_string(version_identity, "regime_identifier"),
            "source_anchor_ids": source_anchor_ids,
            "governing_change_ids": governing_change_ids,
        },
        "contributor": {
            "contributor_kind": contributor_kind,
            "regime_family": regime_family,
            "resolved_domain_path": resolved_domain_path,
            "classification_outcome": classification_outcome,
        },
        "contribution_fields": {
            "coverage_status": coverage_status,
            "summary_status": summary_status,
            "currency": _require_string(contribution_summary, "currency"),
            "contribution_basis_kes": _money_or_zero(
                contribution_summary.get("contribution_basis_kes")
            ),
            "employee_contribution_kes": _money_or_zero(
                contribution_summary.get("employee_contribution_kes")
            ),
            "employer_contribution_kes": _money_or_zero(
                contribution_summary.get("employer_contribution_kes")
            ),
            "household_contribution_kes": _money_or_zero(
                contribution_summary.get("household_contribution_kes")
            ),
            "total_contribution_kes": _money_or_zero(
                contribution_summary.get("total_contribution_kes")
            ),
        },
        "domain_fields": {
            "nhif_legacy": _build_domain_field_block(domain_outcomes, "nhif_legacy"),
            "sha_shif_salaried": _build_domain_field_block(domain_outcomes, "sha_shif_salaried"),
            "sha_shif_non_salaried": _build_domain_field_block(
                domain_outcomes,
                "sha_shif_non_salaried",
            ),
            "regime_selection": _build_domain_field_block(domain_outcomes, "regime_selection"),
            "version_selection": _build_domain_field_block(domain_outcomes, "version_selection"),
            "mixed_context_paths": _build_domain_field_block(
                domain_outcomes,
                "mixed_context_paths",
            ),
            "exemptions_and_special_cases": _build_domain_field_block(
                domain_outcomes,
                "exemptions_and_special_cases",
            ),
        },
        "lineage": {
            "source_anchor_ids": source_anchor_ids,
            "applied_policy_ids": applied_policy_ids,
            "validation_focus_domains": validation_focus_domains,
            "governing_change_ids": governing_change_ids,
            "replay_safe": _require_bool(traceability, "replay_safe"),
            "computation_status": _require_string(traceability, "computation_status"),
        },
        "unsupported_fields": [],
    }
    mapped_output["audit_evidence"] = _build_health_form_mapping_audit_evidence(mapped_output)
    return mapped_output


def _build_domain_field_block(
    domain_outcomes: Mapping[str, object],
    field_name: str,
) -> dict[str, object]:
    domain = _require_object(domain_outcomes, field_name)
    return {
        "status": _require_string(domain, "status"),
        "contribution_basis_kes": _money_or_zero(domain.get("contribution_basis_kes")),
        "employee_contribution_kes": _money_or_zero(domain.get("employee_contribution_kes")),
        "employer_contribution_kes": _money_or_zero(domain.get("employer_contribution_kes")),
        "household_contribution_kes": _money_or_zero(domain.get("household_contribution_kes")),
        "total_contribution_kes": _money_or_zero(domain.get("total_contribution_kes")),
        "decision_refs": _list_of_strings(domain, "decision_refs"),
    }


def _build_health_form_mapping_audit_evidence(
    mapped_output: Mapping[str, object],
) -> dict[str, object]:
    audit_seed = {
        "audit_kind": "health_contribution_form_mapping",
        "form_type": _require_string(mapped_output, "form_type"),
        "form_version": _require_string(mapped_output, "form_version"),
        "supported_lane_id": _require_string(mapped_output, "supported_lane_id"),
        "historical_version_id": _require_string(
            _require_object(mapped_output, "version_identity"),
            "historical_version_id",
        ),
        "input_hash": _require_string(
            _require_object(mapped_output, "computation_identity"),
            "input_hash",
        ),
    }
    return {
        "audit_evidence_id": hashlib.sha256(
            canonical_json_dumps(audit_seed).encode("utf-8")
        ).hexdigest(),
    }


def _money_or_zero(value: object) -> str:
    if value is None:
        return "0.00"
    if isinstance(value, str) and value.strip():
        return value
    raise HealthContributionFormMappingError(
        reason="invalid_money_field",
        message="Expected money field to be a non-empty string or null.",
    )


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HealthContributionFormMappingError(
            reason=reason,
            message="Expected JSON object input for deterministic form mapping.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise HealthContributionFormMappingError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise HealthContributionFormMappingError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _optional_string(source: Mapping[str, object], field_name: str) -> str | None:
    value = source.get(field_name)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise HealthContributionFormMappingError(
        reason="missing_required_field",
        message=f"Required string field '{field_name}' is missing.",
        details={"field_name": field_name},
    )


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise HealthContributionFormMappingError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_bool(source: Mapping[str, object], field_name: str) -> bool:
    value = source.get(field_name)
    if not isinstance(value, bool):
        raise HealthContributionFormMappingError(
            reason="missing_required_field",
            message=f"Required boolean field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_list(source: Mapping[str, object], field_name: str) -> list[object]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise HealthContributionFormMappingError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return cast(list[object], value)


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    values = _require_list(source, field_name)
    strings: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise HealthContributionFormMappingError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(value)
    return strings
