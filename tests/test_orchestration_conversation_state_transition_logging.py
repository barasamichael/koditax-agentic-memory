"""Regression coverage for transition-adjudication parse logging."""

from __future__ import annotations

import logging

import pytest

from services.orchestration.app.conversation_state_transition import (
    ConversationStateTransitionAdjudicatorError,
)
from services.orchestration.app.conversation_state_transition import (
    _build_transition_messages,
)
from services.orchestration.app.conversation_state_transition import (
    _build_transition_response_format,
)
from services.orchestration.app.conversation_state_transition import (
    _parse_transition_response,
)


def test_malformed_transition_response_logs_raw_body(caplog: pytest.LogCaptureFixture) -> None:
    response_body = (
        '{"next_turn":{"response":{"definition":"what is vat?"},"topic_shift":false}}'
    )

    with caplog.at_level(logging.ERROR, logger="services.orchestration.app.conversation_state_transition"):
        with pytest.raises(ConversationStateTransitionAdjudicatorError) as error:
            _parse_transition_response(response_body)

    assert error.value.reason_code == "invalid_response_format"
    assert "Malformed conversation-state transition response body:" in caplog.text
    assert response_body in caplog.text


def test_transition_prompt_forbids_next_turn_wrapper() -> None:
    messages = _build_transition_messages(
        prompt_text="what is vat?",
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge"},
        recent_conversation_state=(),
        prior_context_summary=None,
    )

    user_message = messages[1]["content"]
    assert "Do not wrap the output in next_turn" in user_message
    assert "Do not answer the user's question" in user_message
    assert "ConversationStateTransitionProposal" in user_message


def test_transition_response_format_is_strict_json_schema() -> None:
    response_format = _build_transition_response_format()

    assert response_format["type"] == "json_schema"
    json_schema = response_format["json_schema"]
    assert isinstance(json_schema, dict)
    assert json_schema["strict"] is True
    schema = json_schema["schema"]
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    assert "next_turn" not in schema.get("properties", {})
    assert "adjudication_status" in schema.get("required", [])
    assert "primary_relationship" in schema.get("required", [])
    assert schema["properties"]["updated_semantic_frame"]["type"] == "array"
    assert schema["$defs"]["ConversationStateCandidateBinding"]["properties"]["metadata"]["type"] == "array"
    assert _has_no_additional_properties_true(schema)
    assert _every_object_lists_all_properties_in_required(schema)
    assert _schema_has_no_defaults(schema)
    assert _schema_has_no_ref_sibling_defaults(schema)
    assert _schema_has_no_untyped_anyof_branches(schema)


def _has_no_additional_properties_true(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("additionalProperties") is True:
            return False
        return all(_has_no_additional_properties_true(item) for item in value.values())
    if isinstance(value, list):
        return all(_has_no_additional_properties_true(item) for item in value)
    return True


def _every_object_lists_all_properties_in_required(value: object) -> bool:
    if isinstance(value, dict):
        properties = value.get("properties")
        required = value.get("required")
        if isinstance(properties, dict):
            if required != list(properties.keys()):
                return False
        return all(_every_object_lists_all_properties_in_required(item) for item in value.values())
    if isinstance(value, list):
        return all(_every_object_lists_all_properties_in_required(item) for item in value)
    return True


def _schema_has_no_defaults(value: object) -> bool:
    if isinstance(value, dict):
        if "default" in value:
            return False
        return all(_schema_has_no_defaults(item) for item in value.values())
    if isinstance(value, list):
        return all(_schema_has_no_defaults(item) for item in value)
    return True


def _schema_has_no_ref_sibling_defaults(value: object) -> bool:
    if isinstance(value, dict):
        if "$ref" in value and len(value) > 1:
            return False
        return all(_schema_has_no_ref_sibling_defaults(item) for item in value.values())
    if isinstance(value, list):
        return all(_schema_has_no_ref_sibling_defaults(item) for item in value)
    return True


def _schema_has_no_untyped_anyof_branches(value: object) -> bool:
    if isinstance(value, dict):
        for key in ("anyOf", "oneOf"):
            branches = value.get(key)
            if isinstance(branches, list):
                for branch in branches:
                    if isinstance(branch, dict):
                        if (
                            "type" not in branch
                            and "$ref" not in branch
                            and "properties" not in branch
                            and "items" not in branch
                            and "enum" not in branch
                            and "const" not in branch
                        ):
                            return False
        return all(_schema_has_no_untyped_anyof_branches(item) for item in value.values())
    if isinstance(value, list):
        return all(_schema_has_no_untyped_anyof_branches(item) for item in value)
    return True
