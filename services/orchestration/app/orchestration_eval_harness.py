"""Deterministic orchestration eval corpus runner for governed runtime coverage."""

from __future__ import annotations

import json
from uuid import uuid4
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from typing import Literal
from typing import TypedDict
from pathlib import Path
from datetime import date
from contextlib import contextmanager
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.orchestration.app import main as orchestration_main
from shared.determinism.input_hash import canonical_json_dumps
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
import os

from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.config import OrchestrationRuntimeRolloutConfig
from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import clear_income_tax_audit_events
from services.orchestration.app.feature_flags import set_kill_switch
from services.orchestration.app.feature_flags import set_orchestration_flag
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config
from services.orchestration.app.llm_response_generator import build_default_llm_response_generator
from services.orchestration.app.llm_response_generator import LLMResponseGeneratorProtocol
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator
from services.orchestration.app.action_adapter_registry import KnowledgeRouteRepository
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import ActionExecutionEnvelope
from services.orchestration.app.action_execution_envelope import (
    reset_default_action_execution_idempotency_store,
)
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)

EvalCorpusKind = Literal["golden", "adversarial"]
EvalFlow = Literal["decide_only", "execute_single_turn", "followup_decide_only", "followup_execute"]
ResponseGeneratorMode = Literal["plain", "grounded", "invalid_citations"]
AdapterMutationMode = Literal["missing_form_artifact_reference"]

_CORPUS_ROOT = Path("tests/eval")
_EVAL_SUMMARY_CACHE: dict[EvalCorpusKind, OrchestrationEvalSummary] = {}


class OrchestrationEvalExpectedOutcome(TypedDict, total=False):
    """Represent normalized expected eval outcome assertions."""

    http_status: int
    top_level_status: str | None
    decision_status: str | None
    gate_status: str | None
    plan_mode: str | None
    route_id: str | None
    target_service: str | None
    execution_status: str | None
    response_status: str | None
    answer_mode: str | None
    has_citations: bool
    clarification_reason_code: str | None
    error_code: str | None
    reason_code: str | None
    step_services: list[str]


class OrchestrationEvalCaseRequired(TypedDict):
    """Represent required orchestration eval fixture fields."""

    case_id: str
    flow: EvalFlow
    expected: OrchestrationEvalExpectedOutcome


class OrchestrationEvalCase(OrchestrationEvalCaseRequired, total=False):
    """Represent one orchestration eval fixture case."""

    prompt_text: str
    seed_prompt_text: str
    followup_prompt_text: str
    seed_conversation_id: str
    followup_conversation_id: str
    seed_user_id: str
    followup_user_id: str
    response_generator_mode: ResponseGeneratorMode
    runtime_rollout: dict[str, bool]
    orchestration_flags: dict[str, bool]
    kill_switches: dict[str, bool]
    adapter_mutation: AdapterMutationMode


class OrchestrationEvalCaseResult(TypedDict):
    """Represent one deterministic orchestration eval case result."""

    case_id: str
    corpus: EvalCorpusKind
    passed: bool
    replay_match: bool
    audit_event_count: int
    audit_event_types: list[str]
    actual: OrchestrationEvalExpectedOutcome
    expected: OrchestrationEvalExpectedOutcome


class OrchestrationEvalSummary(TypedDict):
    """Represent one deterministic orchestration eval corpus summary."""

    corpus: EvalCorpusKind
    total_cases: int
    passed_cases: int
    failed_cases: int
    case_ids: list[str]
    results: list[OrchestrationEvalCaseResult]


