"""Verify deterministic prompt normalization and intent-envelope parsing contract."""

from __future__ import annotations

import hashlib

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.trace_context import build_trace_id
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelopeError
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)


@pytest.mark.parametrize(
    ("prompt_text", "expected_lane", "expected_version", "expected_tax_year"),
    [
        (
            (
                "Compute income tax for resident employment lane in tax year 2021 "
                "under KIT-VER-20210101-A."
            ),
            "resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
            2021,
        ),
        (
            (
                "Compute income tax for non-resident employment lane in tax year 2021 "
                "under KIT-VER-20210101-A."
            ),
            "non_resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
            2021,
        ),
        (
            (
                "Compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
            2023,
        ),
        (
            (
                "Compute income tax for non-resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "non_resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
            2023,
        ),
        (
            (
                "Compute income tax for resident employment plus qualifying interest "
                "lane in tax year 2023 under KIT-VER-20230701-A."
            ),
            "resident_employment_plus_qualifying_interest_2023_07_01",
            "KIT-VER-20230701-A",
            2023,
        ),
    ],
)
def test_supported_prompt_shape_parses_into_expected_intent_envelope(
    prompt_text: str,
    expected_lane: str,
    expected_version: str,
    expected_tax_year: int,
) -> None:
    envelope = parse_income_tax_prompt_intent_envelope(prompt_text)

    assert envelope["tax_domain_hint"] == "income_tax"
    assert envelope["requested_lane_hint"] == expected_lane
    assert envelope["historical_version_hint"] == expected_version
    assert envelope["tax_year_hint"] == expected_tax_year
    assert envelope["intent_class"] == "compute_income_tax"
    assert envelope["parsing_status"] == "parsed"
    assert envelope["prompt_class"] == "income_tax_prompt_flow"


def test_prompt_normalization_is_deterministic_and_canonicalized() -> None:
    first = parse_income_tax_prompt_intent_envelope(
        "   COMPUTE   INCOME TAX for resident employment lane in tax year 2023 "
        "under kit-ver-20230701-a.   "
    )
    second = parse_income_tax_prompt_intent_envelope(
        "compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )

    assert first["normalized_prompt_text"] == second["normalized_prompt_text"]
    assert canonical_json_dumps(first) == canonical_json_dumps(second)


def test_supported_prompt_shape_matches_canonical_envelope_exactly() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    normalized_prompt_text = (
        "compute income tax for resident employment lane in tax year 2023 under kit-ver-20230701-a."
    )
    correlation_id = hashlib.sha256(normalized_prompt_text.encode("utf-8")).hexdigest()
    envelope = parse_income_tax_prompt_intent_envelope(prompt_text)
    expected = {
        "normalized_prompt_text": normalized_prompt_text,
        "tax_domain_hint": "income_tax",
        "requested_lane_hint": "resident_employment_income_2023_07_01",
        "historical_version_hint": "KIT-VER-20230701-A",
        "tax_year_hint": 2023,
        "intent_class": "compute_income_tax",
        "parsing_status": "parsed",
        "prompt_class": "income_tax_prompt_flow",
        "correlation_id": correlation_id,
        "trace_id": build_trace_id(correlation_id),
    }

    assert canonical_json_dumps(envelope) == canonical_json_dumps(expected)


def test_empty_prompt_fails_with_canonical_error_contract() -> None:
    with pytest.raises(PromptIntentEnvelopeError) as error_info:
        parse_income_tax_prompt_intent_envelope("   ")

    expected_payload = {
        "error_code": "invalid_prompt_input",
        "message": "Prompt text must be non-empty for intent envelope parsing.",
        "reason": "empty_prompt_text",
        "rejected_context": {
            "tax_domain_hint": None,
            "requested_lane_hint": None,
            "historical_version_hint": None,
            "tax_year_hint": None,
            "intent_class": "unknown",
            "prompt_class": "income_tax_prompt_flow",
        },
        "correlation_id": hashlib.sha256(b"").hexdigest(),
        "trace_id": build_trace_id(hashlib.sha256(b"").hexdigest()),
    }
    assert canonical_json_dumps(error_info.value.payload()) == canonical_json_dumps(
        expected_payload
    )


def test_unsupported_domain_prompt_yields_deterministic_unsupported_intent_envelope() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "Compute VAT filing output for Q3 and submit to regulator."
    )

    assert envelope["tax_domain_hint"] == "vat"
    assert envelope["intent_class"] == "unsupported_domain_request"
    assert envelope["parsing_status"] == "parsed_with_unsupported_scope_hint"
    assert envelope["requested_lane_hint"] is None
    assert envelope["historical_version_hint"] is None
    assert envelope["tax_year_hint"] is None


def test_malformed_income_tax_prompt_shape_yields_unknown_intent_envelope() -> None:
    envelope = parse_income_tax_prompt_intent_envelope("Please compute income tax quickly.")

    assert envelope["tax_domain_hint"] == "income_tax"
    assert envelope["requested_lane_hint"] is None
    assert envelope["historical_version_hint"] is None
    assert envelope["tax_year_hint"] is None
    assert envelope["intent_class"] == "unknown"
    assert envelope["parsing_status"] == "parsed_with_unsupported_scope_hint"


def test_repeated_parse_of_same_prompt_is_byte_equivalent() -> None:
    prompt_text = (
        "Compute income tax for resident employment plus qualifying interest lane "
        "in tax year 2023 under KIT-VER-20230701-A."
    )
    first = parse_income_tax_prompt_intent_envelope(prompt_text)
    second = parse_income_tax_prompt_intent_envelope(prompt_text)

    assert canonical_json_dumps(first) == canonical_json_dumps(second)
