"""Milestone 14 policy tests for the single Document AI OpenAI boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.document_ai.app.retry_policy import classify_document_ai_failure
from services.document_ai.app.governed_openai import OpenAIProviderError
from services.document_ai.app.governed_openai import GovernedOpenAIClient
from services.document_ai.app.governed_openai import PreparedOpenAISource
from services.document_ai.app.governed_openai import build_request_payload
from services.document_ai.app.governed_openai import GovernedOpenAIRequest


class _Transport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.payload: dict[str, object] | None = None

    def create(self, **payload: object) -> object:
        self.payload = payload
        return self.response


def _request() -> GovernedOpenAIRequest:
    return GovernedOpenAIRequest(
        processing_operation_id="operation-1",
        processing_attempt_id="attempt-1",
        tenant_id="tenant-a",
        source=PreparedOpenAISource(
            document_version_id="version-1",
            source_scope_id="pages-1-2",
            media_type="application/pdf",
            content=b"%PDF-1.7",
        ),
    )


def _structured_result() -> str:
    return (
        '{"result":{"schema_version":"v1","pages":['
        '{"page_number":1,"observations":['
        '{"observation_id":"observation-1","kind":"paragraph","order":0,'
        '"text":"read","state":"observed","source_location":null}]}],"warnings":[]}}'
    )


def test_governed_boundary_uses_fixed_model_and_strict_structured_output() -> None:
    transport = _Transport(
        SimpleNamespace(
            id="resp-1",
            output_text=_structured_result(),
            usage=SimpleNamespace(input_tokens=10, output_tokens=3, total_tokens=13),
        )
    )
    result = GovernedOpenAIClient(
        model="gpt-4.1-mini", timeout_seconds=60, transport=transport
    ).understand(_request())
    assert result.model == "gpt-4.1-mini"
    assert result.usage.total_tokens == 13
    assert transport.payload is not None
    assert transport.payload["model"] == "gpt-4.1-mini"
    format_payload = transport.payload["text"]
    assert isinstance(format_payload, dict)
    assert format_payload["format"]["strict"] is True  # type: ignore[index]
    assert "tenant-a" not in str(transport.payload)


def test_invalid_structured_output_is_permanent_failure() -> None:
    transport = _Transport(SimpleNamespace(id="resp-1", output_text="not json", usage=None))
    client = GovernedOpenAIClient(model="gpt-4.1-mini", timeout_seconds=60, transport=transport)
    with pytest.raises(OpenAIProviderError, match="invalid structured output") as caught:
        client.understand(_request())
    classified = classify_document_ai_failure(error=caught.value)
    assert classified.retryable is False
    assert classified.reason == "malformed_structured_output"


def test_request_requires_approved_policy_versions() -> None:
    request = _request().model_copy(update={"prompt_version": "invoice-fields-v1"})
    client = GovernedOpenAIClient(
        model="gpt-4.1-mini",
        timeout_seconds=60,
        transport=_Transport(SimpleNamespace(output_text=_structured_result(), usage=None)),
    )
    with pytest.raises(OpenAIProviderError, match="prompt version"):
        client.understand(request)


def test_arbitrary_model_is_not_a_supported_document_ai_client_configuration() -> None:
    with pytest.raises(ValueError, match="model_not_approved"):
        GovernedOpenAIClient(
            model="caller-selected-model",
            timeout_seconds=60,
            transport=_Transport(SimpleNamespace(output_text='{"result":{}}', usage=None)),
        )


@pytest.mark.parametrize(
    ("media_type", "expected_filename"),
    [
        ("application/pdf", "source.pdf"),
        ("text/plain", "source.txt"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "source.docx",
        ),
    ],
)
def test_provider_request_uses_media_type_aware_filename(
    media_type: str, expected_filename: str
) -> None:
    request = _request().model_copy(
        update={
            "source": _request().source.model_copy(
                update={
                    "media_type": media_type,
                    "content": b"%PDF-1.7" if media_type == "application/pdf" else b"line 1\n",
                }
            )
        }
    )
    payload = build_request_payload(request=request, model="gpt-4.1-mini")
    assert "storage_key" not in str(payload)
    assert expected_filename in str(payload)


def test_schema_rejects_cross_page_source_locations_and_duplicate_observations() -> None:
    invalid = (
        '{"result":{"schema_version":"v1","pages":['
        '{"page_number":1,"observations":['
        '{"observation_id":"same","kind":"paragraph","order":0,"text":"one",'
        '"state":"observed","source_location":{"page_number":2,"bounding_box":null,'
        '"start_offset":null,"end_offset":null}}]}],"warnings":[]}}'
    )
    client = GovernedOpenAIClient(
        model="gpt-4.1-mini",
        timeout_seconds=60,
        transport=_Transport(SimpleNamespace(output_text=invalid, usage=None)),
    )
    with pytest.raises(OpenAIProviderError, match="invalid structured output"):
        client.understand(_request())


def test_refusal_and_incomplete_output_are_not_persistable_successes() -> None:
    refusal = SimpleNamespace(output=[SimpleNamespace(type="refusal")], output_text="", usage=None)
    incomplete = SimpleNamespace(status="incomplete", output_text="", usage=None)
    for response, reason in (
        (refusal, "provider_refused"),
        (incomplete, "provider_output_incomplete"),
    ):
        client = GovernedOpenAIClient(
            model="gpt-4.1-mini", timeout_seconds=60, transport=_Transport(response)
        )
        with pytest.raises(OpenAIProviderError) as caught:
            client.understand(_request())
        assert caught.value.reason == reason