class _KnowledgeRouteStub:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (_knowledge_record(),)

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        normalized_source_ids = {item.lower() for item in source_ids}
        if "knw-ita-15-2" in normalized_source_ids and "income-tax-act-15-2" in anchor_ids:
            return (_knowledge_record(),)
        return ()

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        _ = (query, source_type, tax_domain, start_date, end_date)
        return ()

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        _ = (
            publication_state,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        if source_id not in {None, "KNW-ITA-15-2"}:
            return ()
        return (_knowledge_version_summary(),)


def load_orchestration_eval_cases(corpus: EvalCorpusKind) -> list[OrchestrationEvalCase]:
    """Load deterministic eval cases for one orchestration corpus."""

    case_dir = _CORPUS_ROOT / corpus / "orchestration"
    loaded_cases: list[OrchestrationEvalCase] = []
    for path in sorted(case_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        loaded_cases.append(cast(OrchestrationEvalCase, raw))
    return loaded_cases


def clear_orchestration_eval_summary_cache() -> None:
    """Reset in-process eval summary cache for deterministic test isolation."""

    _EVAL_SUMMARY_CACHE.clear()


def run_orchestration_eval_corpus(*, corpus: EvalCorpusKind) -> OrchestrationEvalSummary:
    """Execute one deterministic orchestration eval corpus and return normalized summary."""

    cached = _EVAL_SUMMARY_CACHE.get(corpus)
    if cached is not None:
        return cast(
            OrchestrationEvalSummary,
            json.loads(canonical_json_dumps(cached)),
        )

    results = [
        run_orchestration_eval_case(corpus=corpus, case=case)
        for case in load_orchestration_eval_cases(corpus)
    ]
    passed_cases = sum(1 for result in results if result["passed"])
    summary: OrchestrationEvalSummary = {
        "corpus": corpus,
        "total_cases": len(results),
        "passed_cases": passed_cases,
        "failed_cases": len(results) - passed_cases,
        "case_ids": [result["case_id"] for result in results],
        "results": results,
    }
    _EVAL_SUMMARY_CACHE[corpus] = cast(
        OrchestrationEvalSummary,
        json.loads(canonical_json_dumps(summary)),
    )
    return cast(
        OrchestrationEvalSummary,
        json.loads(canonical_json_dumps(summary)),
    )


def run_orchestration_eval_case(
    *,
    corpus: EvalCorpusKind,
    case: OrchestrationEvalCase,
) -> OrchestrationEvalCaseResult:
    """Execute one deterministic orchestration eval case with replay verification."""

    first_actual, first_audit_count, first_audit_types = _execute_case_once(case)
    if _use_live_openai_synthesis():
        replay_match = True
    else:
        second_actual, _, _ = _execute_case_once(case)
        replay_match = canonical_json_dumps(first_actual) == canonical_json_dumps(second_actual)
    expected = case["expected"]
    passed = replay_match and canonical_json_dumps(first_actual) == canonical_json_dumps(expected)
    return {
        "case_id": case["case_id"],
        "corpus": corpus,
        "passed": passed,
        "replay_match": replay_match,
        "audit_event_count": first_audit_count,
        "audit_event_types": first_audit_types,
        "actual": first_actual,
        "expected": expected,
    }


def _execute_case_once(
    case: OrchestrationEvalCase,
) -> tuple[OrchestrationEvalExpectedOutcome, int, list[str]]:
    reset_runtime_safety_control_config()
    reset_default_action_execution_idempotency_store()
    clear_income_tax_audit_events()
    try:
        for feature_key, enabled in case.get("orchestration_flags", {}).items():
            set_orchestration_flag(feature_key=feature_key, enabled=enabled)
        for switch_key, enabled in case.get("kill_switches", {}).items():
            set_kill_switch(switch_key=switch_key, enabled=enabled)

        rollout = case.get("runtime_rollout", {})
        app = orchestration_main.create_app(
            knowledge_repository=_KnowledgeRouteStub(),
            llm_response_generator=_build_generator(case.get("response_generator_mode", "plain")),
            conversation_state_store=InMemoryConversationStateStore(),
            conversation_state_protector=LocalAesGcmConversationStateProtector(key=b"a" * 32),
            runtime_rollout_config=OrchestrationRuntimeRolloutConfig(
                response_synthesis_enabled=rollout.get("response_synthesis_enabled", True),
                conversation_continuity_enabled=rollout.get(
                    "conversation_continuity_enabled",
                    True,
                ),
            ),
        )
        with _patched_adapter_mutation(case.get("adapter_mutation")):
            actual = _run_flow(app=app, case=case)
        audit_events = list_income_tax_audit_events()
        audit_event_types = sorted({event["event_type"] for event in audit_events})
        return actual, len(audit_events), audit_event_types
    finally:
        clear_income_tax_audit_events()
        reset_runtime_safety_control_config()


def _run_flow(
    *,
    app: FastAPI,
    case: OrchestrationEvalCase,
) -> OrchestrationEvalExpectedOutcome:
    flow = case["flow"]
    with TestClient(app) as client:
        if flow == "decide_only":
            response = client.post(
                "/v1/orchestration/prompt/decide",
                headers=_orchestration_headers(
                    user_reference=f"user-{case['case_id']}",
                    correlation_id=f"corr-{case['case_id']}-decide",
                ),
                json=_decide_payload(
                    conversation_id=f"conv-{case['case_id']}",
                    prompt_text=_require_case_string(case, "prompt_text"),
                ),
            )
            return _normalize_response(
                cast(dict[str, object], response.json()),
                response.status_code,
            )

        if flow == "execute_single_turn":
            decide_payload = _decide_payload(
                conversation_id=f"conv-{case['case_id']}",
                prompt_text=_require_case_string(case, "prompt_text"),
            )
            decision = client.post(
                "/v1/orchestration/prompt/decide",
                headers=_orchestration_headers(
                    user_reference=f"user-{case['case_id']}",
                    correlation_id=f"corr-{case['case_id']}-decide",
                ),
                json=decide_payload,
            )
            decision_body = _decision_body(decision.json())
            if decision_body.get("selected_route") is None or _optional_string(
                decision_body.get("intent_class")
            ) == "clarification_required":
                return _normalize_response(decision_body, decision.status_code)
            execute = client.post(
                "/v1/orchestration/prompt/execute",
                headers=_orchestration_headers(
                    user_reference=f"user-{case['case_id']}",
                    correlation_id=f"corr-{case['case_id']}-execute",
                ),
                json={
                    **decide_payload,
                    "idempotency_key": f"idem-{case['case_id']}",
                    "intent_class": decision_body["intent_class"],
                    "tax_domain_hint": decision_body["tax_domain_hint"],
                    "decision_id": decision_body["decision_id"],
                    "selected_route": decision_body["selected_route"],
                },
            )
            return _normalize_response(cast(dict[str, object], execute.json()), execute.status_code)

        seed_conversation_id = case.get("seed_conversation_id", f"conv-{case['case_id']}-seed")
        seed_user_id = case.get("seed_user_id", f"user-{case['case_id']}-seed")
        seed_payload = _decide_payload(
            conversation_id=seed_conversation_id,
            prompt_text=_require_case_string(case, "seed_prompt_text"),
        )
        seed_decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers=_orchestration_headers(
                user_reference=seed_user_id,
                correlation_id=f"corr-{case['case_id']}-seed-decide",
            ),
            json=seed_payload,
        )
        seed_decision = _decision_body(seed_decide.json())
        if seed_decision.get("selected_route") is None or _optional_string(
            seed_decision.get("intent_class")
        ) == "clarification_required":
            return _normalize_response(seed_decide.json(), seed_decide.status_code)
        seed_execute = client.post(
            "/v1/orchestration/prompt/execute",
            headers=_orchestration_headers(
                user_reference=seed_user_id,
                correlation_id=f"corr-{case['case_id']}-seed-execute",
            ),
            json={
                **seed_payload,
                "idempotency_key": f"idem-{case['case_id']}-seed",
                "intent_class": seed_decision["intent_class"],
                "tax_domain_hint": seed_decision["tax_domain_hint"],
                "decision_id": seed_decision["decision_id"],
                "selected_route": seed_decision["selected_route"],
            },
        )
        if seed_execute.status_code != 200:
            return _normalize_response(cast(dict[str, object], seed_execute.json()), seed_execute.status_code)

        followup_payload = _decide_payload(
            conversation_id=case.get("followup_conversation_id", seed_conversation_id),
            prompt_text=_require_case_string(case, "followup_prompt_text"),
        )
        followup_decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers=_orchestration_headers(
                user_reference=case.get("followup_user_id", seed_user_id),
                correlation_id=f"corr-{case['case_id']}-followup-decide",
            ),
            json=followup_payload,
        )
        if flow == "followup_decide_only":
            return _normalize_response(
                cast(dict[str, object], followup_decide.json()),
                followup_decide.status_code,
            )

        followup_decision = _decision_body(followup_decide.json())
        if followup_decision.get("selected_route") is None or _optional_string(
            followup_decision.get("intent_class")
        ) == "clarification_required":
            return _normalize_response(followup_decide.json(), followup_decide.status_code)
        execute = client.post(
            "/v1/orchestration/prompt/execute",
            headers=_orchestration_headers(
                user_reference=case.get("followup_user_id", seed_user_id),
                correlation_id=f"corr-{case['case_id']}-followup-execute",
            ),
            json={
                **followup_payload,
                "idempotency_key": f"idem-{case['case_id']}-followup",
                "intent_class": followup_decision["intent_class"],
                "tax_domain_hint": followup_decision["tax_domain_hint"],
                "decision_id": followup_decision["decision_id"],
                "selected_route": followup_decision["selected_route"],
            },
        )
        return _normalize_response(cast(dict[str, object], execute.json()), execute.status_code)


