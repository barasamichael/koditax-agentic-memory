"""OpenAI-backed answer synthesis for governed orchestration execution outputs."""

from __future__ import annotations

import re
from copy import deepcopy
import json
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from typing import TypedDict
import hashlib
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Callable
from collections.abc import Iterator

from openai import OpenAI
from openai import APIError
from openai import APIStatusError
from openai import APITimeoutError
from openai import APIConnectionError

from services.orchestration.app.config import SelfCritiqueConfig
from services.orchestration.app.config import load_self_critique_config
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.debug_trace import emit_orchestration_debug
from services.orchestration.app.llm_response_contract import AnswerMode
from services.orchestration.app.llm_response_contract import StructuredAnswerDraft
from services.orchestration.app.llm_response_contract import UnifiedAnswerCitationModel
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisCitation
from services.orchestration.app.llm_synthesis_context import requires_grounded_legal_basis_synthesis
from services.orchestration.app.action_adapter_registry import KnowledgeRouteRepository
from services.orchestration.app.action_adapter_registry import (
    dispatch_synthesis_knowledge_tool_request,
)
from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals
from services.orchestration.app.grounded_explanation_renderer import GroundedExplanationError
from services.orchestration.app.grounded_explanation_renderer import render_grounded_explanation
from services.orchestration.app.synthesis_integrity_constants import MAX_SYNTHESIS_TOOL_ITERATIONS
from services.orchestration.app.request_timer import timed_print

_ANSWER_TEXT_FIELD_PREFIX = re.compile(r'"answer_text"\s*:\s*"')


class SelfCritiqueResult(TypedDict):
    unsupported_claims: list[str]
    contradictions_found: list[str]
    revised_answer: str


