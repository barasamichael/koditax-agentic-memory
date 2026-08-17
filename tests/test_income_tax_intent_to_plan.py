"""Verify deterministic translation from intent envelope to governed orchestration plan."""

from __future__ import annotations

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.intent_to_plan import IntentToPlanError
from services.orchestration.app.intent_to_plan import PLAN_STEP_DEFINITIONS
from services.orchestration.app.intent_to_plan import translate_income_tax_intent_to_plan
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

EXPECTED_STEP_IDS = [
    "capability_check",
    "income_tax_computation",
    "form_mapping",
    "form_version_binding",
    "form_artifact_generation",
    "report_generation",
    "report_version_binding",
    "submission_payload_construction",
    "submission_workflow_initialize",
    "submission_workflow_ready_transition",
    "submission_workflow_internal_submit_transition",
    "submission_closure",
]
EXPECTED_STEP_TUPLES = [
    (
        step["step_order"],
        step["step_id"],
        step["module_ref"],
        step["action_ref"],
        step["external_action"],
    )
    for step in (
        {
            "step_order": index,
            "step_id": definition.step_id,
            "module_ref": definition.module_ref,
            "action_ref": definition.action_ref,
            "external_action": False,
        }
        for index, definition in enumerate(PLAN_STEP_DEFINITIONS, start=1)
    )
]


@pytest.mark.parametrize("prompt_text", SUPPORTED_PROMPTS)
def test_supported_intent_envelope_translates_to_expected_deterministic_plan(
    prompt_text: str,
) -> None:
    envelope = parse_income_tax_prompt_intent_envelope(prompt_text)
    plan = translate_income_tax_intent_to_plan(envelope)

    assert plan["plan_status"] == "planned"
    assert plan["intent_class"] == "compute_income_tax"
    assert plan["supported_lane_id"]
    assert plan["historical_version_id"]
    assert plan["tax_year"]
    assert plan["plan_id"]
    assert plan["correlation_id"] == envelope["correlation_id"]
    assert plan["trace_id"] == envelope["trace_id"]

    steps = plan["steps"]
    assert [step["step_id"] for step in steps] == EXPECTED_STEP_IDS
    assert [step["step_order"] for step in steps] == list(range(1, len(EXPECTED_STEP_IDS) + 1))
    assert [
        (
            step["step_order"],
            step["step_id"],
            step["module_ref"],
            step["action_ref"],
            step["external_action"],
        )
        for step in steps
    ] == EXPECTED_STEP_TUPLES


def test_unsupported_domain_intent_is_rejected_deterministically() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute VAT filing output for Q3 and submit to regulator."
    )

    with pytest.raises(IntentToPlanError) as error_info:
        translate_income_tax_intent_to_plan(envelope)

    assert error_info.value.error_code == "unsupported_intent_plan"
    assert error_info.value.reason == "unsupported_domain"
    assert canonical_json_dumps(error_info.value.payload()) == canonical_json_dumps(
        {
            "error_code": "unsupported_intent_plan",
            "message": "Intent envelope domain is outside governed income-tax pilot scope.",
            "reason": "unsupported_domain",
            "rejected_context": {
                "tax_domain_hint": "vat",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "unsupported_domain_request",
                "prompt_class": "income_tax_prompt_flow",
            },
            "correlation_id": envelope["correlation_id"],
            "trace_id": envelope["trace_id"],
        }
    )


def test_unknown_intent_class_is_rejected_deterministically() -> None:
    envelope = parse_income_tax_prompt_intent_envelope("Please compute income tax quickly.")

    with pytest.raises(IntentToPlanError) as error_info:
        translate_income_tax_intent_to_plan(envelope)

    assert error_info.value.error_code == "unsupported_intent_plan"
    assert error_info.value.reason == "unsupported_intent_class"


def test_unsupported_lane_version_request_is_rejected_deterministically() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute income tax for resident employment lane in tax year 2024 under KIT-VER-20240101-A."
    )

    with pytest.raises(IntentToPlanError) as error_info:
        translate_income_tax_intent_to_plan(envelope)

    assert error_info.value.error_code == "unsupported_intent_plan"
    assert error_info.value.reason == "unsupported_lane_context"
    assert error_info.value.payload()["rejected_context"] == {
        "tax_domain_hint": "income_tax",
        "requested_lane_hint": None,
        "historical_version_hint": "KIT-VER-20240101-A",
        "tax_year_hint": 2024,
        "intent_class": "compute_income_tax",
        "prompt_class": "income_tax_prompt_flow",
    }


def test_intent_to_plan_translation_is_deterministic_for_identical_envelope() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )

    first = translate_income_tax_intent_to_plan(envelope)
    second = translate_income_tax_intent_to_plan(envelope)

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_generated_plan_never_contains_unsupported_steps_or_external_actions() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute income tax for non-resident employment lane in tax year 2023 "
        "under KIT-VER-20230701-A."
    )
    plan = translate_income_tax_intent_to_plan(envelope)

    assert [step["step_id"] for step in plan["steps"]] == EXPECTED_STEP_IDS
    assert all(step["external_action"] is False for step in plan["steps"])


def test_plan_drift_detection_fails_when_expected_step_order_changes() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute income tax for resident employment lane in tax year 2021 under KIT-VER-20210101-A."
    )
    plan = translate_income_tax_intent_to_plan(envelope)
    drifted_step_ids = EXPECTED_STEP_IDS.copy()
    drifted_step_ids[0] = "drifted_capability_check"

    with pytest.raises(AssertionError):
        assert [step["step_id"] for step in plan["steps"]] == drifted_step_ids