def _normalize_response(
    payload: dict[str, object],
    http_status: int,
) -> OrchestrationEvalExpectedOutcome:
    detail = payload.get("detail")
    errors = payload.get("errors")
    route_id: str | None = None
    target_service: str | None = None
    step_services: list[str] = []
    selected_route = payload.get("selected_route")
    if isinstance(selected_route, dict):
        selected_route_payload = cast(dict[str, object], selected_route)
        route_id = _optional_string(selected_route_payload.get("route_id"))
        target_service = _optional_string(selected_route_payload.get("target_service"))
    step_results = payload.get("step_results")
    if isinstance(step_results, list):
        for raw_step in cast(list[object], step_results):
            if isinstance(raw_step, dict):
                step_payload = cast(dict[str, object], raw_step)
                service = _optional_string(step_payload.get("target_service"))
                if service is not None:
                    step_services.append(service)
    response = payload.get("response")
    result: OrchestrationEvalExpectedOutcome = {
        "http_status": http_status,
        "top_level_status": _optional_string(payload.get("status")),
        "decision_status": _optional_string(payload.get("status"))
        if "decision_id" in payload and "execution_id" not in payload
        else None,
        "gate_status": _optional_string(payload.get("gate_status")),
        "plan_mode": _nested_string(payload.get("plan"), "planning_mode"),
        "route_id": route_id,
        "target_service": target_service,
        "execution_status": _optional_string(payload.get("execution_status")),
        "response_status": _nested_string(response, "status"),
        "answer_mode": _nested_string(response, "answer_mode"),
        "has_citations": _nested_has_items(response, "citations"),
        "clarification_reason_code": _nested_string(payload.get("clarification"), "reason_code"),
        "error_code": None,
        "reason_code": None,
        "step_services": step_services,
    }
    if isinstance(detail, dict):
        detail_payload = cast(dict[str, object], detail)
        result["error_code"] = _optional_string(detail_payload.get("error_code"))
        result["reason_code"] = _optional_string(detail_payload.get("reason_code"))
    elif isinstance(errors, list) and errors:
        first_error = cast(list[object], errors)[0]
        if isinstance(first_error, dict):
            first_error_payload = cast(dict[str, object], first_error)
            result["error_code"] = _optional_string(first_error_payload.get("error_code"))
            result["reason_code"] = _optional_string(first_error_payload.get("reason_code"))
    return result


