"""Fixture-driven deterministic end-to-end negative prompt regression tests."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.main import create_app

_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


@dataclass(frozen=True)
class NegativePromptFixture:
    fixture_id: str
    flow: str
    request_payload: dict[str, object]
    expected_error: dict[str, object]

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> NegativePromptFixture:
        return cls(
            fixture_id=str(payload["fixture_id"]),
            flow=str(payload["flow"]),
            request_payload=cast(dict[str, object], payload["request_payload"]),
            expected_error=cast(dict[str, object], payload["expected_error"]),
        )


def _load_negative_fixtures() -> tuple[NegativePromptFixture, ...]:
    fixtures: list[NegativePromptFixture] = []
    for path in sorted(_FIXTURES_DIR.glob("negative_*.json"), key=lambda item: item.name):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        fixtures.append(NegativePromptFixture.from_payload(cast(dict[str, object], payload)))
    assert fixtures
    return tuple(fixtures)


NEGATIVE_PROMPT_FIXTURES = _load_negative_fixtures()


@pytest.mark.parametrize("fixture", NEGATIVE_PROMPT_FIXTURES, ids=lambda item: item.fixture_id)
def test_negative_prompt_fixture_rejection_is_deterministic(
    fixture: NegativePromptFixture,
) -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: f"corr-{fixture.fixture_id}",
        TRACE_ID_HEADER_NAME: f"trace-{fixture.fixture_id}",
    }

    first = _run_negative_fixture(client=client, fixture=fixture, headers=headers)
    second = _run_negative_fixture(client=client, fixture=fixture, headers=headers)

    status_code_raw = fixture.expected_error.get("status_code")
    assert isinstance(status_code_raw, int)
    expected_status_code = status_code_raw
    assert first.status_code == expected_status_code
    assert second.status_code == expected_status_code
    first_body = cast(dict[str, object], first.json())
    second_body = cast(dict[str, object], second.json())
    assert canonical_json_dumps(second_body) == canonical_json_dumps(first_body)

    if expected_status_code < 400:
        assert first_body["status"] == "resolved"
        assert first_body["gate_status"] == "allowed"
        assert first_body["selected_route"] == {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
        }
        assert first_body["intent_class"] == "lookup_grounded_knowledge"
        assert first_body["tax_domain_hint"] == "vat"
        assert first_body["clarification"] is None
        assert cast(dict[str, object], first_body["turn_resolution"])["answerability"] == "answerable"
        return

    first_detail = cast(dict[str, object], first_body["detail"])
    second_detail = cast(dict[str, object], second_body["detail"])
    assert first_detail["error_code"] == fixture.expected_error["error_code"]
    assert first_detail["reason"] == fixture.expected_error["reason"]
    assert first_detail["reason_code"] == fixture.expected_error["reason_code"]
    assert first_detail["correlation_id"] == headers[CORRELATION_ID_HEADER_NAME]
    assert first_detail["trace_id"] == headers[TRACE_ID_HEADER_NAME]
    assert canonical_json_dumps(second_detail) == canonical_json_dumps(first_detail)


def _run_negative_fixture(
    *,
    client: TestClient,
    fixture: NegativePromptFixture,
    headers: dict[str, str],
):
    if fixture.flow == "decide":
        return client.post(
            "/v1/orchestration/prompt/decide",
            headers=headers,
            json=fixture.request_payload,
        )
    if fixture.flow == "execute_with_decision":
        request_payload = dict(fixture.request_payload)
        decide_payload = {
            "tenant_id": request_payload["tenant_id"],
            "conversation_id": request_payload["conversation_id"],
            "channel": request_payload["channel"],
            "prompt": request_payload["prompt"],
        }
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers=headers,
            json=decide_payload,
        )
        assert decide.status_code == 200
        decision_payload = decide.json()
        request_payload["decision_id"] = decision_payload["decision_id"]
        return client.post(
            "/v1/orchestration/prompt/execute",
            headers=headers,
            json=request_payload,
        )
    raise AssertionError(f"Unsupported fixture flow '{fixture.flow}'.")
