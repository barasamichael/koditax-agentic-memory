"""Fixture-driven deterministic end-to-end prompt regression tests for supported lanes."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.main import create_app

_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


@dataclass(frozen=True)
class SupportedPromptFixture:
    fixture_id: str
    prompt_payload: dict[str, object]
    execution_context: dict[str, object]
    expected: dict[str, object]

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> SupportedPromptFixture:
        return cls(
            fixture_id=str(payload["fixture_id"]),
            prompt_payload=cast(dict[str, object], payload["prompt_payload"]),
            execution_context=cast(dict[str, object], payload["execution_context"]),
            expected=cast(dict[str, object], payload["expected"]),
        )


def _load_supported_fixtures() -> tuple[SupportedPromptFixture, ...]:
    fixtures: list[SupportedPromptFixture] = []
    for path in sorted(_FIXTURES_DIR.glob("supported_*.json"), key=lambda item: item.name):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        fixtures.append(SupportedPromptFixture.from_payload(cast(dict[str, object], payload)))
    assert fixtures
    return tuple(fixtures)


SUPPORTED_PROMPT_FIXTURES = _load_supported_fixtures()


@pytest.mark.parametrize("fixture", SUPPORTED_PROMPT_FIXTURES, ids=lambda item: item.fixture_id)
def test_supported_prompt_fixture_executes_deterministically(
    fixture: SupportedPromptFixture,
) -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: f"corr-{fixture.fixture_id}",
        TRACE_ID_HEADER_NAME: f"trace-{fixture.fixture_id}",
    }

    decide_first = client.post(
        "/v1/orchestration/prompt/decide",
        headers=headers,
        json=fixture.prompt_payload,
    )
    decide_second = client.post(
        "/v1/orchestration/prompt/decide",
        headers=headers,
        json=fixture.prompt_payload,
    )

    assert decide_first.status_code == 200
    assert decide_second.status_code == 200
    decide_first_payload = decide_first.json()
    decide_second_payload = decide_second.json()
    assert canonical_json_dumps(decide_second_payload) == canonical_json_dumps(decide_first_payload)

    execute_payload = {
        **fixture.prompt_payload,
        **fixture.execution_context,
        "idempotency_key": f"{fixture.execution_context['idempotency_key']}-{uuid4().hex}",
        "intent_class": decide_first_payload["intent_class"],
        "tax_domain_hint": decide_first_payload["tax_domain_hint"],
        "decision_id": decide_first_payload["decision_id"],
        "selected_route": decide_first_payload["selected_route"],
    }
    exec_first = client.post(
        "/v1/orchestration/prompt/execute",
        headers=headers,
        json=execute_payload,
    )
    exec_second = client.post(
        "/v1/orchestration/prompt/execute",
        headers=headers,
        json=execute_payload,
    )

    assert exec_first.status_code == 409
    assert exec_second.status_code == 409
    first_detail = exec_first.json()["detail"]
    second_detail = exec_second.json()["detail"]
    assert first_detail["error_code"] == "clarification_required"
    assert first_detail["reason_code"] == "clarification_required"
    assert second_detail["error_code"] == "clarification_required"
    assert second_detail["reason_code"] == "clarification_required"
    assert first_detail["correlation_id"] == headers[CORRELATION_ID_HEADER_NAME]
    assert first_detail["trace_id"] == headers[TRACE_ID_HEADER_NAME]
    assert second_detail["correlation_id"] == headers[CORRELATION_ID_HEADER_NAME]
    assert second_detail["trace_id"] == headers[TRACE_ID_HEADER_NAME]
    assert "income" in cast(list[str], first_detail["context"]["required_context_fields"])
    assert "income" in cast(list[str], second_detail["context"]["required_context_fields"])