def _decision_body(payload: object) -> dict[str, object]:
    if isinstance(payload, dict):
        payload_dict = cast(dict[str, object], payload)
        result = payload_dict.get("result")
        if isinstance(result, dict):
            return cast(dict[str, object], result)
        return cast(dict[str, object], payload)
    raise AssertionError("Expected orchestration decision payload to be a mapping.")


def _orchestration_headers(*, user_reference: str, correlation_id: str) -> dict[str, str]:
    """Build the same trusted auth envelope used by protected prompt routes."""

    user_id = uuid5(NAMESPACE_URL, f"orchestration-eval-user:{user_reference}")
    return {
        "X-Correlation-ID": correlation_id,
        "X-Auth-Context": json.dumps(
            {
                "schema_version": "1.0.0",
                "user_id": str(user_id),
                "tenant_id": "pilot_tenant_alpha",
                "role": "IndividualTaxpayer",
                "session_id": str(uuid4()),
                "delegation_context": {
                    "is_delegated": False,
                    "principal_user_id": None,
                    "delegate_user_id": None,
                    "delegation_id": None,
                    "granted_at": None,
                    "revoked_at": None,
                },
            },
            sort_keys=True,
        ),
    }


def _decide_payload(*, conversation_id: str, prompt_text: str) -> dict[str, object]:
    return {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": conversation_id,
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
    }


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _require_case_string(case: OrchestrationEvalCase, field_name: str) -> str:
    value = case.get(field_name)
    if isinstance(value, str) and value:
        return value
    raise AssertionError(f"Eval case '{case['case_id']}' is missing required field '{field_name}'.")


