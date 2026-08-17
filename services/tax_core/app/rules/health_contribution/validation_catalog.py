"""Implement governed validation checks for supported health-contribution lanes."""

from __future__ import annotations

from typing import cast
from typing import Literal
from decimal import Decimal
from dataclasses import dataclass
from collections.abc import Mapping

from services.tax_core.app.engine.execution_contract import ValidationFinding
from services.tax_core.app.engine.execution_contract import PersistedValidationSource

ZERO = Decimal("0.00")
_IMPLEMENTATION_READY = "implementation_ready"
_REPLAY_CONTEXT_KEY = "_kodi_replay_context"


@dataclass(frozen=True)
class _SupportedLaneSpec:
    lane_id: str
    historical_version_id: str
    tax_year: int
    rule_version: str
    effective_start: str
    effective_end: str
    resolved_regime_identifier: str
    regime_family: str
    resolved_domain_path: str


@dataclass(frozen=True)
class _ParsedPayload:
    version_identity: dict[str, object]
    contributor_outcome: dict[str, object]
    domain_outcomes: dict[str, object]
    contribution_summary: dict[str, object]
    traceability: dict[str, object]
    primary_effective_date: str


_SUPPORTED_LANES: tuple[_SupportedLaneSpec, ...] = (
    _SupportedLaneSpec(
        lane_id="nhif_legacy_20100716_a",
        historical_version_id="HCH-VER-20100716-A",
        tax_year=2012,
        rule_version="v1",
        effective_start="2010-07-16",
        effective_end="2014-12-07",
        resolved_regime_identifier="nhif_legacy",
        regime_family="nhif_legacy",
        resolved_domain_path="nhif_legacy",
    ),
    _SupportedLaneSpec(
        lane_id="nhif_legacy_20150401_a",
        historical_version_id="HCH-VER-20150401-A",
        tax_year=2019,
        rule_version="v1",
        effective_start="2015-04-01",
        effective_end="2021-03-29",
        resolved_regime_identifier="nhif_legacy",
        regime_family="nhif_legacy",
        resolved_domain_path="nhif_legacy",
    ),
    _SupportedLaneSpec(
        lane_id="nhif_legacy_20210528_a",
        historical_version_id="HCH-VER-20210528-A",
        tax_year=2022,
        rule_version="v1",
        effective_start="2021-05-28",
        effective_end="2022-12-30",
        resolved_regime_identifier="nhif_legacy",
        regime_family="nhif_legacy",
        resolved_domain_path="nhif_legacy",
    ),
    _SupportedLaneSpec(
        lane_id="nhif_legacy_20221231_reg",
        historical_version_id="HCH-VER-20221231-REG",
        tax_year=2023,
        rule_version="v1",
        effective_start="2022-12-31",
        effective_end="2023-11-21",
        resolved_regime_identifier="nhif_legacy",
        regime_family="nhif_legacy",
        resolved_domain_path="nhif_legacy",
    ),
    _SupportedLaneSpec(
        lane_id="sha_shif_salaried_20241001_a",
        historical_version_id="HCH-VER-20241001-A",
        tax_year=2024,
        rule_version="v1",
        effective_start="2024-10-01",
        effective_end="2025-02-27",
        resolved_regime_identifier="sha_shif",
        regime_family="sha_shif",
        resolved_domain_path="sha_shif_salaried",
    ),
    _SupportedLaneSpec(
        lane_id="sha_shif_non_salaried_20241001_a",
        historical_version_id="HCH-VER-20241001-A",
        tax_year=2024,
        rule_version="v1",
        effective_start="2024-10-01",
        effective_end="2025-02-27",
        resolved_regime_identifier="sha_shif",
        regime_family="sha_shif",
        resolved_domain_path="sha_shif_non_salaried",
    ),
    _SupportedLaneSpec(
        lane_id="sha_shif_salaried_20250228_pit",
        historical_version_id="HCH-VER-20250228-PIT",
        tax_year=2025,
        rule_version="v1",
        effective_start="2025-02-28",
        effective_end="open",
        resolved_regime_identifier="sha_shif",
        regime_family="sha_shif",
        resolved_domain_path="sha_shif_salaried",
    ),
    _SupportedLaneSpec(
        lane_id="sha_shif_non_salaried_20250228_pit",
        historical_version_id="HCH-VER-20250228-PIT",
        tax_year=2025,
        rule_version="v1",
        effective_start="2025-02-28",
        effective_end="open",
        resolved_regime_identifier="sha_shif",
        regime_family="sha_shif",
        resolved_domain_path="sha_shif_non_salaried",
    ),
)

