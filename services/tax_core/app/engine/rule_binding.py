"""Resolve deterministic tax-core rule binding from explicit selection keys."""

from __future__ import annotations

from datetime import date
from dataclasses import dataclass

from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.rules.health_contribution.transition_boundary import (
    resolve_transition_selection,
)
from services.tax_core.app.rules.health_contribution.transition_boundary import (
    TransitionBoundaryBindingError,
)


@dataclass(frozen=True)
class _RuleBindingCandidate:
    """Represent one deterministic binding candidate."""

    binding_id: str
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    effective_start: date | None = None
    effective_end: date | None = None
    historical_version_id: str | None = None
    resident_status_assertion: str | None = None
    income_category_signature: str | None = None
    regime_identifier: str | None = None
    regime_identifier_required: bool = False


@dataclass(frozen=True)
class _RejectedHealthContributionWindow:
    """Represent one governed non-ready health-contribution window."""

    regime_identifier: str
    historical_version_id: str
    effective_start: date
    effective_end: date | None
    reason: str
    message: str


class RuleBindingError(ValueError):
    """Represent deterministic rule-binding failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        selection_key: RuleSelectionKey,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.selection_key = selection_key

    def details(self) -> dict[str, object]:
        """Build deterministic details payload for API errors."""

        return {
            "reason": self.reason,
            "selection_key": self.selection_key.model_dump(mode="json"),
        }


_REJECTED_HEALTH_CONTRIBUTION_WINDOWS: tuple[_RejectedHealthContributionWindow, ...] = (
    _RejectedHealthContributionWindow(
        regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-19990215-A",
        effective_start=date(1999, 2, 15),
        effective_end=date(2003, 12, 4),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-19990215-A remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20031205-A",
        effective_start=date(2003, 12, 5),
        effective_end=date(2010, 7, 15),
        reason="unsupported_partially_specified_window",
        message=(
            "HCH-VER-20031205-A remains partially_specified and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20141208-A",
        effective_start=date(2014, 12, 8),
        effective_end=date(2015, 3, 31),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20141208-A remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20210330-A",
        effective_start=date(2021, 3, 30),
        effective_end=date(2021, 5, 27),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20210330-A remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="nhif_legacy",
        historical_version_id="HCH-VER-20221231-ACT",
        effective_start=date(2022, 12, 31),
        effective_end=date(2023, 11, 21),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20221231-ACT remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20231122-SHIACT",
        effective_start=date(2023, 11, 22),
        effective_end=date(2024, 3, 7),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20231122-SHIACT remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20240308-A",
        effective_start=date(2024, 3, 8),
        effective_end=date(2024, 6, 30),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20240308-A remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20240701-A",
        effective_start=date(2024, 7, 1),
        effective_end=date(2024, 9, 19),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20240701-A remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20240920-AMD",
        effective_start=date(2024, 9, 20),
        effective_end=date(2024, 9, 30),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20240920-AMD remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20240920-PIT",
        effective_start=date(2024, 9, 20),
        effective_end=date(2024, 9, 30),
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20240920-PIT remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
    _RejectedHealthContributionWindow(
        regime_identifier="sha_shif",
        historical_version_id="HCH-VER-20250228-AMD",
        effective_start=date(2025, 2, 28),
        effective_end=None,
        reason="unsupported_governed_boundary_only_window",
        message=(
            "HCH-VER-20250228-AMD remains governed_boundary_only and is outside the "
            "implementation-ready health-contribution runtime set."
        ),
    ),
)


def _build_health_contribution_rule_bindings() -> tuple[_RuleBindingCandidate, ...]:
    windows = (
        (
            "health_contribution_nhif_legacy_v1_2010_07_16",
            "HCH-VER-20100716-A",
            date(2010, 7, 16),
            date(2014, 12, 7),
            "nhif_legacy",
        ),
        (
            "health_contribution_nhif_legacy_v1_2015_04_01",
            "HCH-VER-20150401-A",
            date(2015, 4, 1),
            date(2021, 3, 29),
            "nhif_legacy",
        ),
        (
            "health_contribution_nhif_legacy_v1_2021_05_28",
            "HCH-VER-20210528-A",
            date(2021, 5, 28),
            date(2022, 12, 30),
            "nhif_legacy",
        ),
        (
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
            "HCH-VER-20221231-REG",
            date(2022, 12, 31),
            date(2023, 11, 21),
            "nhif_legacy",
        ),
        (
            "health_contribution_sha_shif_v1_2024_10_01",
            "HCH-VER-20241001-A",
            date(2024, 10, 1),
            date(2025, 2, 27),
            "sha_shif",
        ),
        (
            "health_contribution_sha_shif_v1_2025_02_28_pit",
            "HCH-VER-20250228-PIT",
            date(2025, 2, 28),
            None,
            "sha_shif",
        ),
    )

    candidates: list[_RuleBindingCandidate] = []
    for (
        binding_id,
        historical_version_id,
        effective_start,
        effective_end,
        regime_identifier,
    ) in windows:
        final_tax_year = 2100 if effective_end is None else effective_end.year
        for tax_year in range(effective_start.year, final_tax_year + 1):
            candidates.append(
                _RuleBindingCandidate(
                    binding_id=binding_id,
                    tax_type="health_contribution",
                    regime_type="health_contribution",
                    tax_year=tax_year,
                    rule_version="v1",
                    effective_start=effective_start,
                    effective_end=effective_end,
                    historical_version_id=historical_version_id,
                    regime_identifier=regime_identifier,
                    regime_identifier_required=True,
                )
            )

    return tuple(candidates)


def _build_mixed_context_fail_closed_bindings() -> tuple[_RuleBindingCandidate, ...]:
    candidates: list[_RuleBindingCandidate] = []
    for tax_year in range(2000, 2101):
        candidates.append(
            _RuleBindingCandidate(
                binding_id="health_contribution_mixed_context_v1_fail_closed",
                tax_type="health_contribution",
                regime_type="health_contribution",
                tax_year=tax_year,
                rule_version="v1",
                regime_identifier="mixed_context",
                regime_identifier_required=True,
            )
        )
    return tuple(candidates)


_RULE_BINDINGS: tuple[_RuleBindingCandidate, ...] = (
    _RuleBindingCandidate(
        binding_id="income_tax_resident_employment_v1_2021_01_01",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2021,
        rule_version="v1",
        effective_start=date(2021, 1, 1),
        effective_end=date(2021, 6, 30),
        historical_version_id="KIT-VER-20210101-A",
        resident_status_assertion="resident",
        income_category_signature="employment",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_non_resident_employment_v1_2021_01_01",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2021,
        rule_version="v1",
        effective_start=date(2021, 1, 1),
        effective_end=date(2021, 6, 30),
        historical_version_id="KIT-VER-20210101-A",
        resident_status_assertion="non_resident",
        income_category_signature="employment",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_resident_employment_v1_2023_07_01",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2023,
        rule_version="v1",
        effective_start=date(2023, 7, 1),
        effective_end=date(2023, 8, 31),
        historical_version_id="KIT-VER-20230701-A",
        resident_status_assertion="resident",
        income_category_signature="employment",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_resident_employment_plus_qualifying_interest_v1_2023_07_01",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2023,
        rule_version="v1",
        effective_start=date(2023, 7, 1),
        effective_end=date(2023, 8, 31),
        historical_version_id="KIT-VER-20230701-A",
        resident_status_assertion="resident",
        income_category_signature="employment+investment",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_non_resident_employment_v1_2023_07_01",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2023,
        rule_version="v1",
        effective_start=date(2023, 7, 1),
        effective_end=date(2023, 8, 31),
        historical_version_id="KIT-VER-20230701-A",
        resident_status_assertion="non_resident",
        income_category_signature="employment",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_default_v1_2025",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2025,
        rule_version="v1",
    ),
    *_build_mixed_context_fail_closed_bindings(),
    *_build_health_contribution_rule_bindings(),
    # Intentionally duplicated binding key for deterministic ambiguity enforcement tests.
    _RuleBindingCandidate(
        binding_id="income_tax_ambiguous_a_2025",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2025,
        rule_version="v_ambiguous",
    ),
    _RuleBindingCandidate(
        binding_id="income_tax_ambiguous_b_2025",
        tax_type="income_tax",
        regime_type="income_tax",
        tax_year=2025,
        rule_version="v_ambiguous",
    ),
)


def bind_rule_selection(selection_key: RuleSelectionKey) -> BoundRule:
    """Resolve one deterministic bound rule for the selection key."""

    try:
        transition_resolution = resolve_transition_selection(selection_key)
    except TransitionBoundaryBindingError as error:
        raise RuleBindingError(
            reason=error.reason,
            message=error.message,
            selection_key=selection_key,
        ) from error
    if transition_resolution is not None:
        return BoundRule(
            binding_id=transition_resolution.binding_id,
            selection_key=selection_key,
        )

    base_matches = tuple(
        candidate for candidate in _RULE_BINDINGS if _matches_base(candidate, selection_key)
    )
    if not base_matches:
        rejected_window = _match_rejected_health_contribution_window(selection_key)
        if rejected_window is not None:
            raise RuleBindingError(
                reason=rejected_window.reason,
                message=rejected_window.message,
                selection_key=selection_key,
            )
        raise RuleBindingError(
            reason="unknown_rule_binding",
            message="No deterministic rule binding exists for the provided key.",
            selection_key=selection_key,
        )

    if any(candidate.regime_identifier_required for candidate in base_matches):
        if selection_key.regime_identifier is None:
            raise RuleBindingError(
                reason="missing_regime_identifier",
                message="regime_identifier is required for this deterministic binding key.",
                selection_key=selection_key,
            )

    matched_candidates = tuple(
        candidate
        for candidate in base_matches
        if _matches_regime_identifier_requirement(candidate, selection_key)
    )
    if not matched_candidates:
        rejected_window = _match_rejected_health_contribution_window(selection_key)
        if rejected_window is not None:
            raise RuleBindingError(
                reason=rejected_window.reason,
                message=rejected_window.message,
                selection_key=selection_key,
            )
        raise RuleBindingError(
            reason="unknown_rule_binding",
            message="No deterministic rule binding exists for the provided key.",
            selection_key=selection_key,
        )

    if len(matched_candidates) > 1:
        raise RuleBindingError(
            reason="ambiguous_rule_binding",
            message="Deterministic rule binding is ambiguous for the provided key.",
            selection_key=selection_key,
        )

    resolved_candidate = next(iter(matched_candidates))
    return BoundRule(binding_id=resolved_candidate.binding_id, selection_key=selection_key)


def _matches_base(candidate: _RuleBindingCandidate, selection_key: RuleSelectionKey) -> bool:
    return (
        candidate.tax_type == selection_key.tax_type
        and candidate.regime_type == selection_key.regime_type
        and candidate.tax_year == selection_key.tax_year
        and candidate.rule_version == selection_key.rule_version
    )


def _matches_regime_identifier_requirement(
    candidate: _RuleBindingCandidate,
    selection_key: RuleSelectionKey,
) -> bool:
    if candidate.effective_start is not None:
        effective_date = selection_key.primary_effective_date
        if effective_date is None:
            return False
        if effective_date < candidate.effective_start:
            return False
        if candidate.effective_end is not None and effective_date > candidate.effective_end:
            return False

    if (
        candidate.historical_version_id is not None
        and selection_key.historical_version_id is not None
        and selection_key.historical_version_id != candidate.historical_version_id
    ):
        return False

    if (
        candidate.resident_status_assertion is not None
        and selection_key.resident_status_assertion != candidate.resident_status_assertion
    ):
        return False

    if (
        candidate.income_category_signature is not None
        and selection_key.income_category_signature != candidate.income_category_signature
    ):
        return False

    if candidate.regime_identifier is not None:
        return selection_key.regime_identifier == candidate.regime_identifier
    if candidate.regime_identifier_required:
        return selection_key.regime_identifier is not None
    return selection_key.regime_identifier is None


def _match_rejected_health_contribution_window(
    selection_key: RuleSelectionKey,
) -> _RejectedHealthContributionWindow | None:
    if selection_key.tax_type != "health_contribution":
        return None
    if selection_key.regime_type != "health_contribution":
        return None
    if selection_key.regime_identifier not in {"nhif_legacy", "sha_shif"}:
        return None

    requested_version_id = selection_key.historical_version_id
    if requested_version_id is not None:
        for window in _REJECTED_HEALTH_CONTRIBUTION_WINDOWS:
            if window.regime_identifier != selection_key.regime_identifier:
                continue
            if window.historical_version_id != requested_version_id:
                continue
            if selection_key.primary_effective_date is None or _date_within_window(
                selection_key.primary_effective_date,
                window,
            ):
                return window

    if selection_key.primary_effective_date is None:
        return None

    for window in _REJECTED_HEALTH_CONTRIBUTION_WINDOWS:
        if window.regime_identifier != selection_key.regime_identifier:
            continue
        if _date_within_window(selection_key.primary_effective_date, window):
            return window
    return None


def _date_within_window(
    effective_date: date,
    window: _RejectedHealthContributionWindow,
) -> bool:
    if effective_date < window.effective_start:
        return False
    if window.effective_end is not None and effective_date > window.effective_end:
        return False
    return True
