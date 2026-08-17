"""Verify deterministic allowlisted validation for income-tax intent plans."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.intent_to_plan import IncomeTaxOrchestrationPlan
from services.orchestration.app.intent_to_plan import translate_income_tax_intent_to_plan
from services.orchestration.app.intent_plan_validator import (
    validate_income_tax_intent_plan_for_dispatch,
)
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)

SUPPORTED_PROMPTS = (
    "Compute income tax for resident employment lane in tax year 2021 under KIT-VER-20210101-A.",
    (
        "Compute income tax for non-resident employment lane in tax year 2021 "
        "under KIT-VER-20210101-A."
    ),
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A.",
    (
        "Compute income tax for non-resident employment lane in tax year 2023 "
        "under KIT-VER-20230701-A."
    ),
    (
        "Compute income tax for resident employment plus qualifying interest lane in tax year 2023 "
        "under KIT-VER-20230701-A."
    ),
)


def _build_supported_plan(prompt_text: str) -> IncomeTaxOrchestrationPlan:
    envelope = parse_income_tax_prompt_intent_envelope(prompt_text)
    return deepcopy(translate_income_tax_intent_to_plan(envelope))


@pytest.mark.parametrize("prompt_text", SUPPORTED_PROMPTS)
def test_valid_supported_plan_passes_for_all_supported_lanes(prompt_text: str) -> None:
    plan = _build_supported_plan(prompt_text)

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result == {"validation_status": "accepted", "error": None}


def test_unknown_step_id_is_rejected_deterministically() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[0])
    plan["steps"][0]["step_id"] = "unknown_step_id"

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    error = result["error"]
    assert error is not None
    assert error["error_code"] == "unsupported_prompt_scope"
    assert error["reason"] == "unsupported_step_action"


def test_mismatched_module_or_action_for_known_step_is_rejected() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[1])
    plan["steps"][0]["module_ref"] = "services.orchestration.app.unknown_module"

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    error = result["error"]
    assert error is not None
    assert error["error_code"] == "unsupported_prompt_scope"
    assert error["reason"] == "unsupported_step_action"


def test_missing_required_field_is_rejected_deterministically() -> None:
    plan = cast(dict[str, object], _build_supported_plan(SUPPORTED_PROMPTS[2]))
    plan.pop("correlation_id", None)

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    error = result["error"]
    assert error is not None
    assert error["error_code"] == "unsupported_prompt_scope"
    assert error["reason"] == "malformed_plan"


def test_unsupported_lane_version_tax_year_is_rejected_deterministically() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[3])
    plan["supported_lane_id"] = "resident_employment_income_2099_01_01"
    plan["historical_version_id"] = "KIT-VER-20990101-A"
    plan["tax_year"] = 2099

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    error = result["error"]
    assert error is not None
    assert error["error_code"] == "unsupported_prompt_scope"
    assert error["reason"] == "unsupported_lane_context"


def test_external_action_true_is_rejected_deterministically() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[4])
    plan["steps"][0]["external_action"] = True

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    error = result["error"]
    assert error is not None
    assert error["error_code"] == "unsupported_prompt_scope"
    assert error["reason"] == "unsupported_external_action"


def test_rejection_envelope_is_canonical_for_unsupported_step_action() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[0])
    plan["steps"][0]["action_ref"] = "unsupported_action_ref"
    correlation_id = plan["correlation_id"]
    trace_id = plan["trace_id"]

    result = validate_income_tax_intent_plan_for_dispatch(plan)

    assert result["validation_status"] == "rejected"
    assert canonical_json_dumps(result["error"]) == canonical_json_dumps(
        {
            "error_code": "unsupported_prompt_scope",
            "message": "Plan step action is outside deterministic allowlisted supported scope.",
            "reason": "unsupported_step_action",
            "rejected_context": {
                "supported_lane_id": "resident_employment_income_2021_01_01",
                "historical_version_id": "KIT-VER-20210101-A",
                "tax_year": 2021,
                "tax_domain": "income_tax",
                "prompt_class": "income_tax_prompt_flow",
            },
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }
    )


def test_identical_invalid_plan_returns_identical_error_payload() -> None:
    plan = _build_supported_plan(SUPPORTED_PROMPTS[0])
    plan["steps"][0]["action_ref"] = "unsupported_action_ref"

    first = validate_income_tax_intent_plan_for_dispatch(deepcopy(plan))
    second = validate_income_tax_intent_plan_for_dispatch(deepcopy(plan))

    assert canonical_json_dumps(second) == canonical_json_dumps(first)
