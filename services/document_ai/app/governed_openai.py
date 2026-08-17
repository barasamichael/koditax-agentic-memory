"""The sole governed OpenAI boundary for Document AI processing.

This boundary deliberately has no document-type prompt or public provider
contract.  It accepts an already-authorized, bounded source scope and returns
only a validated platform-owned result for later canonical assembly.
"""

from __future__ import annotations

import json
from time import perf_counter
import base64
from typing import cast
from typing import Literal
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Mapping

from openai import OpenAI
from openai import APIStatusError
from openai import APITimeoutError
from openai import APIConnectionError
from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError

from services.document_ai.app.config import get_document_ai_openai_model
from services.document_ai.app.config import get_document_ai_openai_api_key
from services.document_ai.app.config import APPROVED_DOCUMENT_AI_OPENAI_MODELS
from services.document_ai.app.config import get_document_ai_openai_timeout_seconds
from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.document_formats import extension_for_media_type

OPENAI_PROCESSING_POLICY_VERSION = "v1"
OPENAI_DOCUMENT_PROMPT_VERSION = "general-document-understanding-v1"
OPENAI_PROVIDER_RESULT_SCHEMA_VERSION = "v1"
# Provider units are deliberately smaller than the 200 MB platform admission
# limit.  Large sources are prepared as bounded units by the worker policy.
MAX_OPENAI_SOURCE_BYTES = 8 * 1024 * 1024
MAX_UNDERSTANDING_PAGES = 1_000
MAX_UNDERSTANDING_OBSERVATIONS_PER_PAGE = 2_000
MAX_UNDERSTANDING_TEXT_LENGTH = 20_000
UNDERSTANDING_OBSERVATION_KINDS: frozenset[str] = frozenset(
    {
        "heading",
        "paragraph",
        "section",
        "list",
        "list_item",
        "table",
        "form",
        "image",
        "chart",
        "caption",
        "header",
        "footer",
        "footnote",
        "annotation",
        "handwriting",
        "identifier",
        "amount",
        "date",
        "relationship",
        "unknown",
    }
)


