"""Implement deterministic tax-core input preparation and execution flow."""

from __future__ import annotations

from typing import cast
from datetime import date
from collections.abc import Mapping

from shared.determinism.input_hash import InputHashError
from shared.determinism.input_hash import canonical_json_dumps
from shared.determinism.input_hash import canonicalize_for_hash
from shared.determinism.input_hash import compute_computation_input_hash
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import RuleExecutor
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.rules.health_contribution.mixed_contexts import (
    execute_fail_closed_mixed_context_rule_pack,
)
from services.tax_core.app.rules.income_tax.mixed_income_computation import (
    execute_resident_employment_plus_qualifying_interest_rule_pack,
)
from services.tax_core.app.rules.health_contribution.sha_shif_rule_pack import (
    execute_sha_shif_rule_pack,
)
from services.tax_core.app.rules.health_contribution.transition_boundary import (
    normalize_transition_prepared_input,
)
from services.tax_core.app.rules.income_tax.resident_employment_rule_pack import (
    execute_resident_employment_rule_pack,
)
from services.tax_core.app.rules.health_contribution.nhif_legacy_rule_pack import (
    execute_nhif_legacy_rule_pack,
)
from services.tax_core.app.rules.income_tax.non_resident_employment_rule_pack import (
    execute_non_resident_employment_rule_pack,
)
from services.tax_core.app.rules.income_tax.resident_employment_rule_pack_2021 import (
    execute_resident_employment_2021_rule_pack,
)
from services.tax_core.app.rules.income_tax.non_resident_employment_rule_pack_2021 import (
    execute_non_resident_employment_2021_rule_pack,
)


def prepare_execution_input(request: ComputationExecutionRequest) -> PreparedExecutionInput:
    """Prepare canonical deterministic input for downstream rule execution."""

    canonical_payload_value = canonicalize_for_hash(request.input_payload)
    canonical_input_payload = _expect_mapping(canonical_payload_value)
    canonical_input_json = canonical_json_dumps(canonical_input_payload)
    input_hash = compute_computation_input_hash(
        tax_type=request.tax_type,
        regime_type=request.regime_type,
        regime_identifier=request.regime_identifier,
        tax_year=request.tax_year,
        rule_version=request.rule_version,
        input_payload=canonical_input_payload,
    ).sha256_hex
    primary_effective_date = _extract_primary_effective_date(canonical_input_payload)
    historical_version_id = _extract_historical_version_id(canonical_input_payload)
    resident_status_assertion = _extract_resident_status_assertion(canonical_input_payload)
    income_category_signature = _extract_income_category_signature(canonical_input_payload)
    return PreparedExecutionInput(
        tax_type=request.tax_type,
        regime_type=request.regime_type,
        regime_identifier=request.regime_identifier,
        tax_year=request.tax_year,
        rule_version=request.rule_version,
        primary_effective_date=primary_effective_date,
        historical_version_id=historical_version_id,
        resident_status_assertion=resident_status_assertion,
        income_category_signature=income_category_signature,
        canonical_input_payload=canonical_input_payload,
        canonical_input_json=canonical_input_json,
        input_hash=input_hash,
    )


def bind_prepared_input(prepared_input: PreparedExecutionInput) -> BoundRule:
    """Bind deterministic rule selection key before rule execution."""

    selection_key = RuleSelectionKey(
        tax_type=prepared_input.tax_type,
        regime_type=prepared_input.regime_type,
        regime_identifier=prepared_input.regime_identifier,
        tax_year=prepared_input.tax_year,
        rule_version=prepared_input.rule_version,
        primary_effective_date=prepared_input.primary_effective_date,
        historical_version_id=prepared_input.historical_version_id,
        resident_status_assertion=prepared_input.resident_status_assertion,
        income_category_signature=prepared_input.income_category_signature,
    )
    return bind_rule_selection(selection_key)


def execute_prepared_input(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
    rule_executor: RuleExecutor | None = None,
) -> ComputationExecutionResult:
    """Execute deterministic rules for already prepared canonical input."""

    normalized_prepared_input = normalize_transition_prepared_input(
        prepared_input=prepared_input,
        bound_rule=bound_rule,
    )
    active_rule_executor = (
        _resolve_rule_executor(bound_rule) if rule_executor is None else rule_executor
    )
    raw_result_payload = active_rule_executor(normalized_prepared_input, bound_rule)
    canonical_result_payload = _expect_mapping(canonicalize_for_hash(raw_result_payload))
    return ComputationExecutionResult(
        status="ok",
        tax_type=prepared_input.tax_type,
        regime_type=prepared_input.regime_type,
        tax_year=prepared_input.tax_year,
        rule_version=bound_rule.selection_key.rule_version,
        input_hash=prepared_input.input_hash,
        result_payload=canonical_result_payload,
    )


def execute_computation(
    request: ComputationExecutionRequest,
    rule_executor: RuleExecutor | None = None,
) -> ComputationExecutionResult:
    """Run deterministic prepare/bind/execute flow without persistence side effects."""

    prepared_input = prepare_execution_input(request)
    bound_rule = bind_prepared_input(prepared_input)
    return execute_prepared_input(
        prepared_input=prepared_input,
        bound_rule=bound_rule,
        rule_executor=rule_executor,
    )


