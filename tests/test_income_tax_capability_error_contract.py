"""Lock canonical unsupported-scope error envelope behavior for runtime prompt gating."""

from __future__ import annotations

from typing import cast
import hashlib

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import SUPPORTED_PROMPT_BINDINGS
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from services.orchestration.app.trace_context import build_trace_id

UNSUPPORTED_VAT_PROMPT = "Compute VAT filing output for Q3 and submit to regulator."
UNSUPPORTED_SCOPE_MESSAGE = "Prompt scope is not supported by governed income-tax pilot capability."


def _expected_unsupported_scope_error_details(prompt_text: str) -> dict[str, object]:
    normalized_prompt_text = " ".join(prompt_text.strip().split()).lower()
    correlation_id = hashlib.sha256(normalized_prompt_text.encode("utf-8")).hexdigest()
    return {
        "error_code": "unsupported_prompt_scope",
        "message": UNSUPPORTED_SCOPE_MESSAGE,
        "reason": "unsupported_domain",
        "rejected_context": {
            "supported_lane_id": None,
            "historical_version_id": None,
            "tax_year": None,
            "tax_domain": "vat",
            "prompt_class": "income_tax_prompt_flow",
        },
        "correlation_id": correlation_id,
        "trace_id": build_trace_id(correlation_id),
    }


def test_supported_prompt_behavior_is_unchanged_by_error_contract_standardization() -> None:
    prompt_text = next(iter(SUPPORTED_PROMPT_BINDINGS))
    result = execute_income_tax_prompt_flow(prompt_text)
    draft_context = cast(dict[str, object], result["draft_context"])

    assert result["status"] == "draft_ready"
    assert draft_context["supported_lane_id"]
    assert draft_context["historical_version_id"]


def test_unsupported_scope_uses_canonical_error_envelope_exactly() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(UNSUPPORTED_VAT_PROMPT)

    assert error_info.value.reason == "unsupported_prompt_scope"
    assert canonical_json_dumps(error_info.value.details()) == canonical_json_dumps(
        _expected_unsupported_scope_error_details(UNSUPPORTED_VAT_PROMPT)
    )


def test_unsupported_scope_error_envelope_is_deterministic() -> None:
    def _details() -> dict[str, object]:
        with pytest.raises(IncomeTaxPromptFlowError) as error_info:
            execute_income_tax_prompt_flow(UNSUPPORTED_VAT_PROMPT)
        return error_info.value.details()

    assert canonical_json_dumps(_details()) == canonical_json_dumps(_details())


def test_unsupported_scope_error_contract_detects_drift() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(UNSUPPORTED_VAT_PROMPT)

    drifted = _expected_unsupported_scope_error_details(UNSUPPORTED_VAT_PROMPT)
    drifted["error_code"] = "drifted_error_code"

    with pytest.raises(AssertionError):
        assert canonical_json_dumps(error_info.value.details()) == canonical_json_dumps(drifted)