class LLMResponseGenerationError(RuntimeError):
    """Represent canonical fail-closed response synthesis failures."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason_code: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason_code = reason_code
        self.context = context


class LLMResponseGeneratorProtocol(Protocol):
    """Describe the narrow orchestration answer-synthesis boundary."""

    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        """Generate one governed user-facing answer from structured orchestration context."""
        ...

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        """Stream one governed user-facing answer from structured orchestration context."""
        ...


@dataclass(frozen=True)
class ResponsesTransportResult:
    payload: dict[str, object]


@dataclass(frozen=True)
class LLMResponseStreamEvent:
    event_type: Literal["delta", "completed"]
    delta: str | None = None
    response: UnifiedAnswerResponseModel | None = None


class _AnswerTextStreamExtractor:
    """Expose only the incremental ``answer_text`` value from strict JSON output."""

    def __init__(self) -> None:
        self._raw_output = ""
        self._value_start: int | None = None
        self._emitted_text = ""

    def push(self, raw_delta: str) -> str:
        """Return the new safe-to-display text represented by one raw JSON delta."""

        self._raw_output += raw_delta
        if self._value_start is None:
            field_match = _ANSWER_TEXT_FIELD_PREFIX.search(self._raw_output)
            if field_match is None:
                return ""
            self._value_start = field_match.end()

        decoded_text = _decode_partial_json_string(self._raw_output[self._value_start :])
        if not decoded_text.startswith(self._emitted_text):
            # Do not surface contract text if an invalid intermediate sequence
            # cannot be reconciled with the already displayed answer.
            return ""
        delta = decoded_text[len(self._emitted_text) :]
        self._emitted_text = decoded_text
        return delta


def _decode_partial_json_string(value: str) -> str:
    """Decode the complete portion of a JSON string whose closing quote may be absent."""

    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == '"':
            break
        if character != "\\":
            decoded.append(character)
            index += 1
            continue

        if index + 1 >= len(value):
            break
        escape = value[index + 1]
        escaped_characters = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape == "u":
            if index + 6 > len(value):
                break
            hex_value = value[index + 2 : index + 6]
            try:
                decoded.append(chr(int(hex_value, 16)))
            except ValueError:
                break
            index += 6
            continue
        escaped = escaped_characters.get(escape)
        if escaped is None:
            break
        decoded.append(escaped)
        index += 2
    return "".join(decoded)


TransportCallable = Callable[
    [OrchestrationOpenAIResponseSynthesisConfig, dict[str, object]],
    ResponsesTransportResult,
]


class _SynthesisToolCall(TypedDict):
    """Represent one validated Responses API function-call request."""

    call_id: str
    name: str
    arguments: dict[str, object]


class _SynthesisToolRuntime(TypedDict):
    """Represent request-scoped controls needed by synthesis retrieval."""

    correlation_id: str
    trace_id: str
    execution_id: str
    tenant_id: str
    user_id: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None


class OpenAIResponsesLLMResponseGenerator:
    """Use OpenAI Responses API to synthesize a governed final answer."""

    def __init__(
        self,
        *,
        config: OrchestrationOpenAIResponseSynthesisConfig,
        critique_config: SelfCritiqueConfig | None = None,
        transport: TransportCallable | None = None,
        knowledge_repository_provider: Callable[[], KnowledgeRouteRepository | None] | None = None,
    ) -> None:
        self._config = config
        self._critique_config = critique_config or load_self_critique_config()
        self._transport = transport or _post_responses_request
        self._knowledge_repository_provider = knowledge_repository_provider

    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        if not self._config.configured:
            raise LLMResponseGenerationError(
                error_code="response_synthesis_unavailable",
                message="OpenAI response synthesis is not configured for orchestration runtime.",
                reason_code="missing_openai_configuration",
            )

        emit_orchestration_debug(
            "SYNTHESIS",
            "generation.start",
            model=self._config.model,
            answer_mode=context["answer_mode"],
            citation_count=len(context["citations"]),
            explanation_item_count=len(context["explanation_items"]),
        )
        last_error: LLMResponseGenerationError | None = None
        total_tool_iterations_used = 0
        for _ in range(self._config.max_retries + 1):
            try:
                timed_print("[SYNTHESIS] About to generate draft")
                draft, effective_context, tool_iterations_used = self._generate_draft(
                    context=context,
                    tool_iteration_limit=(
                        MAX_SYNTHESIS_TOOL_ITERATIONS - total_tool_iterations_used
                    ),
                )
                timed_print(
                    "[SYNTHESIS] Generated draft "
                    f"tool_iterations_used={tool_iterations_used}"
                )
                total_tool_iterations_used += tool_iterations_used
                timed_print("[SYNTHESIS] About to validate draft against context")
                _validate_draft_against_context(draft=draft, context=effective_context)
                timed_print("[SYNTHESIS] Validated draft against context")
                if self._critique_config.configured:
                    timed_print("[SYNTHESIS] About to run self-critique")
                    draft = _apply_self_critique(
                        draft=draft,
                        context=effective_context,
                        critique_config=self._critique_config,
                        synthesis_config=self._config,
                        transport=self._transport,
                    )
                    timed_print("[SYNTHESIS] Completed self-critique")
                timed_print("[SYNTHESIS] About to map citations")
                citations = _map_citations(
                    context=effective_context,
                    cited_indices=draft["cited_indices"],
                )
                timed_print(
                    "[SYNTHESIS] Mapped citations "
                    f"citation_count={len(citations)}"
                )
                timed_print("[SYNTHESIS] About to rewrite citation markers")
                answer_text = _rewrite_citation_markers(
                    answer_text=draft["answer_text"],
                    context=effective_context,
                )
                timed_print("[SYNTHESIS] Rewrote citation markers")
                return UnifiedAnswerResponseModel(
                    status="generated",
                    answer_text=answer_text,
                    answer_mode=effective_context["answer_mode"],
                    citations=citations,
                    source_references=list(effective_context["source_references"]),
                    assumptions=list(effective_context["assumptions"]),
                    warnings=list(effective_context["warnings"]),
                    integrity_signals=ResponseIntegritySignals(
                        unsupported_claims=list(draft.get("unsupported_claims_unresolved", [])),
                        contradictions_found=list(draft.get("contradictions_found", [])),
                        unverified_or_contradicting_user_facts=list(
                            draft["unverified_or_contradicting_user_facts"]
                        ),
                        synthesis_tool_iterations_used=total_tool_iterations_used,
                    ),
                )
            except LLMResponseGenerationError as error:
                emit_orchestration_debug(
                    "SYNTHESIS",
                    "generation.failed",
                    model=self._config.model,
                    answer_mode=context["answer_mode"],
                    exception_type=type(error).__name__,
                    reason_code=error.reason_code,
                    tool_iterations_used=total_tool_iterations_used,
                )
                total_tool_iterations_used += _tool_iterations_from_error(error)
                last_error = error
                if error.reason_code not in {
                    "openai_transport_failure",
                    "openai_timeout",
                }:
                    raise
        assert last_error is not None
        raise last_error

    def _generate_draft(
        self,
        *,
        context: GovernedSynthesisContext,
        tool_iteration_limit: int,
    ) -> tuple[StructuredAnswerDraft, GovernedSynthesisContext, int]:
        """Generate a draft, allowing bounded governed knowledge tool calls."""

        timed_print("[LLM_GENERATOR] About to build responses request payload")
        request_payload = _build_responses_request_payload(
            context=context,
            config=self._config,
        )
        timed_print(
            "[LLM_GENERATOR] Built responses request payload "
            f"has_tools={bool(request_payload.get('tools'))}"
        )
        if not requires_grounded_legal_basis_synthesis(context["answer_mode"]):
            timed_print("[LLM_GENERATOR] About to transport non-grounded synthesis request")
            response = self._transport(self._config, _copy_request_payload(request_payload))
            timed_print("[LLM_GENERATOR] Transported non-grounded synthesis request")
            timed_print("[LLM_GENERATOR] About to parse answer draft")
            draft = _parse_answer_draft(response.payload)
            timed_print("[LLM_GENERATOR] Parsed answer draft")
            return draft, context, 0

        conversation_input = _copy_conversation_input(request_payload["input"])
        effective_context = context
        iterations_used = 0
        while iterations_used < tool_iteration_limit:
            request_payload["input"] = conversation_input
            try:
                timed_print("[LLM_GENERATOR] About to transport grounded synthesis request")
                response = self._transport(self._config, _copy_request_payload(request_payload))
                timed_print("[LLM_GENERATOR] Transported grounded synthesis request")
            except LLMResponseGenerationError as error:
                raise _attach_tool_iterations(error, iterations_used) from error
            timed_print("[TOOL_CALL] About to extract synthesis tool calls")
            tool_calls = _extract_synthesis_tool_calls(response.payload)
            timed_print(
                "[TOOL_CALL] Extracted synthesis tool calls "
                f"tool_call_count={len(tool_calls)}"
            )
            if not tool_calls:
                timed_print("[LLM_GENERATOR] About to parse grounded answer draft")
                draft = _parse_answer_draft(response.payload)
                timed_print("[LLM_GENERATOR] Parsed grounded answer draft")
                return draft, effective_context, iterations_used

            tool_outputs: list[dict[str, object]] = []
            for tool_call in tool_calls:
                timed_print(
                    "[TOOL_CALL] About to dispatch synthesis tool "
                    f"tool_name={tool_call['name']}"
                )
                tool_output = self._dispatch_synthesis_tool(
                    tool_call=tool_call,
                    context=effective_context,
                    iteration_number=iterations_used + 1,
                )
                tool_outputs.append(tool_output)
            timed_print(
                "[TOOL_CALL] Dispatched synthesis tools "
                f"tool_output_count={len(tool_outputs)}"
            )
            timed_print("[GROUNDING] About to extend context with tool outputs")
            effective_context = _extend_context_with_tool_outputs(
                context=effective_context,
                tool_outputs=tool_outputs,
            )
            timed_print(
                "[GROUNDING] Extended context with tool outputs "
                f"citation_count={len(effective_context['citations'])}"
            )
            citation_projection = {
                "explanation_items": effective_context["explanation_items"],
                "citations": effective_context["citations"],
            }
            for tool_call, tool_output in zip(tool_calls, tool_outputs, strict=True):
                tool_output["citation_projection"] = citation_projection
                conversation_input.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call["call_id"],
                        "name": tool_call["name"],
                        "arguments": json.dumps(
                            tool_call["arguments"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
                conversation_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call["call_id"],
                        "output": json.dumps(
                            tool_output,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
            iterations_used += 1

        request_payload.pop("tools", None)
        conversation_input.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "The governed retrieval limit has been reached. Use the evidence and "
                            "updated citation map already supplied to return the best available "
                            "structured answer now; do not request another tool."
                        ),
                    }
                ],
            }
        )
        request_payload["input"] = conversation_input
        try:
            timed_print("[LLM_GENERATOR] About to transport fallback synthesis request")
            response = self._transport(self._config, _copy_request_payload(request_payload))
            timed_print("[LLM_GENERATOR] Transported fallback synthesis request")
        except LLMResponseGenerationError as error:
            raise _attach_tool_iterations(error, iterations_used) from error
        if _extract_synthesis_tool_calls(response.payload):
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI response synthesis requested a tool after the governed limit.",
                reason_code="synthesis_tool_iteration_limit_reached",
            )
        timed_print("[LLM_GENERATOR] About to parse fallback answer draft")
        draft = _parse_answer_draft(response.payload)
        timed_print("[LLM_GENERATOR] Parsed fallback answer draft")
        return draft, effective_context, iterations_used

    def _dispatch_synthesis_tool(
        self,
        *,
        tool_call: _SynthesisToolCall,
        context: GovernedSynthesisContext,
        iteration_number: int,
    ) -> dict[str, object]:
        """Resolve one strict synthesis tool request through the knowledge adapter."""

        runtime = _synthesis_tool_runtime(context)
        repository_provider = self._knowledge_repository_provider
        timed_print(
            "[TOOL_CALL] About to resolve synthesis tool request "
            f"tool_name={tool_call['name']}"
        )
        repository = repository_provider() if repository_provider is not None else None
        adapter_response = dispatch_synthesis_knowledge_tool_request(
            tool_name=tool_call["name"],
            tool_arguments=tool_call["arguments"],
            correlation_id=runtime["correlation_id"],
            trace_id=runtime["trace_id"],
            execution_id=runtime["execution_id"],
            supported_lane_id=runtime["supported_lane_id"],
            historical_version_id=runtime["historical_version_id"],
            tax_year=runtime["tax_year"],
            knowledge_repository=repository,
        )
        timed_print(
            "[TOOL_CALL] Resolved synthesis tool request "
            f"tool_name={tool_call['name']}"
        )
        result_payload = adapter_response.get("result_payload")
        grounded_evidence = (
            result_payload.get("grounded_evidence") if isinstance(result_payload, Mapping) else None
        )
        if adapter_response["adapter_status"] != "accepted" or not isinstance(
            grounded_evidence, list
        ):
            error = adapter_response.get("error") or {}
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="Governed synthesis knowledge tool could not resolve evidence.",
                reason_code=str(error.get("reason_code", "synthesis_tool_dispatch_failed")),
            )
        normalized_evidence: list[dict[str, object]] = []
        for raw_item in cast(list[object], grounded_evidence):
            if isinstance(raw_item, Mapping):
                normalized_evidence.append(dict(cast(Mapping[str, object], raw_item)))
        if not normalized_evidence:
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="Governed synthesis knowledge tool returned no usable evidence.",
                reason_code="insufficient_grounded_evidence",
            )
        timed_print("[TOOL_CALL] About to summarize synthesis tool result")
        result_summary = _summarize_synthesis_tool_result(normalized_evidence)
        timed_print(
            "[TOOL_CALL] Summarized synthesis tool result "
            f"evidence_count={len(normalized_evidence)}"
        )
        emit_income_tax_audit_event(
            event_type="response_synthesis_tool_call_requested",
            status="requested",
            correlation_id=runtime["correlation_id"],
            trace_id=runtime["trace_id"],
            supported_lane_id=runtime["supported_lane_id"],
            historical_version_id=runtime["historical_version_id"],
            tax_year=runtime["tax_year"],
            context={
                "tenant_id": runtime["tenant_id"],
                "user_id": runtime["user_id"],
                "resource_id": runtime["execution_id"],
                "tool_name": tool_call["name"],
                "tool_arguments": tool_call["arguments"],
                "result_summary": result_summary,
                "iteration_number": iteration_number,
                "execution_id": runtime["execution_id"],
            },
        )
        return {
            "tool_name": tool_call["name"],
            "grounded_evidence": normalized_evidence,
            "result_summary": result_summary,
        }

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        if not self._config.configured:
            raise LLMResponseGenerationError(
                error_code="response_synthesis_unavailable",
                message="OpenAI response synthesis is not configured for orchestration runtime.",
                reason_code="missing_openai_configuration",
            )

        emit_orchestration_debug(
            "SYNTHESIS",
            "stream.started",
            model=self._config.model,
            answer_mode=context["answer_mode"],
            citation_count=len(context["citations"]),
            explanation_item_count=len(context["explanation_items"]),
        )
        timed_print("[LLM_GENERATOR] About to build streaming request payload")
        request_payload = _build_responses_request_payload(
            context=context,
            config=self._config,
        )
        timed_print("[LLM_GENERATOR] Built streaming request payload")
        client = _create_openai_client(self._config)
        answer_text_extractor = _AnswerTextStreamExtractor()
        try:
            timed_print("[OPENAI_TRANSPORT] About to open streaming transport")
            with client.responses.stream(
                **cast(Any, request_payload),
                stream_options={"include_obfuscation": False},
            ) as stream:
                for event in stream:
                    if getattr(event, "type", None) != "response.output_text.delta":
                        continue
                    raw_delta = getattr(event, "delta", None)
                    if not isinstance(raw_delta, str):
                        continue
                    answer_delta = answer_text_extractor.push(raw_delta)
                    if answer_delta:
                        yield LLMResponseStreamEvent(
                            event_type="delta",
                            delta=answer_delta,
                        )
                response = stream.get_final_response()
            timed_print("[OPENAI_TRANSPORT] Completed streaming transport")
        except APITimeoutError as error:
            emit_orchestration_debug(
                "SYNTHESIS",
                "stream.failed",
                model=self._config.model,
                answer_mode=context["answer_mode"],
                exception_type=type(error).__name__,
                reason_code="openai_timeout",
            )
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI response synthesis timed out.",
                reason_code="openai_timeout",
            ) from error
        except APIStatusError as error:
            print(str(error))
            emit_orchestration_debug(
                "SYNTHESIS",
                "stream.failed",
                model=self._config.model,
                answer_mode=context["answer_mode"],
                exception_type=type(error).__name__,
                reason_code="openai_transport_failure",
            )
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI response synthesis request failed.",
                reason_code="openai_transport_failure",
                context={"status_code": error.status_code},
            ) from error
        except (APIConnectionError, APIError) as error:
            emit_orchestration_debug(
                "SYNTHESIS",
                "stream.failed",
                model=self._config.model,
                answer_mode=context["answer_mode"],
                exception_type=type(error).__name__,
                reason_code="openai_transport_failure",
            )
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI response synthesis transport failed.",
                reason_code="openai_transport_failure",
            ) from error

        timed_print("[LLM_DRAFT] About to normalize streaming response payload")
        payload = _normalize_sdk_response_payload(response)
        timed_print("[LLM_DRAFT] Normalized streaming response payload")
        timed_print("[LLM_DRAFT] About to parse streaming answer draft")
        draft = _parse_answer_draft(payload)
        timed_print("[LLM_DRAFT] Parsed streaming answer draft")
        _validate_draft_against_context(draft=draft, context=context)
        citations = _map_citations(
            context=context,
            cited_indices=draft["cited_indices"],
        )
        answer_text = _rewrite_citation_markers(
            answer_text=draft["answer_text"],
            context=context,
        )
        yield LLMResponseStreamEvent(
            event_type="completed",
            response=UnifiedAnswerResponseModel(
                status="generated",
                answer_text=answer_text,
                answer_mode=context["answer_mode"],
                citations=citations,
                source_references=list(context["source_references"]),
                assumptions=list(context["assumptions"]),
                warnings=list(context["warnings"]),
                integrity_signals=ResponseIntegritySignals(
                    unverified_or_contradicting_user_facts=list(
                        draft["unverified_or_contradicting_user_facts"]
                    ),
                ),
            ),
        )
        emit_orchestration_debug(
            "SYNTHESIS",
            "stream.completed",
            model=self._config.model,
            answer_mode=context["answer_mode"],
            citation_count=len(citations),
            explanation_item_count=len(context["explanation_items"]),
        )


def build_default_llm_response_generator(
    *,
    knowledge_repository_provider: Callable[[], KnowledgeRouteRepository | None] | None = None,
) -> LLMResponseGeneratorProtocol:
    """Build the default OpenAI-backed response generator for orchestration runtime."""

    return OpenAIResponsesLLMResponseGenerator(
        config=load_orchestration_openai_response_synthesis_config(),
        critique_config=load_self_critique_config(),
        knowledge_repository_provider=knowledge_repository_provider,
    )


def build_failed_unified_answer_response(
    *,
    answer_mode: AnswerMode,
    citations: list[UnifiedAnswerCitationModel] | None = None,
    assumptions: list[str] | None = None,
    warnings: list[str] | None = None,
) -> UnifiedAnswerResponseModel:
    """Build a canonical non-generated response section for degraded-safe fallback."""

    return UnifiedAnswerResponseModel(
        status="failed",
        answer_text=None,
        answer_mode=answer_mode,
        citations=[] if citations is None else citations,
        source_references=[],
        assumptions=[] if assumptions is None else assumptions,
        warnings=[] if warnings is None else warnings,
    )


_GROUNDED_ANSWER_MODES = frozenset({"grounded_knowledge", "compute_plus_grounding"})

_SYSTEM_INSTRUCTION = (
    "You are a Kenyan tax law answer synthesiser. You receive structured evidence from "
    "authoritative Kenyan tax sources (KRA, Kenya Law, KESRA, PwC Kenya) and produce a "
    "precise, user-facing answer formatted in Markdown.\n\n"
    "OUTPUT FORMAT — always produce clean Markdown:\n"
    "• Use ## for section headings, **bold** for key figures and terms.\n"
    "• Use inline citation markers like [1], [2] immediately after each factual claim.\n"
    "• Use bullet lists (-) for enumerating exemptions, rates in brackets, or reliefs.\n"
    "• Bold the direct answer sentence so it reads instantly at a glance.\n"
    "• Use a Markdown table when presenting structured data that has two or more "
    "named columns — for example: tax bands with KES ranges and rates, a comparison "
    "of filing deadlines by taxpayer type, or penalty tiers. Do NOT use a table for "
    "a single-column list, a narrative explanation, or fewer than two rows of data.\n"
    "• Never expose raw JSON, internal labels like '(a)', '(b)', or system field names.\n\n"
    "HARD RULES — never break these:\n"
    "• Only use facts present in the provided evidence. Never invent tax rates, legal "
    "authorities, citation identifiers, or date ranges.\n"
    "• Every factual claim must be followed by its inline citation index [N].\n"
    "• If a computation result is pending, say so clearly — do not estimate.\n"
    "• Return only the required structured response. Its `answer_text` value must contain "
    "Markdown, and must not expose internal response-field names.\n\n"
    "ANSWER QUALITY RULES:\n"
    "• Always state the exact figure (rate, KES amount, percentage) — never describe it vaguely.\n"
    "• If the user asks about exemptions, reliefs, or deductions: enumerate EVERY one found in "
    "the evidence individually with its name and statutory section. Never say 'exemptions may "
    "apply' — list them.\n"
    "• If the user asks about brackets or bands: list EVERY band with its KES range and rate.\n"
    "• If evidence is thin or the answer cannot be established from the provided sources, say so "
    "honestly rather than generating a generic answer.\n"
    "• End with a brief '**Statutory basis:**' line citing the specific Act and section.\n\n"
    # ── NEW BLOCK 1: ANSWER-FIRST ────────────────────────────────────────────
    "ANSWER-FIRST RULE — enforced on every response:\n"
    "• Your first sentence must be the answer itself — not context, not background, "
    "not a restatement of the question.\n"
    "• For yes/no questions: start with 'Yes —' or 'No —' followed immediately by "
    "what that means in practice.\n"
    "• For rate or figure questions: start with the figure. Example: 'The rate is 7.5%, "
    "applied to gross rent received [1].'\n"
    "• For process questions: start with the first action. Example: 'Log into iTax and "
    "navigate to Returns > File Return [1].'\n"
    "• Everything else — explanation, statutory basis, caveats — comes after the answer "
    "is already stated.\n"
    "• Counter-example (fail): 'Under the Income Tax Act CAP 470, the treatment of "
    "losses is...' — this buries the answer. Rewrite: '**No, a loss does not exempt "
    "your client from filing [1].** Under CAP 470...'\n\n"
    # ── NEW BLOCK 2: TEMPORAL VALIDITY ───────────────────────────────────────
    "TEMPORAL VALIDITY RULE:\n"
    "• If your answer draws on a provision that is time-limited (amnesty, waiver window, "
    "transitional provision) or changes annually (Finance Act rates, bands, thresholds), "
    "prepend this tag on its own line before the answer body:\n"
    "  ⚠️ **Time-sensitive:** [provision name] applies until [date]. Verify before advising.\n"
    "• This tag must appear before the answer — not in the detail section, not at the end.\n"
    "• If you are uncertain whether a rate or provision is still current, say so explicitly "
    "rather than stating it as settled fact.\n\n"
    # ── NEW BLOCK 3: NOTICE RESPONSE STRUCTURE ───────────────────────────────
    "NOTICE RESPONSE RULE — applies when the question involves any KRA notice:\n"
    "• Detect notice questions by the presence of: 'notice', 'Agency Notice', 'Demand Notice', "
    "'Assessment', 'Final Notice', 'audit letter', 'enforcement', or similar.\n"
    "• When detected, your response must contain exactly these five labeled fields in this order:\n"
    "  **TYPE:** [Name of notice — one phrase]\n"
    "  **MEANING:** [What this means for the client — 1-2 sentences, plain language]\n"
    "  **ACTION:** [Specific action the agent must take — no vague language]\n"
    "  **DEADLINE:** [Exact deadline, or how to locate it on the notice]\n"
    "  **CONSEQUENCE:** [What happens if no action is taken]\n"
    "• If a field cannot be determined from the available evidence, write: "
    "'[Information needed: state what is missing]' — do not skip the field.\n"
    "• Apply the answer-first rule to notice responses too: TYPE and MEANING come before "
    "any statutory explanation.\n"
)


def _build_structured_input(context: GovernedSynthesisContext) -> str:
    """Build a multi-section plain-text input instead of a flat JSON blob."""

    parts: list[str] = []

    # Section 1 — what the user asked and what mode we're in
    parts.append("=== SECTION 1: USER REQUEST ===")
    parts.append(f"User prompt: {context['prompt_text']}")
    parts.append(f"Answer mode: {context['answer_mode']}")
    parts.append(f"Tax domain: {context['tax_domain_hint']}")

    computation_summary = context.get("computation_summary")
    if computation_summary:
        computation_json = json.dumps(computation_summary, separators=(",", ":"))
        parts.append(f"Computation result: {computation_json}")

    service_result_summary = context.get("service_result_summary")
    if service_result_summary:
        parts.append(f"Service result: {json.dumps(service_result_summary, separators=(',', ':'))}")

    # Section 2 — evidence to use (citation index + excerpt + url for post-processing link rewrite)
    parts.append("\n=== SECTION 2: EVIDENCE TO USE ===")
    parts.append(
        "Use inline citation markers [N] in your answer where N is the number below. "
        "Every factual claim must be followed by its [N] marker."
    )
    citations = context.get("citations") or []
    explanation_items = context.get("explanation_items") or []
    excerpt_by_anchor: dict[str, str] = {}
    for item in explanation_items:
        anchor = item.get("anchor_id")
        text = item.get("explanation_text")
        if isinstance(anchor, str) and isinstance(text, str):
            excerpt_by_anchor[anchor] = text
    if citations:
        for citation in citations:
            idx = citation["citation_index"]
            title = citation["title"]
            url = citation["url"]
            authority = citation["authority_level"]
            temporal = citation["temporal_applicability"]
            excerpt = excerpt_by_anchor.get(citation["anchor_id"], "")
            parts.append(
                f"\n[{idx}] SOURCE: {title}\n"
                f"    URL: {url}\n"
                f"    Authority: {authority} | Temporal: {temporal}\n"
                + (f"    Content: {excerpt}" if excerpt else "    Content: (no excerpt available)")
            )
    else:
        parts.append(
            "No citations available — if you cannot answer from evidence, say so honestly."
        )

    authority_summary = context.get("authority_summary")
    if authority_summary:
        parts.append(f"Authority summary: {json.dumps(authority_summary, separators=(',', ':'))}")

    temporal_applicability = context.get("temporal_applicability")
    if temporal_applicability:
        parts.append(
            f"Temporal applicability: {json.dumps(temporal_applicability, separators=(',', ':'))}"
        )

    # Section 3 — what to say
    parts.append("\n=== SECTION 3: ANSWER INSTRUCTIONS ===")
    parts.append(
        "Write a Markdown answer to the user's question using ONLY the evidence in SECTION 2.\n\n"
        "Required structure:\n"
        "1. **Direct answer** — your FIRST SENTENCE is the answer, written in bold. "
        "State the figure, rule, or yes/no immediately. Do not open with context or a "
        "statutory reference.\n"
        "2. **Temporal flag** — if the answer relies on a time-limited or annually-updated "
        "provision, place the ⚠️ Time-sensitive tag on the line BEFORE the answer body.\n"
        "3. **Notice fields** — if the question involves a KRA notice, output TYPE / MEANING / "
        "ACTION / DEADLINE / CONSEQUENCE as labeled fields before any further explanation.\n"
        "4. **Detail and context** — explain how the figure applies, with inline citations [N] "
        "after every factual claim drawn from SECTION 2.\n"
        "5. **Exemptions and reliefs** — if ANY exemption, relief, or deduction appears in "
        "the evidence, list EVERY one as a bullet with its name and statutory reference. "
        "Never say 'there may be exemptions' — enumerate them.\n"
        "6. **Tax bands/brackets** — if the question is about progressive rates, present "
        "every band in a Markdown table with columns: Band (KES range) | Rate (%). "
        "Use a table here even for a single band — it makes the figure scannable.\n"
        "   Use a Markdown table whenever the answer contains structured multi-column data "
        "(e.g. penalty tiers, deadline comparisons, rate schedules). Do NOT use a table "
        "for a narrative explanation or a single-column list of items.\n"
        "7. **Statutory basis** — close with a short bold line: "
        "'**Statutory basis:** [Act name, section, as referenced in the evidence]'\n\n"
        "Do NOT use the labels (a), (b), (c), (d). Do NOT expose internal field names in "
        "answer_text. Do NOT pad with generic disclaimers not grounded in the evidence.\n\n"
        "POST-GROUNDING FACT GAP REQUIREMENT:\n"
        "If a specific fact you do not have (income, turnover, residency, or filing status) "
        "would change which rule applies or what number results, put its exact identifier in "
        "unverified_or_contradicting_user_facts rather than giving a generic explanation of "
        "the rule. The only permitted identifiers are income, turnover, residency, and "
        "filing_status. Use an empty array only when no such material fact is absent."
    )

    grounding_contradictions = context.get("grounding_contradictions") or []
    if grounding_contradictions:
        parts.append("\n=== GROUNDING CONTRADICTIONS TO ADDRESS ===")
        for finding in grounding_contradictions:
            parts.append(
                "Sources disagree on "
                f"{finding['claim_topic']}: {finding['source_a_id']} states "
                f"{finding['source_a_value']}, {finding['source_b_id']} states "
                f"{finding['source_b_value']}. Address this disagreement explicitly "
                "rather than choosing one silently."
            )

    taxpayer_fact_instructions = context.get("taxpayer_fact_instructions") or []
    if taxpayer_fact_instructions:
        parts.append("\n=== TAXPAYER FACTS TO APPLY OR ADDRESS ===")
        parts.extend(taxpayer_fact_instructions)

    # Section 4 — prior conversation context (optional)
    conversation_context_summary = context.get("conversation_context_summary")
    if conversation_context_summary:
        parts.append("\n=== SECTION 4: PRIOR CONVERSATION CONTEXT ===")
        parts.append(
            json.dumps(
                conversation_context_summary,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    # Section 5 — assumptions and warnings
    assumptions = context.get("assumptions") or []
    warnings = context.get("warnings") or []
    if assumptions or warnings:
        parts.append("\n=== SECTION 5: GOVERNED ASSUMPTIONS AND WARNINGS ===")
        for assumption in assumptions:
            parts.append(f"Assumption: {assumption}")
        for warning in warnings:
            parts.append(f"Warning: {warning}")

    return "\n".join(parts)


def _build_responses_request_payload(
    *,
    context: GovernedSynthesisContext,
    config: OrchestrationOpenAIResponseSynthesisConfig,
) -> dict[str, object]:
    timed_print("[LLM_GENERATOR] About to construct OpenAI request payload")
    assert config.model is not None
    request_payload: dict[str, object] = {
        "model": config.model,
        "temperature": 0.1,
        "instructions": _SYSTEM_INSTRUCTION,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _build_structured_input(context),
                    }
                ],
            }
        ],
        "prompt_cache_key": _build_prompt_cache_key(
            context=context,
            config=config,
        ),
        "store": False,
        "text": {
            "format": _build_synthesis_response_format(),
        },
    }
    if config.reasoning_effort is not None:
        request_payload["reasoning"] = {"effort": config.reasoning_effort}
    if config.service_tier is not None:
        request_payload["service_tier"] = config.service_tier
    if config.prompt_cache_retention is not None:
        request_payload["prompt_cache_retention"] = config.prompt_cache_retention
    if requires_grounded_legal_basis_synthesis(context["answer_mode"]):
        request_payload["tools"] = _build_synthesis_tools()
    timed_print(
        "[LLM_GENERATOR] Constructed OpenAI request payload "
        f"has_tools={bool(request_payload.get('tools'))}"
    )
    return request_payload


def _build_synthesis_tools() -> list[dict[str, object]]:
    """Return the exact bounded knowledge tools available to grounded synthesis."""

    return [
        {
            "type": "function",
            "name": "search_records",
            "strict": True,
            "description": "Search the governed knowledge repository for refined evidence.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                # Strict Responses API schemas require every declared property
                # to be listed in ``required``.  Optional search filters are
                # represented as nullable values so callers can still omit
                # their semantic value without producing an invalid schema.
                "required": [
                    "query",
                    "source_type",
                    "tax_domain",
                    "effective_date",
                ],
                "properties": {
                    "query": {"type": "string"},
                    "source_type": {"type": ["string", "null"]},
                    "tax_domain": {"type": ["string", "null"]},
                    "effective_date": {"type": ["string", "null"], "format": "date"},
                },
            },
        },
        {
            "type": "function",
            "name": "retrieve_records",
            "strict": True,
            "description": "Retrieve governed knowledge records by source or anchor identifier.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_ids", "anchor_ids"],
                "properties": {
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "anchor_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "name": "timeline_search_records",
            "strict": True,
            "description": "Search governed knowledge evidence across a required date range.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "query",
                    "source_type",
                    "tax_domain",
                    "start_date",
                    "end_date",
                ],
                "properties": {
                    "query": {"type": "string"},
                    "source_type": {"type": ["string", "null"]},
                    "tax_domain": {"type": "string"},
                    "start_date": {"type": "string", "format": "date"},
                    "end_date": {"type": "string", "format": "date"},
                },
            },
        },
    ]


def _copy_conversation_input(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI synthesis request did not contain a valid conversation input.",
            reason_code="invalid_synthesis_request_shape",
        )
    copied: list[dict[str, object]] = []
    for item in cast(list[object], value):
        if not isinstance(item, Mapping):
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI synthesis request did not contain a valid conversation input.",
                reason_code="invalid_synthesis_request_shape",
            )
        copied.append(dict(cast(Mapping[str, object], item)))
    return copied


def _copy_request_payload(payload: dict[str, object]) -> dict[str, object]:
    return deepcopy(payload)


def _extract_synthesis_tool_calls(payload: Mapping[str, object]) -> list[_SynthesisToolCall]:
    output = payload.get("output")
    if not isinstance(output, list):
        return []
    calls: list[_SynthesisToolCall] = []
    for raw_item in cast(list[object], output):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast(Mapping[str, object], raw_item)
        if item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(call_id, str) or not call_id:
            raise _invalid_synthesis_tool_call()
        if name not in {"search_records", "retrieve_records", "timeline_search_records"}:
            raise _invalid_synthesis_tool_call()
        if not isinstance(arguments, str):
            raise _invalid_synthesis_tool_call()
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise _invalid_synthesis_tool_call() from error
        if not isinstance(parsed_arguments, dict):
            raise _invalid_synthesis_tool_call()
        calls.append(
            cast(
                _SynthesisToolCall,
                {
                    "call_id": call_id,
                    "name": name,
                    "arguments": parsed_arguments,
                },
            )
        )
    return calls


def _invalid_synthesis_tool_call() -> LLMResponseGenerationError:
    return LLMResponseGenerationError(
        error_code="response_synthesis_failed",
        message="OpenAI response synthesis requested an invalid governed knowledge tool.",
        reason_code="invalid_synthesis_tool_call",
    )


def _attach_tool_iterations(
    error: LLMResponseGenerationError,
    iterations_used: int,
) -> LLMResponseGenerationError:
    error.context = {
        **(error.context or {}),
        "synthesis_tool_iterations_used": iterations_used,
    }
    return error


def _tool_iterations_from_error(error: LLMResponseGenerationError) -> int:
    context = error.context or {}
    iterations_used = context.get("synthesis_tool_iterations_used")
    if isinstance(iterations_used, int) and iterations_used >= 0:
        return iterations_used
    return 0


def _synthesis_tool_runtime(context: GovernedSynthesisContext) -> _SynthesisToolRuntime:
    runtime = context.get("synthesis_tool_runtime")
    if not isinstance(runtime, Mapping):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Governed synthesis tool execution is missing request controls.",
            reason_code="missing_synthesis_tool_runtime",
        )
    required_strings = ("correlation_id", "trace_id", "execution_id", "tenant_id", "user_id")
    if any(
        not isinstance(runtime.get(field), str) or not runtime[field] for field in required_strings
    ):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Governed synthesis tool execution is missing request controls.",
            reason_code="missing_synthesis_tool_runtime",
        )
    tax_year = runtime.get("tax_year")
    if tax_year is not None and not isinstance(tax_year, int):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Governed synthesis tool execution contains invalid request controls.",
            reason_code="invalid_synthesis_tool_runtime",
        )
    return cast(_SynthesisToolRuntime, dict(runtime))


def _extend_context_with_tool_outputs(
    *,
    context: GovernedSynthesisContext,
    tool_outputs: list[dict[str, object]],
) -> GovernedSynthesisContext:
    """Add adapter-projected tool evidence to this call's governed citation universe."""

    additional_evidence: list[dict[str, object]] = []
    for tool_output in tool_outputs:
        evidence = tool_output.get("grounded_evidence")
        if isinstance(evidence, list):
            for raw_item in cast(list[object], evidence):
                if isinstance(raw_item, Mapping):
                    additional_evidence.append(dict(cast(Mapping[str, object], raw_item)))
    combined_evidence = [*context["grounded_evidence"], *additional_evidence]
    try:
        rendered = render_grounded_explanation(grounded_evidence=combined_evidence)
    except GroundedExplanationError as error:
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Governed synthesis tool evidence could not be rendered for citation.",
            reason_code="invalid_synthesis_tool_grounding",
        ) from error
    extended_context = dict(context)
    extended_context["grounded_evidence"] = combined_evidence
    extended_context["explanation_items"] = list(rendered["explanation_items"])
    extended_context["citations"] = cast(list[GovernedSynthesisCitation], rendered["citations"])
    return cast(GovernedSynthesisContext, extended_context)