_WINDOW_STATUS_BY_ID: dict[str, str] = {
    "HCH-VER-19990215-A": "governed_boundary_only",
    "HCH-VER-20031205-A": "partially_specified",
    "HCH-VER-20100716-A": _IMPLEMENTATION_READY,
    "HCH-VER-20141208-A": "governed_boundary_only",
    "HCH-VER-20150401-A": _IMPLEMENTATION_READY,
    "HCH-VER-20210330-A": "governed_boundary_only",
    "HCH-VER-20210528-A": _IMPLEMENTATION_READY,
    "HCH-VER-20221231-ACT": "governed_boundary_only",
    "HCH-VER-20221231-REG": _IMPLEMENTATION_READY,
    "HCH-VER-20231122-REPEAL": "governed_boundary_only",
    "HCH-VER-20231122-SHIACT": "governed_boundary_only",
    "HCH-VER-20240308-A": "governed_boundary_only",
    "HCH-VER-20240701-A": "governed_boundary_only",
    "HCH-VER-20240920-AMD": "governed_boundary_only",
    "HCH-VER-20240920-PIT": "governed_boundary_only",
    "HCH-VER-20241001-A": _IMPLEMENTATION_READY,
    "HCH-VER-20250228-AMD": "governed_boundary_only",
    "HCH-VER-20250228-PIT": _IMPLEMENTATION_READY,
}


def derive_health_contribution_validation_findings(
    persisted_source: PersistedValidationSource,
) -> list[ValidationFinding]:
    """Return deterministic health-contribution validation findings."""

    if (
        persisted_source.tax_type != "health_contribution"
        or persisted_source.regime_type != "health_contribution"
    ):
        return [_unsupported_scope_finding(persisted_source, "non_health_contribution_computation")]

    parse_result = _parse_supported_context(persisted_source)
    if isinstance(parse_result, ValidationFinding):
        return [parse_result]

    lane_spec, payload = parse_result
    findings = [
        _build_finding(
            code="health_contribution_supported_lane_detected",
            severity="info",
            domain_id="HCD-GOV-SCOPE",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Validation matched a supported governed health-contribution lane.",
            details={
                "request_regime_identifier": persisted_source.regime_identifier,
                "resolved_regime_identifier": lane_spec.resolved_regime_identifier,
                "resolved_domain_path": lane_spec.resolved_domain_path,
                "transition_route": persisted_source.regime_identifier == "transition_boundary",
            },
        )
    ]
    findings.append(_validate_version_binding(persisted_source, lane_spec, payload))
    findings.append(_validate_effective_window(persisted_source, lane_spec, payload))
    findings.append(_validate_contribution_summary(persisted_source, lane_spec, payload))
    return findings


