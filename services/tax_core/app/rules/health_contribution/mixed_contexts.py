"""Implement governed fail-closed mixed-context screening for health contribution."""

from __future__ import annotations

from typing import cast
from typing import NoReturn
from dataclasses import dataclass
from collections.abc import Mapping

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput

FAIL_CLOSED_MIXED_CONTEXT_BINDING_ID = "health_contribution_mixed_context_v1_fail_closed"


@dataclass(frozen=True)
class _RejectedMixedContextCandidate:
    candidate_id: str
    reason: str
    blocking_policy_ids: tuple[str, ...]
    description: str


_REJECTED_CANDIDATES = {
    "HC-MCTX-CMB-0001": _RejectedMixedContextCandidate(
        candidate_id="HC-MCTX-CMB-0001",
        reason="unsupported_mixed_context_hc_mctx_cmb_0001",
        blocking_policy_ids=("HCP-POL-304",),
        description="NHIF-to-SHA crossover mixed facts",
    ),
    "HC-MCTX-CMB-0002": _RejectedMixedContextCandidate(
        candidate_id="HC-MCTX-CMB-0002",
        reason="unsupported_mixed_context_hc_mctx_cmb_0002",
        blocking_policy_ids=("HCP-POL-008",),
        description="same-period SHA salaried-plus-non-salaried mixed facts",
    ),
    "HC-MCTX-CMB-0003": _RejectedMixedContextCandidate(
        candidate_id="HC-MCTX-CMB-0003",
        reason="unsupported_mixed_context_hc_mctx_cmb_0003",
        blocking_policy_ids=("HCP-POL-003", "HCP-POL-008"),
        description="employer/employee split-style mixed facts",
    ),
    "HC-MCTX-CMB-0004": _RejectedMixedContextCandidate(
        candidate_id="HC-MCTX-CMB-0004",
        reason="unsupported_mixed_context_hc_mctx_cmb_0004",
        blocking_policy_ids=("HCP-POL-U03",),
        description="exemption-dependent or special-case-dependent mixed facts",
    ),
}

_MIXED_CONTEXT_TYPES_FOR_CANDIDATE_0003 = {
    "registration_without_payment_window",
    "payment_without_classification_confirmation",
    "other_governed_mixed_context",
}