def _summarize_synthesis_tool_result(grounded_evidence: list[dict[str, object]]) -> str:
    source_ids = [
        str(item["source_id"])
        for item in grounded_evidence
        if isinstance(item.get("source_id"), str)
    ]
    summary = f"{len(grounded_evidence)} governed evidence record(s): {', '.join(source_ids[:5])}"
    return summary[:500]


def _build_synthesis_response_format() -> dict[str, object]:
    """Return the strict Responses API schema for governed answer drafts."""

    return {
        "type": "json_schema",
        "name": "governed_answer_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "answer_text",
                "cited_indices",
                "unverified_or_contradicting_user_facts",
            ],
            "properties": {
                "answer_text": {"type": "string"},
                "cited_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "unverified_or_contradicting_user_facts": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["income", "turnover", "residency", "filing_status"],
                    },
                },
            },
        },
    }


def _apply_self_critique(
    *,
    draft: StructuredAnswerDraft,
    context: GovernedSynthesisContext,
    critique_config: SelfCritiqueConfig,
    synthesis_config: OrchestrationOpenAIResponseSynthesisConfig,
    transport: TransportCallable,
) -> StructuredAnswerDraft:
    """For grounded modes, ask the model to list unsupported claims and strip them.

    Returns the original draft unchanged if the mode is not grounded, if there are
    no citations, or if the critique call fails (non-blocking).
    """
    if context["answer_mode"] not in _GROUNDED_ANSWER_MODES:
        return draft
    citations = context.get("citations") or []
    if not citations:
        return draft

    # Use the dedicated critique config when available; fall back to synthesis config
    # so the call still works if ORCHESTRATION_SELF_CRITIQUE_MODEL is not set.
    if critique_config.configured:
        effective_model = critique_config.model
        effective_transport_config = OrchestrationOpenAIResponseSynthesisConfig(
            api_key=critique_config.api_key,
            model=critique_config.model,
            base_url=critique_config.base_url,
            timeout_seconds=critique_config.timeout_seconds,
            max_retries=critique_config.max_retries,
        )
    else:
        effective_model = synthesis_config.model
        effective_transport_config = synthesis_config

    critique_payload = _build_critique_payload(
        model=effective_model,
        critique_input=_build_critique_input(draft=draft, context=context),
    )

    try:
        timed_print("[SYNTHESIS] About to request self-critique")
        critique_result = _request_self_critique(
            transport=transport,
            transport_config=effective_transport_config,
            payload=critique_payload,
        )
        timed_print("[SYNTHESIS] Requested self-critique")
        if critique_result is None:
            return draft
        attempts_used = 0
        critique_draft = draft
        while critique_result["unsupported_claims"] and attempts_used < critique_config.max_retries:
            attempts_used += 1
            revised_answer = critique_result["revised_answer"].strip()
            if revised_answer:
                critique_draft = cast(
                    StructuredAnswerDraft,
                    {
                        "answer_text": revised_answer,
                        "cited_indices": draft["cited_indices"],
                        "unverified_or_contradicting_user_facts": draft[
                            "unverified_or_contradicting_user_facts"
                        ],
                    },
                )
            followup_payload = _build_critique_payload(
                model=effective_model,
                critique_input=_build_targeted_critique_followup_input(
                    draft=critique_draft,
                    context=context,
                    unsupported_claims=critique_result["unsupported_claims"],
                ),
            )
            followup_result = _request_self_critique(
                transport=transport,
                transport_config=effective_transport_config,
                payload=followup_payload,
            )
            if followup_result is None:
                break
            critique_result = followup_result

        updated_draft: StructuredAnswerDraft = {
            "answer_text": draft["answer_text"],
            "cited_indices": draft["cited_indices"],
            "unverified_or_contradicting_user_facts": draft[
                "unverified_or_contradicting_user_facts"
            ],
        }
        if critique_result["unsupported_claims"]:
            updated_draft["unsupported_claims_unresolved"] = critique_result["unsupported_claims"]
        revised = critique_result["revised_answer"]
        if revised.strip():
            updated_draft["answer_text"] = revised.strip()
        updated_draft["contradictions_found"] = critique_result["contradictions_found"]
        return updated_draft
    except Exception:  # noqa: BLE001 — critique is non-blocking
        return draft