class OpenAIProviderError(RuntimeError):
    """A safe, classified provider failure for worker retry handling."""

    def __init__(self, reason: str, *, retryable: bool, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.message = message


class PreparedOpenAISource(BaseModel):
    """A source scope prepared only after authorization and source inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str = Field(min_length=1)
    source_scope_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1, max_length=127)
    content: bytes = Field(min_length=1, max_length=MAX_OPENAI_SOURCE_BYTES)
    structural_scope_ids: tuple[str, ...] = ()
    structural_scope_manifest: tuple[dict[str, object], ...] = ()


class GovernedOpenAIRequest(BaseModel):
    """Required platform identity and policy binding for one provider call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    processing_operation_id: str = Field(min_length=1)
    processing_attempt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    source: PreparedOpenAISource
    processing_policy_version: str = OPENAI_PROCESSING_POLICY_VERSION
    prompt_version: str = OPENAI_DOCUMENT_PROMPT_VERSION
    canonical_schema_version: str = OPENAI_PROVIDER_RESULT_SCHEMA_VERSION
    validation_policy: str = "strict-json-schema-v1"


class ProviderUsage(BaseModel):
    """Permitted usage metadata; content and raw provider payload are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ValidatedProviderResult(BaseModel):
    """Validated non-canonical provider artifact for a future assembly milestone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai"] = "openai"
    provider_response_id: str | None = None
    provider_request_id: str | None = None
    model: str
    processing_operation_id: str
    processing_attempt_id: str
    document_version_id: str
    source_scope_id: str
    processing_policy_version: str
    prompt_version: str
    canonical_schema_version: str
    result: dict[str, object]
    usage: ProviderUsage
    latency_ms: int = Field(ge=0)
    structural_scope_ids: tuple[str, ...] = ()
    provider_result_state: Literal["validated"] = "validated"


class OpenAIResponsesTransport(Protocol):
    """Narrow injectable transport; production remains the official SDK client."""

    def create(self, **payload: object) -> object: ...


@dataclass(frozen=True)
class GovernedOpenAIClient:
    """Construct requests from fixed policy, not external caller-selected models."""

    model: str
    timeout_seconds: int
    transport: OpenAIResponsesTransport

    def __post_init__(self) -> None:
        """Keep even internal construction within the configured model policy."""

        if self.model not in APPROVED_DOCUMENT_AI_OPENAI_MODELS:
            raise ValueError("document_ai_openai_model_not_approved")
        if not 1 <= self.timeout_seconds <= 600:
            raise ValueError("document_ai_openai_timeout_out_of_range")

    @classmethod
    def from_environment(cls) -> GovernedOpenAIClient:
        api_key = get_document_ai_openai_api_key()
        if api_key is None:
            raise OpenAIProviderError(
                "missing_openai_configuration",
                retryable=False,
                message="OpenAI document processing is not configured.",
            )
        timeout = get_document_ai_openai_timeout_seconds()
        client = OpenAI(api_key=api_key, timeout=timeout)
        return cls(
            model=get_document_ai_openai_model(),
            timeout_seconds=timeout,
            transport=cast(OpenAIResponsesTransport, client.responses),
        )

    def understand(self, request: GovernedOpenAIRequest) -> ValidatedProviderResult:
        """Make exactly one policy-bound Responses API request and validate its result."""

        if request.processing_policy_version != OPENAI_PROCESSING_POLICY_VERSION:
            raise OpenAIProviderError(
                "invalid_processing_policy",
                retryable=False,
                message="OpenAI processing policy is not approved.",
            )
        if request.prompt_version != OPENAI_DOCUMENT_PROMPT_VERSION:
            raise OpenAIProviderError(
                "invalid_prompt_version",
                retryable=False,
                message="OpenAI prompt version is not approved.",
            )
        started = perf_counter()
        try:
            payload = build_request_payload(request=request, model=self.model)
            response = self.transport.create(**payload)
        except APITimeoutError as error:
            raise OpenAIProviderError(
                "upstream_timeout", retryable=True, message="OpenAI timed out."
            ) from error
        except APIConnectionError as error:
            raise OpenAIProviderError(
                "upstream_unavailable", retryable=True, message="OpenAI is unavailable."
            ) from error
        except APIStatusError as error:
            retryable = error.status_code == 429 or error.status_code >= 500
            reason = _provider_status_reason(error.status_code)
            raise OpenAIProviderError(
                reason, retryable=retryable, message="OpenAI rejected the processing request."
            ) from error
        except Exception as error:  # defensive boundary around SDK changes
            raise OpenAIProviderError(
                "openai_transport_failure", retryable=True, message="OpenAI transport failed."
            ) from error
        latency_ms = round((perf_counter() - started) * 1000)
        return _validate_response(
            response=response, request=request, model=self.model, latency_ms=latency_ms
        )


def build_request_payload(*, request: GovernedOpenAIRequest, model: str) -> dict[str, object]:
    """Build the fixed, non-public provider payload for one validated source."""

    encoded = base64.b64encode(request.source.content).decode("ascii")
    extension = extension_for_media_type(request.source.media_type) or ".bin"
    scope_manifest = list(request.source.structural_scope_manifest)
    scope_instruction = (
        "Process only the exact structural scopes in the manifest below. "
        "Do not infer content outside those scopes. Manifest: "
        + json.dumps(scope_manifest, sort_keys=True)
        if scope_manifest
        else "Process the supplied source using the fixed document-understanding schema."
    )
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": f"source{extension}",
                        "file_data": f"data:{request.source.media_type};base64,{encoded}",
                    },
                    {
                        "type": "input_text",
                        "text": scope_instruction,
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "document_understanding_result",
                "strict": True,
                "schema": _structured_output_schema(),
            }
        },
        "metadata": {
            "processing_operation_id": request.processing_operation_id,
            "processing_attempt_id": request.processing_attempt_id,
            "document_version_id": request.source.document_version_id,
            "source_scope_id": request.source.source_scope_id,
            "structural_scope_ids": list(request.source.structural_scope_ids),
        },
    }


def _structured_output_schema() -> dict[str, object]:
    source_location = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["page_number", "bounding_box", "start_offset", "end_offset"],
        "properties": {
            "page_number": {"type": ["integer", "null"], "minimum": 1},
            "bounding_box": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
            },
            "start_offset": {"type": ["integer", "null"], "minimum": 0},
            "end_offset": {"type": ["integer", "null"], "minimum": 0},
        },
    }
    observation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["observation_id", "kind", "order", "text", "state", "source_location"],
        "properties": {
            "observation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "kind": {
                "type": "string",
                "enum": sorted(UNDERSTANDING_OBSERVATION_KINDS),
            },
            "order": {"type": "integer", "minimum": 0},
            "text": {"type": ["string", "null"], "maxLength": MAX_UNDERSTANDING_TEXT_LENGTH},
            "state": {"type": "string", "enum": ["observed", "unknown", "ambiguous", "unreadable"]},
            "source_location": source_location,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["result"],
        "properties": {
            "result": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "pages", "warnings"],
                "properties": {
                    "schema_version": {
                        "type": "string",
                        "const": OPENAI_PROVIDER_RESULT_SCHEMA_VERSION,
                    },
                    "pages": {
                        "type": "array",
                        "maxItems": MAX_UNDERSTANDING_PAGES,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["page_number", "observations"],
                            "properties": {
                                "page_number": {"type": "integer", "minimum": 1},
                                "observations": {
                                    "type": "array",
                                    "maxItems": MAX_UNDERSTANDING_OBSERVATIONS_PER_PAGE,
                                    "items": observation,
                                },
                            },
                        },
                    },
                    "warnings": {
                        "type": "array",
                        "maxItems": 100,
                        "items": {"type": "string", "maxLength": 1_000},
                    },
                },
            }
        },
    }