def execute_fail_closed_mixed_context_rule_pack(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> dict[str, object]:
    """Reject explicit mixed-context execution through the governed fail-closed path."""

    if bound_rule.binding_id != FAIL_CLOSED_MIXED_CONTEXT_BINDING_ID:
        _raise_rule_input_error(
            reason="invalid_mixed_context_binding",
            message="Mixed-context runtime module received an unexpected binding.",
            path="$.binding_id",
        )

    reject_governed_mixed_context_request(
        prepared_input,
        explicit_mixed_context=True,
    )
    raise AssertionError("Mixed-context screening must always fail closed in this pass.")


def reject_governed_mixed_context_request(
    prepared_input: PreparedExecutionInput,
    *,
    explicit_mixed_context: bool = False,
) -> None:
    """Reject governed mixed-context combinations deterministically."""

    payload = _expect_mapping(prepared_input.canonical_input_payload)
    classification = _classify_rejected_candidate(
        payload=payload,
        explicit_mixed_context=explicit_mixed_context,
    )
    if classification is None:
        if explicit_mixed_context:
            _raise_rule_input_error(
                reason="unsupported_mixed_context_request_shape",
                message=(
                    "Mixed-context requests must contain governed mixed-context facts that map "
                    "to the HC-MCTX verdict table."
                ),
                path="$.input_payload",
            )
        return

    candidate, path = classification
    blocking_policy_ids = ", ".join(candidate.blocking_policy_ids)
    _raise_rule_input_error(
        reason=candidate.reason,
        message=(
            f"Mixed-context combination {candidate.candidate_id} "
            f"({candidate.description}) is fail-closed in this pass. "
            f"Blocking policy IDs: {blocking_policy_ids}."
        ),
        path=path,
    )


def _classify_rejected_candidate(
    *,
    payload: dict[str, object],
    explicit_mixed_context: bool,
) -> tuple[_RejectedMixedContextCandidate, str] | None:
    mixed_context_types = _mixed_context_types(payload)
    has_special_case_items = _has_special_case_items(payload)
    lane_population = _lane_population(payload)
    has_multiple_lanes = len(lane_population) > 1
    has_mixed_context_items = bool(mixed_context_types)
    explicit_marker = explicit_mixed_context or _has_explicit_mixed_context_marker(payload)

    if (
        explicit_marker
        and not has_special_case_items
        and not has_mixed_context_items
        and not has_multiple_lanes
    ):
        return None

    if has_special_case_items and (
        explicit_marker or has_mixed_context_items or has_multiple_lanes
    ):
        return (
            _REJECTED_CANDIDATES["HC-MCTX-CMB-0004"],
            "$.input_payload.special_case_assertions.assertion_items",
        )

    if "legacy_and_active_overlap" in mixed_context_types or (
        "nhif_legacy" in lane_population
        and ("sha_shif_salaried" in lane_population or "sha_shif_non_salaried" in lane_population)
    ):
        return (
            _REJECTED_CANDIDATES["HC-MCTX-CMB-0001"],
            _path_for_mixed_context(
                payload=payload,
                fallback="$.input_payload",
            ),
        )

    if "salaried_and_non_salaried_overlap" in mixed_context_types or (
        "sha_shif_salaried" in lane_population and "sha_shif_non_salaried" in lane_population
    ):
        return (
            _REJECTED_CANDIDATES["HC-MCTX-CMB-0002"],
            _path_for_mixed_context(
                payload=payload,
                fallback="$.input_payload",
            ),
        )

    if mixed_context_types.intersection(_MIXED_CONTEXT_TYPES_FOR_CANDIDATE_0003) or (
        _looks_like_employer_employee_split(payload) and has_mixed_context_items
    ):
        return (
            _REJECTED_CANDIDATES["HC-MCTX-CMB-0003"],
            _path_for_mixed_context(
                payload=payload,
                fallback="$.input_payload.contributor_context",
            ),
        )

    if explicit_marker and has_mixed_context_items:
        return (
            _REJECTED_CANDIDATES["HC-MCTX-CMB-0003"],
            "$.input_payload.mixed_context_inputs.context_items",
        )

    return None


def _path_for_mixed_context(*, payload: dict[str, object], fallback: str) -> str:
    return (
        "$.input_payload.mixed_context_inputs.context_items"
        if _mixed_context_types(payload)
        else fallback
    )


def _lane_population(payload: dict[str, object]) -> set[str]:
    populated: set[str] = set()
    if _section_has_items(payload, "nhif_legacy_inputs"):
        populated.add("nhif_legacy")
    if _section_has_items(payload, "sha_shif_salaried_inputs"):
        populated.add("sha_shif_salaried")
    if _section_has_items(payload, "sha_shif_non_salaried_inputs"):
        populated.add("sha_shif_non_salaried")
    return populated


def _section_has_items(payload: dict[str, object], section_name: str) -> bool:
    section = _expect_mapping(
        payload.get(section_name),
        f"$.input_payload.{section_name}",
    )
    for value in section.values():
        if isinstance(value, list) and bool(cast(list[object], value)):
            return True
    return False


def _mixed_context_types(payload: dict[str, object]) -> set[str]:
    section = _expect_mapping(
        payload.get("mixed_context_inputs"),
        "$.input_payload.mixed_context_inputs",
    )
    items = section.get("context_items")
    if not isinstance(items, list):
        return set()

    mixed_context_types: set[str] = set()
    for index, item in enumerate(cast(list[object], items)):
        item_mapping = _expect_mapping(
            item,
            f"$.input_payload.mixed_context_inputs.context_items[{index}]",
        )
        mixed_context_type = item_mapping.get("mixed_context_type")
        if isinstance(mixed_context_type, str):
            mixed_context_types.add(mixed_context_type)
    return mixed_context_types


def _has_special_case_items(payload: dict[str, object]) -> bool:
    section = _expect_mapping(
        payload.get("special_case_assertions"),
        "$.input_payload.special_case_assertions",
    )
    items = section.get("assertion_items")
    return isinstance(items, list) and bool(cast(list[object], items))


def _has_explicit_mixed_context_marker(payload: dict[str, object]) -> bool:
    contributor_context = _expect_mapping(
        payload.get("contributor_context"),
        "$.input_payload.contributor_context",
    )
    asserted_domain_path = contributor_context.get("asserted_domain_path")
    contributor_kind = contributor_context.get("contributor_kind")
    return asserted_domain_path == "mixed_context" or contributor_kind == "mixed_context"


def _looks_like_employer_employee_split(payload: dict[str, object]) -> bool:
    contributor_context = _expect_mapping(
        payload.get("contributor_context"),
        "$.input_payload.contributor_context",
    )
    return (
        contributor_context.get("asserted_domain_path") in {"mixed_context", "undetermined"}
        and contributor_context.get("employer_reference_id") is not None
        and contributor_context.get("contribution_subject_reference_id") is not None
    )


def _expect_mapping(value: object, path: str = "$.input_payload") -> dict[str, object]:
    if not isinstance(value, Mapping):
        _raise_rule_input_error(
            reason="unsupported_mixed_context_request_shape",
            message="Mixed-context health input must contain governed object sections.",
            path=path,
        )
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _raise_rule_input_error(
    *,
    reason: str,
    message: str,
    path: str,
) -> NoReturn:
    raise InputHashError(reason=reason, message=message, path=path)
