"""Verify deterministic end-to-end health-contribution prompt flow for governed lanes."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.health_contribution_prompt_flow_support import SUPPORTED_HEALTH_PROMPT_BINDINGS
from tests.health_contribution_prompt_flow_support import decide_health_contribution_prompt
from tests.health_contribution_prompt_flow_support import ALL_SUPPORTED_HEALTH_PROMPT_BINDINGS
from tests.health_contribution_prompt_flow_support import execute_health_contribution_prompt_flow
from tests.health_contribution_prompt_flow_support import (
    SUPPORTED_HEALTH_TRANSITION_PROMPT_BINDINGS,
)


@pytest.mark.parametrize(
    ("prompt_text", "expected_lane_id", "expected_historical_version_id", "expected_regime"),
    [
        (
            prompt_text,
            binding.supported_lane_id,
            binding.historical_version_id,
            binding.regime_identifier,
        )
        for prompt_text, binding in SUPPORTED_HEALTH_PROMPT_BINDINGS.items()
    ],
)
def test_supported_health_prompts_complete_end_to_end_deterministically(
    prompt_text: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
    expected_regime: str,
) -> None:
    prompt_result = execute_health_contribution_prompt_flow(prompt_text)

    ingestion = cast(dict[str, object], prompt_result["ingestion"])
    decision = cast(dict[str, object], prompt_result["decision"])
    execution = cast(dict[str, object], prompt_result["execution"])
    computation_output = cast(dict[str, object], prompt_result["computation_output"])
    finalized_output = cast(dict[str, object], prompt_result["finalized_output"])
    forms_mapping = cast(dict[str, object], prompt_result["forms_mapping"])
    report_generation = cast(dict[str, object], prompt_result["report_generation"])

    assert ingestion["status"] == "accepted"
    assert decision["status"] == "resolved"
    assert decision["tax_domain_hint"] == "health_contribution"
    assert decision["supported_lane_id"] == expected_lane_id
    assert decision["historical_version_id"] == expected_historical_version_id
    assert decision["regime_identifier"] == expected_regime

    assert execution["status"] == "executed"
    assert execution["tax_domain_hint"] == "health_contribution"
    assert execution["supported_lane_id"] == expected_lane_id
    assert execution["historical_version_id"] == expected_historical_version_id
    assert execution["regime_identifier"] == expected_regime

    assert computation_output["status"] == "ok"
    result_payload = cast(dict[str, object], computation_output["result_payload"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    traceability = cast(dict[str, object], result_payload["traceability"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    assert version_identity["historical_version_id"] == expected_historical_version_id
    assert contribution_summary["coverage_status"] == "implementation_ready"
    assert traceability["replay_safe"] is True

    assert finalized_output["tax_type"] == "health_contribution"
    assert finalized_output["input_hash"] == computation_output["input_hash"]

    assert forms_mapping["status"] == "ok"
    assert forms_mapping["mapping_status"] == "ok"
    mapping_output = cast(dict[str, object], forms_mapping["mapping_output"])
    mapping_version_identity = cast(dict[str, object], mapping_output["version_identity"])
    contributor = cast(dict[str, object], mapping_output["contributor"])
    assert mapping_output["supported_lane_id"] == expected_lane_id
    assert mapping_version_identity["historical_version_id"] == expected_historical_version_id
    assert contributor["regime_family"] in {"nhif_legacy", "sha_shif"}

    assert report_generation["status"] == "generated"
    assert report_generation["report_type"] == "health_contribution_summary"
    lineage_reference = cast(dict[str, object], report_generation["lineage_reference"])
    assert lineage_reference["tax_type"] == "health_contribution"
    assert lineage_reference["supported_lane_id"] == expected_lane_id
    assert lineage_reference["historical_version_id"] == expected_historical_version_id
    assert lineage_reference["computation_id"] == finalized_output["computation_id"]


@pytest.mark.parametrize(
    ("prompt_text", "expected_lane_id", "expected_historical_version_id"),
    [
        (
            prompt_text,
            binding.supported_lane_id,
            binding.historical_version_id,
        )
        for prompt_text, binding in SUPPORTED_HEALTH_TRANSITION_PROMPT_BINDINGS.items()
    ],
)
def test_supported_health_transition_prompts_preserve_resolved_governed_identity(
    prompt_text: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    prompt_result = execute_health_contribution_prompt_flow(prompt_text)

    decision = cast(dict[str, object], prompt_result["decision"])
    execution = cast(dict[str, object], prompt_result["execution"])
    computation_output = cast(dict[str, object], prompt_result["computation_output"])
    result_payload = cast(dict[str, object], computation_output["result_payload"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contributor_outcome = cast(dict[str, object], result_payload["contributor_outcome"])
    forms_mapping = cast(dict[str, object], prompt_result["forms_mapping"])
    mapping_output = cast(dict[str, object], forms_mapping["mapping_output"])

    assert decision["regime_identifier"] == "transition_boundary"
    assert decision["supported_lane_id"] == expected_lane_id
    assert execution["regime_identifier"] == "transition_boundary"
    assert execution["supported_lane_id"] == expected_lane_id
    assert version_identity["historical_version_id"] == expected_historical_version_id
    assert contributor_outcome["regime_family"] in {"nhif_legacy", "sha_shif"}
    assert mapping_output["supported_lane_id"] == expected_lane_id


def test_health_prompt_flow_is_deterministic_for_identical_supported_prompt_inputs() -> None:
    prompt_text = (
        "Compute health contribution for sha/shif salaried lane in tax year 2024 "
        "under HCH-VER-20241001-A."
    )

    first = execute_health_contribution_prompt_flow(prompt_text)
    second = execute_health_contribution_prompt_flow(prompt_text)

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_non_ready_2003_health_prompt_fails_closed_canonically() -> None:
    status_code, payload = decide_health_contribution_prompt(
        "Compute health contribution for nhif legacy lane in tax year 2009 "
        "under HCH-VER-20031205-A."
    )

    assert status_code == 404
    detail = cast(dict[str, object], payload["detail"])
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "unsupported_health_version_window"
    context = cast(dict[str, object], detail["context"])
    assert context["historical_version_id"] == "HCH-VER-20031205-A"
    assert context["regime_identifier"] == "nhif_legacy"


def test_governed_boundary_only_health_prompt_fails_closed_canonically() -> None:
    status_code, payload = decide_health_contribution_prompt(
        "Compute health contribution for nhif legacy lane in tax year 2015 "
        "under HCH-VER-20141208-A."
    )

    assert status_code == 404
    detail = cast(dict[str, object], payload["detail"])
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "unsupported_health_version_window"
    context = cast(dict[str, object], detail["context"])
    assert context["historical_version_id"] == "HCH-VER-20141208-A"


def test_unresolved_mixed_context_health_prompt_fails_closed_canonically() -> None:
    status_code, payload = decide_health_contribution_prompt(
        "Compute health contribution for mixed context lane in tax year 2024 "
        "under HCH-VER-20241001-A."
    )

    assert status_code == 404
    detail = cast(dict[str, object], payload["detail"])
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "missing_health_lane_context"


def test_unresolved_special_case_health_prompt_fails_closed_canonically() -> None:
    status_code, payload = decide_health_contribution_prompt(
        "Compute health contribution for exemption claim lane in tax year 2024 "
        "under HCH-VER-20241001-A."
    )

    assert status_code == 404
    detail = cast(dict[str, object], payload["detail"])
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "missing_health_lane_context"


def test_malformed_health_prompt_fails_deterministically() -> None:
    status_code, payload = decide_health_contribution_prompt("Compute health contribution.")

    assert status_code == 404
    detail = cast(dict[str, object], payload["detail"])
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "missing_health_lane_context"


def test_rejected_health_prompt_flow_is_deterministic_for_identical_inputs() -> None:
    prompt_text = (
        "Compute health contribution for nhif legacy lane in tax year 2009 "
        "under HCH-VER-20031205-A."
    )

    first_status, first_payload = decide_health_contribution_prompt(prompt_text)
    second_status, second_payload = decide_health_contribution_prompt(prompt_text)

    assert first_status == second_status == 404
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)


def test_supported_health_prompt_bindings_cover_full_governed_runtime_set() -> None:
    governed_versions = {
        binding.historical_version_id for binding in ALL_SUPPORTED_HEALTH_PROMPT_BINDINGS.values()
    }
    assert governed_versions == {
        "HCH-VER-20100716-A",
        "HCH-VER-20150401-A",
        "HCH-VER-20210528-A",
        "HCH-VER-20221231-REG",
        "HCH-VER-20241001-A",
        "HCH-VER-20250228-PIT",
    }