def _validate_response(
    *, response: object, request: GovernedOpenAIRequest, model: str, latency_ms: int
) -> ValidatedProviderResult:
    if getattr(response, "status", None) == "incomplete":
        raise OpenAIProviderError(
            "provider_output_incomplete",
            retryable=False,
            message="OpenAI returned incomplete structured output.",
        )
    if _response_has_refusal(response):
        raise OpenAIProviderError(
            "provider_refused",
            retryable=False,
            message="OpenAI refused the document-understanding request.",
        )
    output_text = getattr(response, "output_text", None)
    if not isinstance(output_text, str) or not output_text.strip():
        raise OpenAIProviderError(
            "malformed_structured_output",
            retryable=False,
            message="OpenAI returned no structured output.",
        )
    try:
        parsed = json.loads(output_text)
        if not isinstance(parsed, Mapping):
            raise ValueError("structured result is not an object")
        parsed_object = cast(Mapping[str, object], parsed)
        result = _validate_understanding_result(parsed_object.get("result"))
    except (json.JSONDecodeError, ValueError) as error:
        raise OpenAIProviderError(
            "malformed_structured_output",
            retryable=False,
            message="OpenAI returned invalid structured output.",
        ) from error
    usage = getattr(response, "usage", None)
    try:
        usage_result = ProviderUsage(
            input_tokens=_integer_attr(usage, "input_tokens"),
            output_tokens=_integer_attr(usage, "output_tokens"),
            total_tokens=_integer_attr(usage, "total_tokens"),
        )
    except ValidationError as error:
        raise OpenAIProviderError(
            "invalid_provider_usage",
            retryable=False,
            message="OpenAI returned invalid usage metadata.",
        ) from error
    return ValidatedProviderResult(
        provider_response_id=_string_attr(response, "id"),
        provider_request_id=_string_attr(response, "request_id"),
        model=model,
        processing_operation_id=request.processing_operation_id,
        processing_attempt_id=request.processing_attempt_id,
        document_version_id=request.source.document_version_id,
        source_scope_id=request.source.source_scope_id,
        processing_policy_version=request.processing_policy_version,
        prompt_version=request.prompt_version,
        canonical_schema_version=request.canonical_schema_version,
        result=result,
        usage=usage_result,
        latency_ms=latency_ms,
        structural_scope_ids=request.source.structural_scope_ids,
    )


def _provider_status_reason(status_code: int) -> str:
    if status_code == 429:
        return "upstream_rate_limited"
    if status_code >= 500:
        return "upstream_unavailable"
    if status_code in {401, 403}:
        return "upstream_authentication_failed"
    if status_code in {400, 404, 413, 415, 422}:
        return "provider_invalid_request"
    return "openai_rejected"


def _response_has_refusal(response: object) -> bool:
    """Recognize SDK refusal variants without retaining the provider response."""

    output = getattr(response, "output", ())
    if not isinstance(output, list):
        return False
    for item in cast(list[object], output):
        item_type = getattr(item, "type", None)
        if item_type == "refusal":
            return True
        content = getattr(item, "content", ())
        if isinstance(content, list) and any(
            getattr(part, "type", None) == "refusal" for part in cast(list[object], content)
        ):
            return True
    return False