def _parse_supported_context(
    persisted_source: PersistedValidationSource,
) -> tuple[_SupportedLaneSpec, _ParsedPayload] | ValidationFinding:
    payload = persisted_source.stored_result_payload
    try:
        version_identity = _require_mapping(payload, "version_identity")
        contributor_outcome = _require_mapping(payload, "contributor_outcome")
        domain_outcomes = _require_mapping(payload, "domain_outcomes")
        contribution_summary = _require_mapping(payload, "contribution_summary")
        traceability = _require_mapping(payload, "traceability")
        historical_version_id = _require_string(version_identity, "historical_version_id")
        resolved_regime_identifier = _require_string(version_identity, "regime_identifier")
        resolved_domain_path = _require_string(contributor_outcome, "resolved_domain_path")
        primary_effective_date = _extract_primary_effective_date(payload)
    except _PayloadShapeError as error:
        return _unsupported_scope_finding(
            persisted_source,
            "malformed_health_contribution_result",
            shape_error=str(error),
        )

    governed_status = _WINDOW_STATUS_BY_ID.get(historical_version_id)
    if governed_status is None:
        return _unsupported_scope_finding(
            persisted_source,
            "unknown_health_version_window",
            historical_version_id=historical_version_id,
            resolved_regime_identifier=resolved_regime_identifier,
            resolved_domain_path=resolved_domain_path,
        )
    if governed_status != _IMPLEMENTATION_READY:
        return ValidationFinding(
            code="health_contribution_version_window_unsupported",
            severity="error",
            message=("Health-contribution version identity claims a non-ready governed window."),
            details={
                "domain_id": "HCD-GOV-VERSION",
                "tax_type": persisted_source.tax_type,
                "regime_type": persisted_source.regime_type,
                "tax_year": persisted_source.tax_year,
                "rule_version": persisted_source.rule_version,
                "input_hash": persisted_source.input_hash,
                "historical_version_id": historical_version_id,
                "governed_window_status": governed_status,
                "request_regime_identifier": persisted_source.regime_identifier,
                "resolved_regime_identifier": resolved_regime_identifier,
                "resolved_domain_path": resolved_domain_path,
            },
        )

    lane_spec = _find_lane_spec(
        historical_version_id=historical_version_id,
        resolved_domain_path=resolved_domain_path,
    )
    if lane_spec is None:
        return _unsupported_scope_finding(
            persisted_source,
            "unsupported_governed_health_lane",
            historical_version_id=historical_version_id,
            resolved_regime_identifier=resolved_regime_identifier,
            resolved_domain_path=resolved_domain_path,
        )

    return (
        lane_spec,
        _ParsedPayload(
            version_identity=version_identity,
            contributor_outcome=contributor_outcome,
            domain_outcomes=domain_outcomes,
            contribution_summary=contribution_summary,
            traceability=traceability,
            primary_effective_date=primary_effective_date,
        ),
    )