def _nested_string(container: object, key: str) -> str | None:
    if not isinstance(container, dict):
        return None
    container_dict = cast(dict[str, object], container)
    return _optional_string(container_dict.get(key))


def _nested_has_items(container: object, key: str) -> bool:
    if not isinstance(container, dict):
        return False
    container_dict = cast(dict[str, object], container)
    value = container_dict.get(key)
    return isinstance(value, list) and len(cast(list[object], value)) > 0


def _build_generator(mode: ResponseGeneratorMode) -> LLMResponseGeneratorProtocol:
    if mode != "invalid_citations" and _use_live_openai_synthesis():
        return build_default_llm_response_generator(
            knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
        )

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.configured
        assert request_payload.get("model") == config.model
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": "Deterministic orchestration eval answer.",
                        "cited_indices": [99],
                    },
                    sort_keys=True,
                )
            }
        )

    config = load_orchestration_openai_response_synthesis_config()
    return OpenAIResponsesLLMResponseGenerator(
        config=config,
        transport=cast(TransportCallable, transport),
        knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
    )


def _use_live_openai_synthesis() -> bool:
    value = os.getenv("ORCHESTRATION_EVAL_USE_LIVE_OPENAI_SYNTHESIS", "")
    if not value.strip():
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


@contextmanager
def _patched_adapter_mutation(mode: AdapterMutationMode | None) -> Iterator[None]:
    if mode is None:
        yield
        return
    original_dispatch = orchestration_main.dispatch_route_action_request_with_envelope

    if mode == "missing_form_artifact_reference":

        def malformed_forms_dispatch(
            request: ActionExecutionRequest,
            *,
            knowledge_repository: KnowledgeRouteRepository | None = None,
        ) -> ActionExecutionEnvelope:
            envelope = original_dispatch(request, knowledge_repository=knowledge_repository)
            adapter_response = envelope["adapter_response"]
            assert adapter_response is not None
            adapter_response["provider_reference"] = None
            adapter_response["result_payload"] = {
                "status": "generated",
                "form_type": "income_tax_return",
            }
            return envelope

        orchestration_main.dispatch_route_action_request_with_envelope = malformed_forms_dispatch
        try:
            yield
        finally:
            orchestration_main.dispatch_route_action_request_with_envelope = original_dispatch
        return

    yield


def _knowledge_record() -> KnowledgeSearchRecord:
    return KnowledgeSearchRecord(
        source_id="KNW-ITA-15-2",
        title="Income Tax Act (Cap. 470), Section 15(2)",
        url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
        source_type="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        effective_from="1974-01-01",
        effective_to=None,
        tax_year=None,
        anchor_id="income-tax-act-15-2",
        content="Allowable deductions in production of income under section 15(2).",
    )


def _knowledge_version_summary() -> KnowledgeSourceVersionSummaryRecord:
    return KnowledgeSourceVersionSummaryRecord(
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        source_id="KNW-ITA-15-2",
        source_family_id="KNW-ITA-FAMILY",
        title="Income Tax Act (Cap. 470), Section 15(2)",
        source_class="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        publication_state="published",
        source_input_origin="official_source_upload",
        source_version_form="point_in_time_consolidation",
        effective_from="1974-01-01",
        effective_to=None,
        tax_year=None,
        supersedes_source_version_id=None,
        superseded_by_source_version_id=None,
    )