def _validate_understanding_result(value: object) -> dict[str, object]:
    """Validate the provider-owned observation graph before durable persistence."""

    if not isinstance(value, dict):
        raise ValueError("structured result has an invalid schema version")
    result = cast(dict[str, object], value)
    if result.get("schema_version") != OPENAI_PROVIDER_RESULT_SCHEMA_VERSION:
        raise ValueError("structured result has an invalid schema version")
    pages = result.get("pages")
    warnings = result.get("warnings")
    if not isinstance(pages, list) or not isinstance(warnings, list):
        raise ValueError("structured result is incomplete")
    typed_pages = cast(list[object], pages)
    typed_warnings = cast(list[object], warnings)
    if len(typed_pages) > MAX_UNDERSTANDING_PAGES or len(typed_warnings) > 100:
        raise ValueError("structured result exceeds policy limits")
    page_numbers: set[int] = set()
    for page in typed_pages:
        if not isinstance(page, dict):
            raise ValueError("structured result has an invalid page")
        typed_page = cast(dict[str, object], page)
        if set(typed_page) != {"page_number", "observations"}:
            raise ValueError("structured result has an invalid page")
        page_number = typed_page.get("page_number")
        observations = typed_page.get("observations")
        if not isinstance(page_number, int) or page_number < 1 or page_number in page_numbers:
            raise ValueError("structured result has invalid page ordering")
        if not isinstance(observations, list):
            raise ValueError("structured result has invalid observations")
        typed_observations = cast(list[object], observations)
        if len(typed_observations) > MAX_UNDERSTANDING_OBSERVATIONS_PER_PAGE:
            raise ValueError("structured result has invalid observations")
        page_numbers.add(page_number)
        observation_ids: set[str] = set()
        previous_order = -1
        for observation in typed_observations:
            _validate_observation(
                observation=observation,
                page_number=page_number,
                observation_ids=observation_ids,
                previous_order=previous_order,
            )
            previous_order = cast(int, cast(dict[str, object], observation)["order"])
    if not all(isinstance(warning, str) and len(warning) <= 1_000 for warning in typed_warnings):
        raise ValueError("structured result has invalid warnings")
    return result


def _validate_observation(
    *, observation: object, page_number: int, observation_ids: set[str], previous_order: int
) -> None:
    required = {"observation_id", "kind", "order", "text", "state", "source_location"}
    if not isinstance(observation, dict):
        raise ValueError("structured result has an invalid observation")
    typed_observation = cast(dict[str, object], observation)
    if set(typed_observation) != required:
        raise ValueError("structured result has an invalid observation")
    observation_id = typed_observation["observation_id"]
    kind = typed_observation["kind"]
    order = typed_observation["order"]
    text = typed_observation["text"]
    state = typed_observation["state"]
    if (
        not isinstance(observation_id, str)
        or not observation_id
        or observation_id in observation_ids
    ):
        raise ValueError("structured result has duplicate observation identity")
    if not isinstance(order, int) or order < 0 or order <= previous_order:
        raise ValueError("structured result has invalid observation ordering")
    if not isinstance(kind, str) or kind not in UNDERSTANDING_OBSERVATION_KINDS:
        raise ValueError("structured result has invalid observation kind")
    if text is not None and (
        not isinstance(text, str) or len(text) > MAX_UNDERSTANDING_TEXT_LENGTH
    ):
        raise ValueError("structured result has invalid observation text")
    if state not in {"observed", "unknown", "ambiguous", "unreadable"}:
        raise ValueError("structured result has invalid observation state")
    _validate_source_location(typed_observation["source_location"], page_number)
    observation_ids.add(observation_id)


def _validate_source_location(location: object, page_number: int) -> None:
    if location is None:
        return
    required = {"page_number", "bounding_box", "start_offset", "end_offset"}
    if not isinstance(location, dict):
        raise ValueError("structured result has invalid source location")
    typed_location = cast(dict[str, object], location)
    if set(typed_location) != required:
        raise ValueError("structured result has invalid source location")
    location_page = typed_location["page_number"]
    if location_page is not None and location_page != page_number:
        raise ValueError("structured result source location references another page")
    box = typed_location["bounding_box"]
    if box is not None and not isinstance(box, list):
        raise ValueError("structured result has invalid bounding box")
    if box is not None:
        typed_box = cast(list[object], box)
        if len(typed_box) != 4 or not all(isinstance(value, (int, float)) for value in typed_box):
            raise ValueError("structured result has invalid bounding box")
    start = typed_location["start_offset"]
    end = typed_location["end_offset"]
    if (
        (start is not None and (not isinstance(start, int) or start < 0))
        or (end is not None and (not isinstance(end, int) or end < 0))
        or (isinstance(start, int) and isinstance(end, int) and end < start)
    ):
        raise ValueError("structured result has invalid source span")


def _integer_attr(value: object, name: str) -> int | None:
    candidate = getattr(value, name, None)
    return candidate if isinstance(candidate, int) else None


def _string_attr(value: object, name: str) -> str | None:
    candidate = getattr(value, name, None)
    return candidate if isinstance(candidate, str) else None


def provider_error_details(error: OpenAIProviderError) -> dict[str, object]:
    """Produce only safe telemetry dimensions for audit/logging callers."""

    return {
        "reason_code": error.reason,
        "retryable": error.retryable,
        "message": redact_sensitive_fields(error.message),
    }
