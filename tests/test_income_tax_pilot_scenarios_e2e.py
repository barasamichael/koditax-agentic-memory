"""Execute governed phase 6.8 pilot scenario pack for supported income-tax lanes."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import cast
from typing import TypedDict
from typing import NotRequired
from pathlib import Path
from collections.abc import Generator

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof
from tests.income_tax_prompt_flow_support import build_income_tax_action_final_outcome_envelope
from services.orchestration.app.feature_flags import set_kill_switch
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config

SCENARIO_PACK_PATH = Path("eval/golden/e2e/pilot_income_tax_scenarios.json")

REQUIRED_SCENARIO_TYPES = {
    "happy_path_supported_lane",
    "unsupported_domain_rejection",
    "tenant_guard_rejection",
    "kill_switch_rejection",
    "confirmed_low_risk_action",
    "high_risk_step_up_required",
    "step_up_failed_rejection",
    "step_up_expired_rejection",
    "idempotent_replay_no_duplicate",
}
REQUIRED_HAPPY_LANES = {
    "resident_employment_income_2021_01_01",
    "non_resident_employment_income_2021_01_01",
    "resident_employment_income_2023_07_01",
    "non_resident_employment_income_2023_07_01",
    "resident_employment_plus_qualifying_interest_2023_07_01",
}


class ScenarioActionInput(TypedDict):
    action_type: str
    risk_class: str
    confirmation_decision: str


class ScenarioStepUpInput(TypedDict):
    proof_code: str
    verified_at: str
    authorized_at: str


class ScenarioSafetyControlInput(TypedDict):
    action_kill_switches: list[str]


class ScenarioInput(TypedDict):
    prompt_text: str
    tenant_id: str | None
    action: NotRequired[ScenarioActionInput]
    step_up: NotRequired[ScenarioStepUpInput]
    safety_controls: NotRequired[ScenarioSafetyControlInput]


class ScenarioExpected(TypedDict):
    outcome_status: str
    required_audit_event_types: list[str]
    required_trace_fields: list[str]
    expected_reason_code: NotRequired[str]
    supported_lane_id: NotRequired[str]
    expected_action_status: NotRequired[str]
    replay_identical: NotRequired[bool]


class PilotScenario(TypedDict):
    scenario_id: str
    scenario_type: str
    description: str
    input: ScenarioInput
    expected: ScenarioExpected


class ScenarioExecutionResult(TypedDict):
    final_envelope: dict[str, object]
    action_outcome: NotRequired[dict[str, object]]
    replay_identical: NotRequired[bool]


@pytest.fixture(autouse=True)
def _reset_runtime_safety_controls() -> (
    Generator[None, None, None]
):  # pyright: ignore[reportUnusedFunction]
    reset_runtime_safety_control_config()
    yield
    reset_runtime_safety_control_config()


def _load_scenario_pack() -> dict[str, object]:
    return cast(dict[str, object], json.loads(SCENARIO_PACK_PATH.read_text(encoding="utf-8")))


def _load_scenarios() -> list[PilotScenario]:
    pack = _load_scenario_pack()
    raw_scenarios = pack.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise AssertionError("Pilot scenario pack is missing scenarios list.")
    scenarios: list[PilotScenario] = []
    for raw_scenario in cast(list[object], raw_scenarios):
        if not isinstance(raw_scenario, dict):
            raise AssertionError("Pilot scenario entry must be object.")
        scenarios.append(cast(PilotScenario, raw_scenario))
    return scenarios


def _scenario_id(scenario: PilotScenario) -> str:
    return scenario["scenario_id"]


def _extract_reason_code_or_reason(envelope: dict[str, object]) -> str | None:
    result = cast(dict[str, object], envelope["result"])
    reason_code = result.get("reason_code")
    if isinstance(reason_code, str):
        return reason_code
    rejection = result.get("rejection")
    if isinstance(rejection, dict):
        rejected_reason_code = cast(dict[str, object], rejection).get("reason_code")
        if isinstance(rejected_reason_code, str):
            return rejected_reason_code
    mapped_result = result.get("mapped_result")
    if isinstance(mapped_result, dict):
        mapped_reason_code = cast(dict[str, object], mapped_result).get("reason_code")
        if isinstance(mapped_reason_code, str):
            return mapped_reason_code
    policy_decision = result.get("policy_decision")
    if isinstance(policy_decision, dict):
        policy_reason_code = cast(dict[str, object], policy_decision).get("reason_code")
        if isinstance(policy_reason_code, str):
            return policy_reason_code
    reason = result.get("reason")
    if isinstance(reason, str):
        return reason
    return None


def _resolve_confirmation_record(
    *,
    draft_outcome: dict[str, object],
    decision: str,
) -> dict[str, object]:
    awaiting = prepare_income_tax_confirmation_review(draft_outcome)
    awaiting_record = cast(dict[str, object], awaiting["state_record"])
    resolved = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision=decision,
    )
    return cast(dict[str, object], resolved["state_record"])


def _run_action_flow_scenario(scenario: PilotScenario) -> ScenarioExecutionResult:
    scenario_input = scenario["input"]
    action_input = scenario_input.get("action")
    if action_input is None:
        raise AssertionError(f"Scenario '{scenario['scenario_id']}' is missing action input.")
    prompt_text = scenario_input["prompt_text"]
    tenant_id = scenario_input.get("tenant_id")
    draft = execute_income_tax_prompt_flow(prompt_text, tenant_id=tenant_id)
    confirmation_record = _resolve_confirmation_record(
        draft_outcome=draft,
        decision=action_input["confirmation_decision"],
    )
    scenario_type = scenario["scenario_type"]

    if scenario_type == "kill_switch_rejection":
        safety_controls = scenario_input.get("safety_controls")
        if safety_controls is None:
            raise AssertionError(
                f"Scenario '{scenario['scenario_id']}' is missing safety_controls input."
            )
        for switch_key in safety_controls["action_kill_switches"]:
            set_kill_switch(switch_key=switch_key, enabled=True)

    first_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmation_record,
        action_type=action_input["action_type"],
        risk_class=action_input["risk_class"],
        tenant_id=tenant_id,
    )
    first_attempt_typed = first_attempt

    if scenario_type == "high_risk_step_up_required":
        return {
            "final_envelope": build_income_tax_action_final_outcome_envelope(first_attempt_typed),
            "action_outcome": first_attempt_typed,
        }

    if scenario_type in {"step_up_failed_rejection", "step_up_expired_rejection"}:
        step_up_input = scenario_input.get("step_up")
        if step_up_input is None:
            raise AssertionError(f"Scenario '{scenario['scenario_id']}' is missing step_up input.")
        challenge = cast(dict[str, object], first_attempt_typed["step_up_challenge"])
        challenge_record = cast(dict[str, object], challenge["challenge_record"])
        verification = verify_income_tax_action_step_up_proof(
            challenge_record=challenge_record,
            proof_code=step_up_input["proof_code"],
            verified_at=step_up_input["verified_at"],
        )
        binding = bind_income_tax_action_step_up_proof(
            action_attempt=first_attempt_typed,
            verification_result=verification,
        )
        proof_binding = binding["proof_binding"]
        if proof_binding is not None and not isinstance(proof_binding, dict):
            raise AssertionError(
                f"Scenario '{scenario['scenario_id']}' returned non-object proof_binding."
            )
        authorization = authorize_income_tax_action_with_step_up_proof(
            confirmation_record=confirmation_record,
            action_type=action_input["action_type"],
            risk_class=action_input["risk_class"],
            proof_binding=cast(dict[str, object] | None, proof_binding),
            tenant_id=tenant_id,
            authorized_at=step_up_input["authorized_at"],
        )
        authorization_typed = authorization
        return {
            "final_envelope": build_income_tax_action_final_outcome_envelope(authorization_typed),
            "action_outcome": authorization_typed,
        }

    if scenario_type == "idempotent_replay_no_duplicate":
        second_attempt = attempt_income_tax_action_request(
            confirmation_record=confirmation_record,
            action_type=action_input["action_type"],
            risk_class=action_input["risk_class"],
            tenant_id=tenant_id,
        )
        second_attempt_typed = second_attempt
        first_execution_envelope = cast(
            dict[str, object],
            first_attempt_typed["execution_envelope"],
        )
        second_execution_envelope = cast(
            dict[str, object], second_attempt_typed["execution_envelope"]
        )
        return {
            "final_envelope": build_income_tax_action_final_outcome_envelope(second_attempt_typed),
            "action_outcome": second_attempt_typed,
            "replay_identical": canonical_json_dumps(second_execution_envelope)
            == canonical_json_dumps(first_execution_envelope),
        }

    return {
        "final_envelope": build_income_tax_action_final_outcome_envelope(first_attempt_typed),
        "action_outcome": first_attempt_typed,
    }


def _execute_scenario(scenario: PilotScenario) -> ScenarioExecutionResult:
    scenario_type = scenario["scenario_type"]
    if scenario_type in {
        "happy_path_supported_lane",
        "unsupported_domain_rejection",
        "tenant_guard_rejection",
    }:
        scenario_input = scenario["input"]
        envelope = execute_income_tax_prompt_flow_final_outcome(
            scenario_input["prompt_text"],
            tenant_id=scenario_input.get("tenant_id"),
        )
        return {"final_envelope": envelope}

    return _run_action_flow_scenario(scenario)


def _assert_final_envelope_shape(envelope: dict[str, object]) -> None:
    assert set(envelope) == {"outcome_status", "message", "trace", "audit", "result"}
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    assert set(trace) == {"trace_id", "correlation_id", "lineage_refs"}
    assert set(audit) == {"event_count", "event_ids", "event_types", "latest_event_id_by_type"}


def test_pilot_scenario_pack_covers_required_supported_lanes_and_control_paths() -> None:
    scenarios = _load_scenarios()
    scenario_types = {scenario["scenario_type"] for scenario in scenarios}
    happy_lane_ids: set[str] = set()
    for scenario in scenarios:
        if scenario["scenario_type"] != "happy_path_supported_lane":
            continue
        lane_id = scenario["expected"].get("supported_lane_id")
        if lane_id is None:
            raise AssertionError(
                f"Scenario '{scenario['scenario_id']}' is missing expected supported_lane_id."
            )
        happy_lane_ids.add(lane_id)

    assert REQUIRED_SCENARIO_TYPES.issubset(scenario_types)
    assert REQUIRED_HAPPY_LANES == happy_lane_ids


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=_scenario_id)
def test_pilot_scenarios_execute_with_deterministic_expected_outcomes(
    scenario: PilotScenario,
) -> None:
    execution = _execute_scenario(scenario)
    envelope = execution["final_envelope"]
    expected = scenario["expected"]

    _assert_final_envelope_shape(envelope)
    assert envelope["outcome_status"] == expected["outcome_status"]

    trace = cast(dict[str, object], envelope["trace"])
    for required_trace_field in expected["required_trace_fields"]:
        assert isinstance(trace.get(required_trace_field), str)

    audit = cast(dict[str, object], envelope["audit"])
    event_types = set(cast(list[str], audit["event_types"]))
    assert set(expected["required_audit_event_types"]).issubset(event_types)

    expected_reason_code = expected.get("expected_reason_code")
    if expected_reason_code is not None:
        assert _extract_reason_code_or_reason(envelope) == expected_reason_code

    expected_lane_id = expected.get("supported_lane_id")
    if expected_lane_id is not None:
        result = cast(dict[str, object], envelope["result"])
        draft_context = cast(dict[str, object], result["draft_context"])
        assert draft_context["supported_lane_id"] == expected_lane_id

    expected_action_status = expected.get("expected_action_status")
    if expected_action_status is not None:
        result = cast(dict[str, object], envelope["result"])
        assert result["action_status"] == expected_action_status

    if expected.get("replay_identical") is True:
        assert execution.get("replay_identical") is True


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=_scenario_id)
def test_pilot_scenarios_are_deterministic_on_repeated_execution(
    scenario: PilotScenario,
) -> None:
    first = _execute_scenario(scenario)
    second = _execute_scenario(scenario)

    assert canonical_json_dumps(second["final_envelope"]) == canonical_json_dumps(
        first["final_envelope"]
    )
    assert second.get("replay_identical") == first.get("replay_identical")


def test_pilot_scenario_drift_guard_fails_fast_on_fixture_mismatch() -> None:
    scenario = deepcopy(_load_scenarios()[0])
    execution = _execute_scenario(scenario)
    expected = scenario["expected"]
    drifted_expected = dict(expected)
    drifted_expected["outcome_status"] = "rejected"

    with pytest.raises(AssertionError):
        assert execution["final_envelope"]["outcome_status"] == drifted_expected["outcome_status"]