def _build_critique_payload(
    *,
    model: str | None,
    critique_input: str,
) -> dict[str, object]:
    return {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a fact-checking assistant. You will be given a draft answer and a "
                    "list of cited evidence excerpts. Identify every factual claim in the answer "
                    "that is NOT directly supported by the cited excerpts. Return JSON with three "
                    "fields: 'unsupported_claims' (array of strings), 'contradictions_found' "
                    "(array of strings), and 'revised_answer' (the answer with unsupported claims "
                    "removed or qualified as assumptions). If all claims are supported, return "
                    "the original answer unchanged and empty arrays."
                ),
            },
            {"role": "user", "content": critique_input},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "self_critique_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "unsupported_claims",
                        "contradictions_found",
                        "revised_answer",
                    ],
                    "properties": {
                        "unsupported_claims": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "contradictions_found": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "revised_answer": {
                            "type": "string",
                        },
                    },
                },
            },
        },
    }


def _request_self_critique(
    *,
    transport: TransportCallable,
    transport_config: OrchestrationOpenAIResponseSynthesisConfig,
    payload: dict[str, object],
) -> SelfCritiqueResult | None:
    timed_print("[SYNTHESIS] About to transport self-critique request")
    critique_response = transport(transport_config, payload)
    timed_print("[SYNTHESIS] Transported self-critique request")
    critique_text = _extract_output_text(critique_response.payload)
    if critique_text is None:
        return None
    timed_print("[SYNTHESIS] About to parse self-critique response")
    parsed = json.loads(critique_text)
    timed_print("[SYNTHESIS] Parsed self-critique response")
    if not isinstance(parsed, dict):
        return None
    parsed_map = cast(dict[str, object], parsed)
    unsupported_claims = _string_list(parsed_map.get("unsupported_claims"))
    contradictions_found = _string_list(parsed_map.get("contradictions_found"))
    revised_answer = parsed_map.get("revised_answer")
    if unsupported_claims is None or contradictions_found is None:
        return None
    if not isinstance(revised_answer, str):
        return None
    return {
        "unsupported_claims": unsupported_claims,
        "contradictions_found": contradictions_found,
        "revised_answer": revised_answer,
    }


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    normalized: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            return None
        normalized.append(item)
    return normalized