def _validate_version_binding(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        tax_year = _require_int(payload.version_identity, "tax_year")
        rule_version = _require_string(payload.version_identity, "rule_version")
        historical_version_id = _require_string(payload.version_identity, "historical_version_id")
        resolved_regime_identifier = _require_string(payload.version_identity, "regime_identifier")
        regime_family = _require_string(payload.contributor_outcome, "regime_family")
        resolved_domain_path = _require_string(payload.contributor_outcome, "resolved_domain_path")
        classification_outcome = _require_string(
            payload.contributor_outcome,
            "classification_outcome",
        )
    except _PayloadShapeError as error:
        return _unsupported_scope_finding(
            persisted_source,
            "malformed_health_contribution_result",
            shape_error=str(error),
        )

    request_regime_identifier = persisted_source.regime_identifier
    request_binding_is_supported = request_regime_identifier in {
        lane_spec.resolved_regime_identifier,
        "transition_boundary",
    }
    is_consistent = (
        tax_year == persisted_source.tax_year
        and tax_year == lane_spec.tax_year
        and rule_version == persisted_source.rule_version
        and rule_version == lane_spec.rule_version
        and historical_version_id == lane_spec.historical_version_id
        and resolved_regime_identifier == lane_spec.resolved_regime_identifier
        and regime_family == lane_spec.regime_family
        and resolved_domain_path == lane_spec.resolved_domain_path
        and classification_outcome == "fully_classified"
        and request_binding_is_supported
    )

    if is_consistent:
        return _build_finding(
            code="health_contribution_version_binding_consistent",
            severity="info",
            domain_id="HCD-GOV-VERSION",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message=(
                "Health-contribution version identity and governed lane binding are "
                "internally consistent."
            ),
            details={
                "request_regime_identifier": request_regime_identifier,
                "resolved_regime_identifier": resolved_regime_identifier,
                "resolved_domain_path": resolved_domain_path,
            },
        )
    return _build_finding(
        code="health_contribution_version_binding_inconsistent",
        severity="error",
        domain_id="HCD-GOV-VERSION",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Health-contribution version identity does not match the persisted lane.",
        details={
            "expected_tax_year": lane_spec.tax_year,
            "actual_tax_year": tax_year,
            "expected_rule_version": lane_spec.rule_version,
            "actual_rule_version": rule_version,
            "expected_historical_version_id": lane_spec.historical_version_id,
            "actual_historical_version_id": historical_version_id,
            "expected_resolved_regime_identifier": lane_spec.resolved_regime_identifier,
            "actual_resolved_regime_identifier": resolved_regime_identifier,
            "expected_resolved_domain_path": lane_spec.resolved_domain_path,
            "actual_resolved_domain_path": resolved_domain_path,
            "request_regime_identifier": request_regime_identifier,
        },
    )


def _validate_effective_window(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        effective_start = _require_string(payload.version_identity, "effective_start")
        effective_end = _require_string(payload.version_identity, "effective_end")
        version_selection_basis = _require_string(
            payload.version_identity,
            "version_selection_basis",
        )
        primary_effective_date = payload.primary_effective_date
    except _PayloadShapeError as error:
        return _unsupported_scope_finding(
            persisted_source,
            "malformed_health_contribution_result",
            shape_error=str(error),
        )

    open_window = effective_end == "open" and primary_effective_date >= effective_start
    bounded_window = effective_start <= primary_effective_date <= effective_end
    is_consistent = (
        effective_start == lane_spec.effective_start
        and effective_end == lane_spec.effective_end
        and version_selection_basis != ""
        and (open_window or bounded_window)
    )

    if is_consistent:
        return _build_finding(
            code="health_contribution_effective_window_consistent",
            severity="info",
            domain_id="HCD-XCUT-VERSION-SELECTION",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Health-contribution effective-window identity is internally consistent.",
            details={
                "effective_start": effective_start,
                "effective_end": effective_end,
                "primary_effective_date": primary_effective_date,
                "version_selection_basis": version_selection_basis,
            },
        )
    return _build_finding(
        code="health_contribution_effective_window_inconsistent",
        severity="error",
        domain_id="HCD-XCUT-VERSION-SELECTION",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Health-contribution effective-window identity is inconsistent.",
        details={
            "expected_effective_start": lane_spec.effective_start,
            "actual_effective_start": effective_start,
            "expected_effective_end": lane_spec.effective_end,
            "actual_effective_end": effective_end,
            "primary_effective_date": primary_effective_date,
            "version_selection_basis": version_selection_basis,
        },
    )


def _validate_contribution_summary(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        summary_status = _require_string(payload.contribution_summary, "summary_status")
        coverage_status = _require_string(payload.contribution_summary, "coverage_status")
        regime_family = _require_string(payload.contribution_summary, "regime_family")
        contribution_basis = _require_money(payload.contribution_summary, "contribution_basis_kes")
        employee_contribution = _require_money(
            payload.contribution_summary,
            "employee_contribution_kes",
        )
        employer_contribution = _require_money(
            payload.contribution_summary,
            "employer_contribution_kes",
        )
        household_contribution = _require_money(
            payload.contribution_summary,
            "household_contribution_kes",
        )
        total_contribution = _require_money(
            payload.contribution_summary,
            "total_contribution_kes",
        )
        traceability_input_hash = _require_string(payload.traceability, "input_hash")
        active_domain = _require_mapping(payload.domain_outcomes, lane_spec.resolved_domain_path)
        active_domain_status = _require_string(active_domain, "status")
        domain_basis = _require_money(active_domain, "contribution_basis_kes")
        domain_employee = _require_money(active_domain, "employee_contribution_kes")
        domain_employer = _require_money(active_domain, "employer_contribution_kes")
        domain_household = _require_money(active_domain, "household_contribution_kes")
        domain_total = _require_money(active_domain, "total_contribution_kes")
    except _PayloadShapeError as error:
        return _unsupported_scope_finding(
            persisted_source,
            "malformed_health_contribution_result",
            shape_error=str(error),
        )

    computed_total = employee_contribution + employer_contribution + household_contribution
    is_consistent = (
        summary_status == "computed"
        and coverage_status == _IMPLEMENTATION_READY
        and regime_family == lane_spec.regime_family
        and traceability_input_hash == persisted_source.input_hash
        and active_domain_status == "computed"
        and contribution_basis == domain_basis
        and employee_contribution == domain_employee
        and employer_contribution == domain_employer
        and household_contribution == domain_household
        and total_contribution == domain_total
        and total_contribution == computed_total
    )

    if is_consistent:
        return _build_finding(
            code="health_contribution_summary_consistent",
            severity="info",
            domain_id="HCD-CORE-CONTRIBUTION-SUMMARY",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message=(
                "Health-contribution summary is internally consistent with the governed "
                "domain outcome."
            ),
            details={
                "coverage_status": coverage_status,
                "contribution_basis_kes": _format_decimal(contribution_basis),
                "total_contribution_kes": _format_decimal(total_contribution),
            },
        )
    return _build_finding(
        code="health_contribution_summary_inconsistent",
        severity="error",
        domain_id="HCD-CORE-CONTRIBUTION-SUMMARY",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Health-contribution summary is inconsistent with the governed domain outcome.",
        details={
            "expected_coverage_status": _IMPLEMENTATION_READY,
            "actual_coverage_status": coverage_status,
            "expected_regime_family": lane_spec.regime_family,
            "actual_regime_family": regime_family,
            "expected_total_contribution_kes": _format_decimal(domain_total),
            "actual_total_contribution_kes": _format_decimal(total_contribution),
            "computed_total_contribution_kes": _format_decimal(computed_total),
            "traceability_input_hash": traceability_input_hash,
        },
    )


def _build_finding(
    code: str,
    severity: Literal["info", "warning", "error"],
    domain_id: str,
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    message: str,
    details: dict[str, object] | None = None,
) -> ValidationFinding:
    finding_details: dict[str, object] = {
        "domain_id": domain_id,
        "lane_id": lane_spec.lane_id,
        "historical_version_id": lane_spec.historical_version_id,
        "tax_year": persisted_source.tax_year,
        "rule_version": persisted_source.rule_version,
        "input_hash": persisted_source.input_hash,
    }
    if details is not None:
        finding_details.update(details)
    return ValidationFinding(
        code=code,
        severity=severity,
        message=message,
        details=finding_details,
    )


def _unsupported_scope_finding(
    persisted_source: PersistedValidationSource,
    reason: str,
    historical_version_id: str | None = None,
    resolved_regime_identifier: str | None = None,
    resolved_domain_path: str | None = None,
    shape_error: str | None = None,
) -> ValidationFinding:
    details: dict[str, object] = {
        "domain_id": "HCD-GOV-SCOPE",
        "tax_type": persisted_source.tax_type,
        "regime_type": persisted_source.regime_type,
        "tax_year": persisted_source.tax_year,
        "rule_version": persisted_source.rule_version,
        "input_hash": persisted_source.input_hash,
        "reason": reason,
        "historical_version_id": historical_version_id,
        "resolved_regime_identifier": resolved_regime_identifier,
        "resolved_domain_path": resolved_domain_path,
        "request_regime_identifier": persisted_source.regime_identifier,
    }
    if shape_error is not None:
        details["shape_error"] = shape_error
    return ValidationFinding(
        code="health_contribution_validation_scope_unsupported",
        severity="error",
        message="Health-contribution validation catalog does not support this persisted lane.",
        details=details,
    )


def _find_lane_spec(
    historical_version_id: str,
    resolved_domain_path: str,
) -> _SupportedLaneSpec | None:
    for lane_spec in _SUPPORTED_LANES:
        if (
            lane_spec.historical_version_id == historical_version_id
            and lane_spec.resolved_domain_path == resolved_domain_path
        ):
            return lane_spec
    return None


class _PayloadShapeError(ValueError):
    pass


def _extract_primary_effective_date(payload: Mapping[str, object]) -> str:
    replay_context = _require_mapping(payload, _REPLAY_CONTEXT_KEY)
    normalized_input = _require_mapping(replay_context, "normalized_input")
    if "version_context" in normalized_input:
        version_context = _require_mapping(normalized_input, "version_context")
    else:
        input_payload = _require_mapping(normalized_input, "input_payload")
        version_context = _require_mapping(input_payload, "version_context")
    return _require_string(version_context, "primary_effective_date")


def _require_mapping(container: Mapping[str, object], key: str) -> dict[str, object]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise _PayloadShapeError(f"{key} must be a JSON object")
    return dict(cast(Mapping[str, object], value))


def _require_string(container: Mapping[str, object], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise _PayloadShapeError(f"{key} must be a non-empty string")
    return value


def _require_int(container: Mapping[str, object], key: str) -> int:
    value = container.get(key)
    if not isinstance(value, int):
        raise _PayloadShapeError(f"{key} must be an integer")
    return value


def _require_money(container: Mapping[str, object], key: str) -> Decimal:
    value = container.get(key)
    if not isinstance(value, str):
        raise _PayloadShapeError(f"{key} must be a money string")
    try:
        return Decimal(value)
    except Exception as error:  # pragma: no cover - Decimal is deterministic here
        raise _PayloadShapeError(f"{key} must be a money string") from error


def _format_decimal(value: Decimal) -> str:
    return format(value, ".2f")