def deterministic_stub_rule_executor(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> dict[str, object]:
    """Return deterministic stub output for Milestone 3 execution substrate."""

    return {
        "execution_mode": "deterministic_stub",
        "binding_id": bound_rule.binding_id,
        "rule_version": bound_rule.selection_key.rule_version,
        "input_hash": prepared_input.input_hash,
        "normalized_input": prepared_input.canonical_input_payload,
    }


def _resolve_rule_executor(bound_rule: BoundRule) -> RuleExecutor:
    if bound_rule.binding_id == "income_tax_resident_employment_v1_2021_01_01":
        return execute_resident_employment_2021_rule_pack
    if bound_rule.binding_id == "income_tax_non_resident_employment_v1_2021_01_01":
        return execute_non_resident_employment_2021_rule_pack
    if bound_rule.binding_id == "income_tax_resident_employment_v1_2023_07_01":
        return execute_resident_employment_rule_pack
    if (
        bound_rule.binding_id
        == "income_tax_resident_employment_plus_qualifying_interest_v1_2023_07_01"
    ):
        return execute_resident_employment_plus_qualifying_interest_rule_pack
    if bound_rule.binding_id == "income_tax_non_resident_employment_v1_2023_07_01":
        return execute_non_resident_employment_rule_pack
    if bound_rule.binding_id in {
        "health_contribution_nhif_legacy_v1_2010_07_16",
        "health_contribution_nhif_legacy_v1_2015_04_01",
        "health_contribution_nhif_legacy_v1_2021_05_28",
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
    }:
        return execute_nhif_legacy_rule_pack
    if bound_rule.binding_id in {
        "health_contribution_sha_shif_v1_2024_10_01",
        "health_contribution_sha_shif_v1_2025_02_28_pit",
    }:
        return execute_sha_shif_rule_pack
    if bound_rule.binding_id == "health_contribution_mixed_context_v1_fail_closed":
        return execute_fail_closed_mixed_context_rule_pack

    return deterministic_stub_rule_executor


def _expect_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Rule executor must return an object payload.")

    object_mapping = cast(Mapping[object, object], value)
    return {str(key): canonicalize_for_hash(item) for key, item in object_mapping.items()}


def _extract_primary_effective_date(
    canonical_input_payload: Mapping[str, object],
) -> date | None:
    version_context = canonical_input_payload.get("version_context")
    if not isinstance(version_context, Mapping):
        return None
    version_context_mapping = cast(Mapping[str, object], version_context)

    primary_effective_date = version_context_mapping.get("primary_effective_date")
    if primary_effective_date is None:
        return None
    if not isinstance(primary_effective_date, str):
        raise InputHashError(
            reason="invalid_primary_effective_date",
            message="primary_effective_date must be an ISO date string.",
            path="$.input_payload.version_context.primary_effective_date",
        )

    try:
        return date.fromisoformat(primary_effective_date)
    except ValueError as error:
        raise InputHashError(
            reason="invalid_primary_effective_date",
            message="primary_effective_date must be an ISO date string.",
            path="$.input_payload.version_context.primary_effective_date",
        ) from error


def _extract_historical_version_id(
    canonical_input_payload: Mapping[str, object],
) -> str | None:
    version_context = canonical_input_payload.get("version_context")
    if not isinstance(version_context, Mapping):
        return None
    version_context_mapping = cast(Mapping[str, object], version_context)

    historical_version_id = version_context_mapping.get("historical_version_id")
    if historical_version_id is None:
        return None
    if not isinstance(historical_version_id, str) or not historical_version_id.strip():
        raise InputHashError(
            reason="invalid_historical_version_id",
            message="historical_version_id must be a non-empty string when provided.",
            path="$.input_payload.version_context.historical_version_id",
        )
    return historical_version_id


def _extract_resident_status_assertion(
    canonical_input_payload: Mapping[str, object],
) -> str | None:
    taxpayer_context = canonical_input_payload.get("taxpayer_context")
    if not isinstance(taxpayer_context, Mapping):
        return None
    taxpayer_context_mapping = cast(Mapping[str, object], taxpayer_context)

    resident_status_assertion = taxpayer_context_mapping.get("resident_status_assertion")
    if resident_status_assertion is None:
        return None
    if not isinstance(resident_status_assertion, str) or not resident_status_assertion.strip():
        raise InputHashError(
            reason="invalid_resident_status_assertion",
            message="resident_status_assertion must be a non-empty string when provided.",
            path="$.input_payload.taxpayer_context.resident_status_assertion",
        )
    return resident_status_assertion


def _extract_income_category_signature(
    canonical_input_payload: Mapping[str, object],
) -> str | None:
    income_sections = canonical_input_payload.get("income_sections")
    if not isinstance(income_sections, Mapping):
        return None

    income_sections_mapping = cast(Mapping[str, object], income_sections)
    categories: list[str] = []
    for category in ("employment", "business", "investment", "rental"):
        section_value = income_sections_mapping.get(category)
        if isinstance(section_value, Mapping):
            categories.append(category)

    if not categories:
        return None
    return "+".join(sorted(categories))