def _build_targeted_critique_followup_input(
    *,
    draft: StructuredAnswerDraft,
    context: GovernedSynthesisContext,
    unsupported_claims: list[str],
) -> str:
    claims = "\n".join(f"- {claim}" for claim in unsupported_claims)
    return (
        _build_critique_input(draft=draft, context=context)
        + "\n\n=== UNSUPPORTED CLAIMS TO RESOLVE ===\n"
        + claims
        + "\n\nRevise the draft by removing or qualifying only these unsupported claims, "
        "then run the same support check again against the cited excerpts."
    )


def _build_critique_input(
    *,
    draft: StructuredAnswerDraft,
    context: GovernedSynthesisContext,
) -> str:
    parts: list[str] = [
        "=== DRAFT ANSWER ===",
        draft["answer_text"],
        "\n=== CITED EXCERPTS ===",
    ]
    explanation_items = context.get("explanation_items") or []
    citations = context.get("citations") or []
    cited_set = set(draft["cited_indices"])
    excerpt_by_anchor: dict[str, str] = {}
    for item in explanation_items:
        anchor = item.get("anchor_id")
        text = item.get("explanation_text")
        if isinstance(anchor, str) and isinstance(text, str):
            excerpt_by_anchor[anchor] = text
    for citation in citations:
        idx = citation["citation_index"]
        if idx not in cited_set:
            continue
        excerpt = excerpt_by_anchor.get(citation["anchor_id"], "")
        parts.append(f"[{idx}] {citation['title']}: {excerpt}")
    if len(parts) == 3:
        parts.append("No excerpts available for cited indices.")
    return "\n".join(parts)


