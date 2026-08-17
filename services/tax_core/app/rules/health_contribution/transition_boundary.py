"""Resolve governed NHIF to SHA/SHIF transition-boundary selection."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from datetime import date
from dataclasses import dataclass
from collections.abc import Mapping

from shared.determinism.input_hash import InputHashError
from shared.determinism.input_hash import canonical_json_dumps
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput

TRANSITION_MODE_IDENTIFIERS = frozenset({"transition_boundary", "undetermined"})


@dataclass(frozen=True)
class TransitionBoundaryBindingError(ValueError):
    """Represent deterministic transition-boundary binding failures."""

    reason: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class TransitionBoundaryResolution:
    """Represent one governed transition-boundary resolution."""

    binding_id: str
    resolved_regime_identifier: str
    historical_version_id: str
    effective_start: date
    effective_end: date | None
    governing_change_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]


@dataclass(frozen=True)
class _WindowResolution:
    binding_id: str
    resolved_regime_identifier: str
    historical_version_id: str
    effective_start: date
    effective_end: date | None
    governing_change_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RejectedWindow:
    historical_version_id: str
    start: date
    end: date | None
    reason: str
    message: str


IMPLEMENTATION_READY_WINDOWS: tuple[_WindowResolution, ...] = (
    _WindowResolution(
        binding_id="health_contribution_nhif_legacy_v1_2010_07_16",
        resolved_regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20100716-A",
        effective_start=date(2010, 7, 16),
        effective_end=date(2014, 12, 7),
        governing_change_ids=("HC-CHG-2010-07-16-A",),
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2010-07-16",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-101", "HCP-POL-301"),
    ),
    _WindowResolution(
        binding_id="health_contribution_nhif_legacy_v1_2015_04_01",
        resolved_regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20150401-A",
        effective_start=date(2015, 4, 1),
        effective_end=date(2021, 3, 29),
        governing_change_ids=("HC-CHG-2015-04-01-A",),
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2015-04-01",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-101", "HCP-POL-301"),
    ),
    _WindowResolution(
        binding_id="health_contribution_nhif_legacy_v1_2021_05_28",
        resolved_regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20210528-A",
        effective_start=date(2021, 5, 28),
        effective_end=date(2022, 12, 30),
        governing_change_ids=("HC-CHG-2021-05-28-A",),
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2021-05-28",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-101", "HCP-POL-301"),
    ),
    _WindowResolution(
        binding_id="health_contribution_nhif_legacy_v1_2022_12_31_reg",
        resolved_regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20221231-REG",
        effective_start=date(2022, 12, 31),
        effective_end=date(2023, 11, 21),
        governing_change_ids=("HC-CHG-2022-12-31-B",),
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2022-12-31",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-101", "HCP-POL-301"),
    ),
    _WindowResolution(
        binding_id="health_contribution_sha_shif_v1_2024_10_01",
        resolved_regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20241001-A",
        effective_start=date(2024, 10, 1),
        effective_end=date(2025, 2, 27),
        governing_change_ids=("HC-CHG-2024-10-01-A",),
        source_anchor_ids=("HC-SHI-REG-2024-09-20",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-201", "HCP-POL-302"),
    ),
    _WindowResolution(
        binding_id="health_contribution_sha_shif_v1_2025_02_28_pit",
        resolved_regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20250228-PIT",
        effective_start=date(2025, 2, 28),
        effective_end=None,
        governing_change_ids=("HC-CHG-2025-02-28-B",),
        source_anchor_ids=("HC-SHI-REG-2025-02-28",),
        applied_policy_ids=("HCP-POL-001", "HCP-POL-002", "HCP-POL-201", "HCP-POL-302"),
    ),
)

UNRESOLVED_TRANSITION_WINDOWS: tuple[_RejectedWindow, ...] = (
    _RejectedWindow(
        historical_version_id="HCH-VER-20231122-REPEAL",
        start=date(2023, 11, 22),
        end=date(2023, 11, 22),
        reason="unresolved_transition_window",
        message=(
            "The NHIF repeal boundary on 2023-11-22 remains a transition-only anchor and "
            "cannot be normalized into a computation-safe regime selection."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20231122-SHIACT",
        start=date(2023, 11, 22),
        end=date(2024, 3, 7),
        reason="unresolved_transition_window",
        message=(
            "The SHA/SHIF commencement and staged pre-payment windows remain transition-only "
            "and are not implementation-ready for deterministic contribution execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20240308-A",
        start=date(2024, 3, 8),
        end=date(2024, 6, 30),
        reason="unresolved_transition_window",
        message=(
            "The SHA/SHIF regulation-commenced window remains transition-only until the "
            "live payment boundary on 2024-10-01."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20240701-A",
        start=date(2024, 7, 1),
        end=date(2024, 9, 19),
        reason="unresolved_transition_window",
        message=(
            "The SHA/SHIF registration-start window remains transition-only until the "
            "live payment boundary on 2024-10-01."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20240920-AMD",
        start=date(2024, 9, 20),
        end=date(2024, 9, 30),
        reason="unresolved_transition_window",
        message=(
            "The 2024-09-20 SHA amendment-layer boundary remains unresolved for direct "
            "transition normalization and must fail closed."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20240920-PIT",
        start=date(2024, 9, 20),
        end=date(2024, 9, 30),
        reason="unresolved_transition_window",
        message=(
            "The 2024-09-20 to 2024-09-30 SHA point-in-time state is pre-payment only and "
            "cannot be selected as a live contribution regime."
        ),
    ),
)

UNSUPPORTED_NON_READY_WINDOWS: tuple[_RejectedWindow, ...] = (
    _RejectedWindow(
        historical_version_id="HCH-VER-19990215-A",
        start=date(1999, 2, 15),
        end=date(2003, 12, 4),
        reason="unsupported_transition_window",
        message=(
            "The pre-2003 NHIF window is not implementation-ready and remains outside "
            "supported transition-boundary execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20031205-A",
        start=date(2003, 12, 5),
        end=date(2010, 7, 15),
        reason="unsupported_transition_window",
        message=(
            "The original NHIF regulatory baseline remains partially specified and is not "
            "available for transition-boundary execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20141208-A",
        start=date(2014, 12, 8),
        end=date(2015, 3, 31),
        reason="unsupported_transition_window",
        message=(
            "The 2014 NHIF boundary-only act state is not implementation-ready for "
            "transition-boundary execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20210330-A",
        start=date(2021, 3, 30),
        end=date(2021, 5, 27),
        reason="unsupported_transition_window",
        message=(
            "The 2021 NHIF boundary-only act state is not implementation-ready for "
            "transition-boundary execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20221231-ACT",
        start=date(2022, 12, 31),
        end=date(2023, 11, 21),
        reason="unsupported_transition_window",
        message=(
            "The 2022 NHIF boundary-only act state is not implementation-ready for "
            "transition-boundary execution."
        ),
    ),
    _RejectedWindow(
        historical_version_id="HCH-VER-20250228-AMD",
        start=date(2025, 2, 28),
        end=None,
        reason="unsupported_transition_window",
        message=(
            "The 2025 SHA amendment-layer boundary remains governed-boundary-only and "
            "is not implementation-ready for transition-boundary execution."
        ),
    ),
)


def resolve_transition_selection(
    selection_key: RuleSelectionKey,
) -> TransitionBoundaryResolution | None:
    """Resolve transition-boundary selection into exactly one implementation-ready window."""

    if not _is_transition_mode(selection_key.regime_identifier):
        return None
    if selection_key.tax_type != "health_contribution":
        return None
    if selection_key.regime_type != "health_contribution":
        return None
    if selection_key.primary_effective_date is None:
        raise TransitionBoundaryBindingError(
            reason="missing_primary_effective_date",
            message=(
                "transition-boundary health-contribution selection requires primary_effective_date."
            ),
        )

    effective_date = selection_key.primary_effective_date
    requested_version_id = selection_key.historical_version_id
    if requested_version_id is not None:
        explicit_ready_match = _match_ready_window_by_version_id(requested_version_id)
        if explicit_ready_match is not None:
            if not _date_within_window(effective_date, explicit_ready_match):
                raise TransitionBoundaryBindingError(
                    reason="ambiguous_transition_regime_selection",
                    message=(
                        "historical_version_id does not align with the provided "
                        "primary_effective_date for transition-boundary resolution."
                    ),
                )
            _validate_tax_year(selection_key=selection_key, window=explicit_ready_match)
            return _build_resolution(explicit_ready_match)

        explicit_rejected_match = _match_rejected_window_by_version_id(requested_version_id)
        if explicit_rejected_match is not None:
            raise TransitionBoundaryBindingError(
                reason=explicit_rejected_match.reason,
                message=explicit_rejected_match.message,
            )

        raise TransitionBoundaryBindingError(
            reason="unsupported_transition_window",
            message="historical_version_id is not a governed transition-boundary execution window.",
        )

    ready_matches = [
        window
        for window in IMPLEMENTATION_READY_WINDOWS
        if _date_within_window(effective_date, window)
    ]
    if len(ready_matches) == 1:
        _validate_tax_year(selection_key=selection_key, window=ready_matches[0])
        return _build_resolution(ready_matches[0])
    if len(ready_matches) > 1:
        raise TransitionBoundaryBindingError(
            reason="ambiguous_transition_regime_selection",
            message=(
                "primary_effective_date maps to more than one implementation-ready "
                "transition window and must be disambiguated by historical_version_id."
            ),
        )

    rejected_window = _match_rejected_window_by_date(effective_date)
    if rejected_window is not None:
        raise TransitionBoundaryBindingError(
            reason=rejected_window.reason,
            message=rejected_window.message,
        )

    raise TransitionBoundaryBindingError(
        reason="unsupported_transition_window",
        message=(
            "primary_effective_date is outside the governed implementation-ready windows "
            "for transition-boundary execution."
        ),
    )


def normalize_transition_prepared_input(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> PreparedExecutionInput:
    """Adapt a transition-boundary request into the exact supported regime lane."""

    if not _is_transition_mode(prepared_input.regime_identifier):
        return prepared_input

    resolution = _resolution_from_binding_id(bound_rule.binding_id)
    if resolution is None:
        raise InputHashError(
            reason="unsupported_transition_window",
            message="Transition-boundary request did not resolve to a supported governed window.",
            path="$.regime_identifier",
        )

    canonical_payload = _expect_mapping(prepared_input.canonical_input_payload)
    normalized_payload = deepcopy(canonical_payload)
    resolved_domain_path = _infer_transition_domain_path(normalized_payload)
    _validate_domain_alignment(
        resolved_domain_path=resolved_domain_path,
        resolution=resolution,
    )

    contributor_context = _expect_mapping(
        normalized_payload.get("contributor_context"),
        "$.input_payload.contributor_context",
    )
    contributor_context["asserted_domain_path"] = resolved_domain_path

    version_context = _expect_mapping(
        normalized_payload.get("version_context"),
        "$.input_payload.version_context",
    )
    version_context["historical_version_id"] = resolution.historical_version_id
    version_context["governing_change_ids"] = list(resolution.governing_change_ids)
    version_context["source_anchor_ids"] = list(resolution.source_anchor_ids)

    return prepared_input.model_copy(
        update={
            "regime_identifier": resolution.resolved_regime_identifier,
            "historical_version_id": resolution.historical_version_id,
            "canonical_input_payload": normalized_payload,
            "canonical_input_json": canonical_json_dumps(normalized_payload),
        }
    )


def _build_resolution(window: _WindowResolution) -> TransitionBoundaryResolution:
    return TransitionBoundaryResolution(
        binding_id=window.binding_id,
        resolved_regime_identifier=window.resolved_regime_identifier,
        historical_version_id=window.historical_version_id,
        effective_start=window.effective_start,
        effective_end=window.effective_end,
        governing_change_ids=window.governing_change_ids,
        source_anchor_ids=window.source_anchor_ids,
        applied_policy_ids=window.applied_policy_ids,
    )


def _resolution_from_binding_id(binding_id: str) -> TransitionBoundaryResolution | None:
    for window in IMPLEMENTATION_READY_WINDOWS:
        if window.binding_id == binding_id:
            return _build_resolution(window)
    return None


def _match_ready_window_by_version_id(version_id: str) -> _WindowResolution | None:
    for window in IMPLEMENTATION_READY_WINDOWS:
        if window.historical_version_id == version_id:
            return window
    return None


def _match_rejected_window_by_version_id(version_id: str) -> _RejectedWindow | None:
    for window in (*UNRESOLVED_TRANSITION_WINDOWS, *UNSUPPORTED_NON_READY_WINDOWS):
        if window.historical_version_id == version_id:
            return window
    return None


def _match_rejected_window_by_date(effective_date: date) -> _RejectedWindow | None:
    for window in (*UNRESOLVED_TRANSITION_WINDOWS, *UNSUPPORTED_NON_READY_WINDOWS):
        if _date_within_window(effective_date, window):
            return window
    return None


def _validate_tax_year(
    *,
    selection_key: RuleSelectionKey,
    window: _WindowResolution,
) -> None:
    final_year = 2100 if window.effective_end is None else window.effective_end.year
    if selection_key.tax_year < window.effective_start.year or selection_key.tax_year > final_year:
        raise TransitionBoundaryBindingError(
            reason="unsupported_transition_window",
            message=(
                "tax_year falls outside the governed year range for the resolved "
                "transition-boundary window."
            ),
        )


def _expect_mapping(value: object, path: str = "$.input_payload") -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InputHashError(
            reason="unsupported_transition_request_shape",
            message="transition-boundary health input must contain governed object sections.",
            path=path,
        )
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _infer_transition_domain_path(payload: dict[str, object]) -> str:
    if _has_special_case_items(payload):
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message="Special-case assertions are outside the governed transition-boundary lane.",
            path="$.input_payload.special_case_assertions.assertion_items",
        )
    if _has_mixed_context_items(payload):
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message="Mixed-context inputs are outside the governed transition-boundary lane.",
            path="$.input_payload.mixed_context_inputs.context_items",
        )

    candidate_paths: list[str] = []
    if _section_has_items(payload, "nhif_legacy_inputs"):
        candidate_paths.append("nhif_legacy")
    if _section_has_items(payload, "sha_shif_salaried_inputs"):
        candidate_paths.append("sha_shif_salaried")
    if _section_has_items(payload, "sha_shif_non_salaried_inputs"):
        candidate_paths.append("sha_shif_non_salaried")

    if len(candidate_paths) != 1:
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message=(
                "Transition-boundary execution requires exactly one governed input lane "
                "to be populated."
            ),
            path="$.input_payload",
        )

    contributor_context = _expect_mapping(
        payload.get("contributor_context"),
        "$.input_payload.contributor_context",
    )
    asserted_domain_path = contributor_context.get("asserted_domain_path")
    if asserted_domain_path not in {
        "transition_boundary",
        "undetermined",
        candidate_paths[0],
    }:
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message=(
                "asserted_domain_path does not align with the populated governed lane "
                "for transition-boundary execution."
            ),
            path="$.input_payload.contributor_context.asserted_domain_path",
        )

    return candidate_paths[0]


def _validate_domain_alignment(
    *,
    resolved_domain_path: str,
    resolution: TransitionBoundaryResolution,
) -> None:
    if (
        resolution.resolved_regime_identifier == "nhif_legacy"
        and resolved_domain_path != "nhif_legacy"
    ):
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message=(
                "The resolved transition-boundary date selects an NHIF legacy window, but "
                "the request payload does not carry an NHIF legacy governed lane."
            ),
            path="$.input_payload",
        )
    if resolution.resolved_regime_identifier == "sha_shif" and resolved_domain_path not in {
        "sha_shif_salaried",
        "sha_shif_non_salaried",
    }:
        raise InputHashError(
            reason="ambiguous_transition_regime_selection",
            message=(
                "The resolved transition-boundary date selects a SHA/SHIF window, but "
                "the request payload does not carry a governed SHA/SHIF lane."
            ),
            path="$.input_payload",
        )


def _has_special_case_items(payload: dict[str, object]) -> bool:
    section = _expect_mapping(
        payload.get("special_case_assertions"),
        "$.input_payload.special_case_assertions",
    )
    items = section.get("assertion_items")
    return isinstance(items, list) and bool(cast(list[object], items))


def _has_mixed_context_items(payload: dict[str, object]) -> bool:
    section = _expect_mapping(
        payload.get("mixed_context_inputs"),
        "$.input_payload.mixed_context_inputs",
    )
    items = section.get("context_items")
    return isinstance(items, list) and bool(cast(list[object], items))


def _section_has_items(payload: dict[str, object], section_name: str) -> bool:
    section = _expect_mapping(
        payload.get(section_name),
        f"$.input_payload.{section_name}",
    )
    for value in section.values():
        if isinstance(value, list) and bool(cast(list[object], value)):
            return True
    return False


def _date_within_window(
    effective_date: date,
    window: _WindowResolution | _RejectedWindow,
) -> bool:
    start = window.start if isinstance(window, _RejectedWindow) else window.effective_start
    if effective_date < start:
        return False
    end = window.end if isinstance(window, _RejectedWindow) else window.effective_end
    if end is not None and effective_date > end:
        return False
    return True


def _is_transition_mode(regime_identifier: str | None) -> bool:
    return regime_identifier in TRANSITION_MODE_IDENTIFIERS