def _post_responses_request(
    config: OrchestrationOpenAIResponseSynthesisConfig,
    request_payload: dict[str, object],
) -> ResponsesTransportResult:
    client = _create_openai_client(config)
    try:
        timed_print("[OPENAI_TRANSPORT] About to send OpenAI request")
        if "messages" in request_payload:
            response = cast(
                object,
                client.chat.completions.create(**cast(Any, request_payload)),
            )
        else:
            response = cast(
                object,
                client.responses.create(**cast(Any, request_payload)),
            )
        timed_print("[OPENAI_TRANSPORT] Sent OpenAI request")
    except APITimeoutError as error:
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis timed out.",
            reason_code="openai_timeout",
        ) from error
    except APIStatusError as error:
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis request failed.",
            reason_code="openai_transport_failure",
            context={"status_code": error.status_code},
        ) from error
    except (APIConnectionError, APIError) as error:
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis transport failed.",
            reason_code="openai_transport_failure",
        ) from error
    timed_print("[LLM_DRAFT] About to normalize OpenAI response payload")
    payload = _normalize_sdk_response_payload(response)
    timed_print("[LLM_DRAFT] Normalized OpenAI response payload")
    return ResponsesTransportResult(payload=payload)


def _create_openai_client(
    config: OrchestrationOpenAIResponseSynthesisConfig,
) -> OpenAI:
    return OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )


def _normalize_sdk_response_payload(response: object) -> dict[str, object]:
    model_dump = getattr(response, "model_dump", None)
    if not callable(model_dump):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis returned an invalid top-level payload.",
            reason_code="malformed_model_response",
        )
    dumped = model_dump(mode="json")
    if not isinstance(dumped, dict):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis returned an invalid top-level payload.",
            reason_code="malformed_model_response",
        )
    payload = cast(dict[str, object], dumped)
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        payload["output_text"] = output_text
    # Extract content from Chat Completions response: choices[0].message.content
    try:
        content = response.choices[0].message.content  # type: ignore[union-attr]
        if isinstance(content, str) and content.strip():
            payload["output_text"] = content
    except (AttributeError, IndexError, TypeError):
        pass
    return payload


def _parse_answer_draft(
    payload: Mapping[str, object],
) -> StructuredAnswerDraft:
    output_text = _extract_output_text(payload)
    if output_text is None:
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis returned no structured text output.",
            reason_code="malformed_model_response",
        )
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        parsed_map = cast(dict[str, object], parsed)
        answer_text = parsed_map.get("answer_text")
        cited_indices = parsed_map.get("cited_indices")
        fact_gaps = _parse_missing_fact_fields(
            parsed_map.get("unverified_or_contradicting_user_facts")
        )
        if isinstance(answer_text, str) and answer_text.strip():
            if (
                isinstance(cited_indices, list)
                and not any(not isinstance(item, int) for item in cast(list[object], cited_indices))
                and fact_gaps is not None
            ):
                return {
                    "answer_text": answer_text.strip(),
                    "cited_indices": list(dict.fromkeys(cast(list[int], cited_indices))),
                    "unverified_or_contradicting_user_facts": fact_gaps,
                }
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="OpenAI response synthesis returned an invalid structured answer draft.",
                reason_code="invalid_synthesis_response_shape",
            )
    raise LLMResponseGenerationError(
        error_code="response_synthesis_failed",
        message="OpenAI response synthesis returned an invalid structured answer draft.",
        reason_code="invalid_synthesis_response_shape",
    )


def _parse_missing_fact_fields(value: object) -> list[str] | None:
    """Validate the bounded identifiers used for post-grounding fact gaps."""

    allowed_fields = {"income", "turnover", "residency", "filing_status"}
    if not isinstance(value, list):
        return None
    fields: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or item not in allowed_fields:
            return None
        if item not in fields:
            fields.append(item)
    return fields


def _extract_output_text(payload: Mapping[str, object]) -> str | None:
    top_level_output = payload.get("output_text")
    if isinstance(top_level_output, str) and top_level_output.strip():
        return top_level_output
    output_items = payload.get("output")
    if not isinstance(output_items, list):
        return None
    collected: list[str] = []
    for raw_item in cast(list[object], output_items):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast(Mapping[str, object], raw_item)
        if item.get("type") != "message":
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for raw_content_item in cast(list[object], content_items):
            if not isinstance(raw_content_item, Mapping):
                continue
            content_item = cast(Mapping[str, object], raw_content_item)
            if content_item.get("type") == "output_text" and isinstance(
                content_item.get("text"), str
            ):
                collected.append(str(content_item["text"]))
    return "".join(collected).strip() or None


def _validate_draft_against_context(
    *,
    draft: StructuredAnswerDraft,
    context: GovernedSynthesisContext,
) -> None:
    if (
        context["answer_mode"] in _GROUNDED_ANSWER_MODES
        and context["citations"]
        and not draft["cited_indices"]
    ):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis omitted required grounded citations.",
            reason_code="malformed_model_response",
        )


def _build_prompt_cache_key(
    *,
    context: GovernedSynthesisContext,
    config: OrchestrationOpenAIResponseSynthesisConfig,
) -> str:
    model_name = config.model or "unknown-model"
    prefix = re.sub(r"[^a-z0-9:_-]+", "-", config.prompt_cache_key_prefix.lower())
    prefix = prefix.strip(":-_") or "orchestration"
    stable_digest = hashlib.sha256(
        f"{prefix}:{model_name}:{context['answer_mode']}".encode()
    ).hexdigest()[:16]
    max_prefix_length = 64 - len(":pc:") - len(stable_digest)
    normalized_prefix = prefix[:max_prefix_length].rstrip(":-_") or "orchestration"
    return f"{normalized_prefix}:pc:{stable_digest}"


def _shorten_citation_title(title: str, max_len: int = 40) -> str:
    """Derive a short, readable label from a raw citation title.

    Strips leading format tags like '[PDF]', drops edition/revision noise
    ('Revised Edition, 2021'), removes trailing authority suffixes like
    '- KRA' or '- Nairobi - KRA', and truncates to max_len chars.
    """
    label = title.strip()
    # Remove leading format tags: [PDF], [DOC], [HTML], etc.
    label = re.sub(r"^\[[\w]+\]\s*", "", label)
    # Drop edition/revision noise before the authority suffix
    label = re.sub(r",?\s+Revised Edition[^-]*", "", label, flags=re.IGNORECASE)
    # Drop trailing authority/location suffixes: "- KRA", "- Nairobi - KRA", etc.
    label = re.sub(
        r"\s*-\s*(KRA|Kenya Law|KESRA|Nairobi|PwC Kenya).*$",
        "",
        label,
        flags=re.IGNORECASE,
    )
    label = label.strip(" -,")
    # If what remains is still generic (all-caps short word like "TAXPAYERS"),
    # prepend "KRA: " to give it context
    if label.isupper() and len(label.split()) <= 2:
        label = f"KRA: {label.title()}"
    if len(label) > max_len:
        label = label[:max_len].rsplit(" ", 1)[0].rstrip(" -,") + "…"
    return label or title[:max_len]


def _rewrite_citation_markers(
    *,
    answer_text: str,
    context: GovernedSynthesisContext,
) -> str:
    """Replace [N] markers with short inline links and append a references section.

    Inline form:  (Source: [Short Title](url))
    Footer form:  ## Sources\n1. [Full Title](url)\n2. ...
    """
    citations_by_index: dict[int, dict[str, str]] = {
        citation["citation_index"]: {
            "title": citation["title"],
            "url": citation["url"],
            "short": _shorten_citation_title(citation["title"]),
        }
        for citation in context["citations"]
    }

    # Track which indices actually appear in the text, in order of first appearance
    seen: dict[int, None] = {}

    def _replace(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        entry = citations_by_index.get(idx)
        if entry is None:
            return match.group(0)
        seen.setdefault(idx, None)
        return f"(Source: [{entry['short']}]({entry['url']}))"

    rewritten = re.sub(r"\[(\d+)\]", _replace, answer_text)

    if not seen:
        return rewritten

    # Append a compact references section listing only cited sources
    ref_lines = ["\n\n---\n**Sources**"]
    for rank, idx in enumerate(seen, start=1):
        entry = citations_by_index[idx]
        ref_lines.append(f"{rank}. [{entry['title']}]({entry['url']})")

    return rewritten + "\n".join(ref_lines)


def _map_citations(
    *,
    context: GovernedSynthesisContext,
    cited_indices: list[int],
) -> list[UnifiedAnswerCitationModel]:
    cited_indices = _normalize_cited_indices(
        cited_indices=cited_indices,
        context=context,
    )
    citations_by_index = {citation["citation_index"]: citation for citation in context["citations"]}
    if any(index not in citations_by_index for index in cited_indices):
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="OpenAI response synthesis referenced citations outside governed evidence.",
            reason_code="invalid_response_citations",
        )
    ordered_indices = [
        citation["citation_index"]
        for citation in context["citations"]
        if citation["citation_index"] in set(cited_indices)
    ]
    return [
        UnifiedAnswerCitationModel.model_validate(citations_by_index[index])
        for index in ordered_indices
    ]


def _normalize_cited_indices(
    *,
    cited_indices: list[int],
    context: GovernedSynthesisContext,
) -> list[int]:
    """Accept either the expected 1-based citation indices or zero-based drafts.

    Some live model responses incorrectly use zero-based indices when the governed
    evidence is numbered from 1. We normalize only when the entire citation set can be
    interpreted as a zero-based projection of the governed citation list. Any mixed,
    invented, or out-of-range references still fail validation later.
    """

    if not cited_indices:
        return cited_indices
    normalized = list(dict.fromkeys(cited_indices))
    citations = context.get("citations") or []
    citation_indices = [
        citation["citation_index"]
        for citation in citations
    ]
    citation_index_set = set(citation_indices)
    if all(index in citation_index_set for index in normalized):
        return normalized
    if (
        citation_indices
        and min(normalized) >= 0
        and max(normalized) < len(citation_indices)
        and any(index == 0 for index in normalized)
    ):
        shifted = [index + 1 for index in normalized]
        if all(index in citation_index_set for index in shifted):
            return shifted
    return normalized
