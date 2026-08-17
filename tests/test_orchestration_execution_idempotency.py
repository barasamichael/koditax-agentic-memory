"""Persistence checks for orchestration execution idempotency."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg
from psycopg.abc import Query
from fastapi.testclient import TestClient

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.main import create_app
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionInput
from tests.orchestration_auth_support import orchestration_auth_headers
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.action_execution_store import ActionExecutionStoreError
from services.orchestration.app.action_execution_store import ActionExecutionStoreRecord
from services.orchestration.app.action_execution_store import InMemoryActionExecutionStore
from services.orchestration.app.action_execution_store import PersistentActionExecutionStore
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.action_execution_envelope import (
    set_default_action_execution_idempotency_store,
)
from services.orchestration.app.action_execution_envelope import (
    reset_default_action_execution_idempotency_store,
)
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)

pytestmark = pytest.mark.integration

DATABASE_URL_ENV_VAR = "DATABASE_URL"
MIGRATION_FILE = Path("database/migrations/0019_orchestration_persistence_baseline.sql")


class _KnowledgeRouteStub:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: object | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (
            KnowledgeSearchRecord(
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
            ),
        )

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
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
            source_id,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        return (
            KnowledgeSourceVersionSummaryRecord(
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
            ),
        )

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


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    database_url = _load_database_url()
    if database_url is None:
        pytest.skip("DATABASE_URL is not set; skipping orchestration execution persistence tests.")
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.Error:
        pytest.skip(
            "DATABASE_URL is not reachable; skipping orchestration execution persistence tests."
        )
    try:
        _ensure_migration_applied(connection=connection)
        yield connection
    finally:
        connection.close()


def test_prompt_execute_replays_from_persistent_execution_store(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentActionExecutionStore(database_url=database_url)
    store.clear()
    set_default_action_execution_idempotency_store(store)
    try:
        client = TestClient(_build_knowledge_route_app(), headers=orchestration_auth_headers(user_reference="exec-persist"))
        payload = _build_execution_payload(client)
        first = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-persist-001"},
            json=payload,
        )
        second = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-persist-001"},
            json=payload,
        )
        stored = PersistentActionExecutionStore(database_url=database_url).get(
            cast(str, payload["idempotency_key"])
        )
    finally:
        reset_default_action_execution_idempotency_store()

    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(second.json()) == canonical_json_dumps(first.json())
    assert stored is not None
    assert stored["execution_id"] == first.json()["execution_id"]
    stored_envelope = stored["envelope"]
    stored_plan = cast(dict[str, object], stored_envelope["plan"])
    assert stored_plan["plan_status"] == "planned"


def test_conflicting_prompt_execute_idempotency_fails_canonically(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentActionExecutionStore(database_url=database_url)
    store.clear()
    set_default_action_execution_idempotency_store(store)
    try:
        client = TestClient(_build_knowledge_route_app(), headers=orchestration_auth_headers(user_reference="exec-persist"))
        payload = _build_execution_payload(client)
        first = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-conflict-001"},
            json=payload,
        )
        conflicting = deepcopy(payload)
        conflicting["prompt"] = {
            "text": (
                "Which acts govern VAT in Kenya?"
            ),
            "format": "plain_text",
        }
        second = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-conflict-001"},
            json=conflicting,
        )
    finally:
        reset_default_action_execution_idempotency_store()

    assert first.status_code == 200
    assert second.status_code == 400
    detail = cast(dict[str, object], second.json()["detail"])
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] == "prompt_context_mismatch"
    assert detail["reason_code"] == "prompt_context_mismatch"


def test_conflicting_prompt_execute_idempotency_fails_canonically_in_memory() -> None:
    store = InMemoryActionExecutionStore()
    set_default_action_execution_idempotency_store(store)
    try:
        client = TestClient(_build_knowledge_route_app(), headers=orchestration_auth_headers(user_reference="exec-persist"))
        payload = _build_execution_payload(client)
        first = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-conflict-memory-001"},
            json=payload,
        )
        conflicting = deepcopy(payload)
        conflicting["prompt"] = {
            "text": (
                "Which acts govern VAT in Kenya?"
            ),
            "format": "plain_text",
        }
        second = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-conflict-memory-001"},
            json=conflicting,
        )
    finally:
        reset_default_action_execution_idempotency_store()

    assert first.status_code == 200
    assert second.status_code == 400
    detail = cast(dict[str, object], second.json()["detail"])
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] == "prompt_context_mismatch"
    assert detail["reason_code"] == "prompt_context_mismatch"


def test_persistent_execution_record_survives_store_recreation(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentActionExecutionStore(database_url=database_url)
    store.clear()
    set_default_action_execution_idempotency_store(store)
    try:
        client = TestClient(_build_knowledge_route_app(), headers=orchestration_auth_headers(user_reference="exec-persist"))
        payload = _build_execution_payload(client)
        response = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-recreate-001"},
            json=payload,
        )
        recreated = PersistentActionExecutionStore(database_url=database_url)
        stored = recreated.get(cast(str, payload["idempotency_key"]))
    finally:
        reset_default_action_execution_idempotency_store()

    assert response.status_code == 200
    assert stored is not None
    stored_envelope = stored["envelope"]
    assert stored_envelope["execution_id"] == response.json()["execution_id"]


def test_multi_step_prompt_execute_replays_from_persistent_execution_store(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentActionExecutionStore(database_url=database_url)
    store.clear()
    set_default_action_execution_idempotency_store(store)
    try:
        client = TestClient(_build_knowledge_route_app(), headers=orchestration_auth_headers(user_reference="exec-persist"))
        payload = _build_multi_step_execution_payload(client)
        first = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-multi-step-001"},
            json=payload,
        )
        second = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-exec-multi-step-001"},
            json=payload,
        )
        stored_record = PersistentActionExecutionStore(database_url=database_url).get(
            cast(
                str,
                payload["idempotency_key"],
            )
        )
    finally:
        reset_default_action_execution_idempotency_store()

    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(second.json()) == canonical_json_dumps(first.json())
    assert stored_record is not None
    assert stored_record["execution_id"] == first.json()["execution_id"]


def test_persistent_execution_store_rejects_conflicting_duplicate_fingerprint(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentActionExecutionStore(database_url=database_url)
    store.clear()
    first_record = cast(
        ActionExecutionStoreRecord,
        {
            "execution_id": "exec-conflict-001",
            "idempotency_key": "idem-conflict-001",
            "request_fingerprint": "fingerprint-001",
            "envelope": {
                "execution_id": "exec-conflict-001",
                "idempotency_key": "idem-conflict-001",
                "correlation_id": "corr-conflict-001",
                "request_fingerprint": "fingerprint-001",
                "plan": {
                    "plan_id": "plan-conflict-001",
                    "plan_version": "2.0.0",
                    "plan_status": "planned",
                    "planning_mode": "single_step",
                    "execution_ready": True,
                    "steps": [],
                },
                "action_context": {
                    "action_type": "tax_core_execute_computation",
                    "supported_lane_id": None,
                    "historical_version_id": None,
                    "tax_year": None,
                },
                "execution_status": "resolved",
                "adapter_response": None,
                "mapped_result": {
                    "action_status": "pending",
                    "reason_code": "submission_action_mock_pending",
                    "reason": "pending",
                    "retryable": False,
                    "next_retry_at": None,
                    "provider_reference": None,
                    "correlation_id": "corr-conflict-001",
                    "idempotency_key": "idem-conflict-001",
                    "trace_id": "trace-conflict-001",
                },
                "error": None,
                "trace": {
                    "execution_envelope_id": "exec-conflict-001",
                    "correlation_id": "corr-conflict-001",
                    "trace_id": "trace-conflict-001",
                    "idempotency_key": "idem-conflict-001",
                    "request_fingerprint": "fingerprint-001",
                },
            },
        },
    )
    store.put(first_record)

    with pytest.raises(ActionExecutionStoreError) as error_info:
        store.put(
            {
                **first_record,
                "request_fingerprint": "fingerprint-002",
            }
        )

    assert error_info.value.reason_code == "execution_persistence_conflict"


def test_followup_prompt_execute_replays_deterministically_with_same_conversation_context() -> None:
    execution_store = InMemoryActionExecutionStore()
    conversation_store = InMemoryConversationStateStore()
    set_default_action_execution_idempotency_store(execution_store)
    try:
        app = create_app(
            knowledge_repository=_KnowledgeRouteStub(),
            conversation_state_store=conversation_store,
            conversation_state_protector=_conversation_state_protector(),
            turn_resolver=_ContinuationTestTurnResolver(),
        )
        client = TestClient(app, headers=orchestration_auth_headers(user_reference="exec-persist"))
        initial_decide_payload = {
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-exec-persist-001",
            "channel": "chat",
            "prompt": {
                "text": "What is VAT?",
                "format": "plain_text",
            },
        }
        initial_decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers={"X-Correlation-ID": "corr-followup-idem-seed-decide-001"},
            json=initial_decide_payload,
        )
        assert initial_decide.status_code == 200
        initial_decision = initial_decide.json()
        initial_payload = {
            **initial_decide_payload,
            "idempotency_key": "idem-followup-idem-seed-001",
            "intent_class": initial_decision["intent_class"],
            "tax_domain_hint": initial_decision["tax_domain_hint"],
            "decision_id": initial_decision["decision_id"],
            "selected_route": initial_decision["selected_route"],
        }
        seeded = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-followup-idem-seed-001"},
            json=initial_payload,
        )
        assert seeded.status_code == 200

        followup_decide_payload = {
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-exec-persist-001",
            "channel": "chat",
            "prompt": {
                "text": "Which acts govern it?",
                "format": "plain_text",
            },
        }
        followup_decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers={"X-Correlation-ID": "corr-followup-idem-decide-001"},
            json=followup_decide_payload,
        )
        assert followup_decide.status_code == 200
        decision = followup_decide.json()
        followup_execute_payload = {
            **followup_decide_payload,
            "idempotency_key": "idem-followup-idem-001-v146",
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        }

        first = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-followup-idem-execute-001"},
            json=followup_execute_payload,
        )
        second = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-followup-idem-execute-001"},
            json=followup_execute_payload,
        )
    finally:
        reset_default_action_execution_idempotency_store()

    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first.json()) == canonical_json_dumps(second.json())


def _build_execution_payload(client: TestClient) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-exec-persist-001",
        "channel": "chat",
        "prompt": {
            "text": "What is VAT?",
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-exec-persist-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "idempotency_key": "idem-exec-persist-001",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _build_multi_step_execution_payload(client: TestClient) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-exec-multi-step-001",
        "channel": "chat",
        "prompt": {
            "text": "What is VAT?",
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-exec-multi-step-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "idempotency_key": "idem-exec-multi-step-001",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _ensure_migration_applied(*, connection: psycopg.Connection) -> None:
    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(cast(Query, sql_text))


def _conversation_state_protector() -> LocalAesGcmConversationStateProtector:
    return LocalAesGcmConversationStateProtector(key=b"a" * 32)


def _build_knowledge_route_app():
    return create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        conversation_state_store=InMemoryConversationStateStore(),
        conversation_state_protector=_conversation_state_protector(),
    )


class _ContinuationTestTurnResolver:
    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution:
        has_history = bool(payload.recent_candidates)
        contextualized_prompt = (
            "Which laws govern VAT in Kenya?"
            if "which acts govern it?" in payload.current_prompt.lower()
            else payload.current_prompt
        )
        return ConversationTurnResolution.model_validate(
            {
                "schema_version": "1.0",
                "relationship": "continuation" if has_history else "standalone",
                "operation_mode": "informational",
                "raw_prompt": payload.current_prompt,
                "contextualized_prompt": contextualized_prompt,
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": "vat",
                "retrieval_tax_domain_filter": "vat",
                "jurisdiction_hint": "Kenya",
                "tax_year_hint": None,
                "answerability": "answerable",
                "clarification_reason_code": None,
                "clarification_question": None,
                "required_context_fields": [],
                "provided_context_fields": [],
                "missing_required_context_fields": [],
                "needs_knowledge_retrieval": True,
                "needs_computation": False,
                "needs_external_action": False,
                "needs_artifact_operation": False,
                "referenced_candidate_ids": [
                    payload.recent_candidates[-1].candidate_id
                ]
                if has_history
                else [],
                "resolved_references": [],
                "retained_fields": [],
                "corrected_fields": [],
                "reuse_prior_semantic_facts": has_history,
                "reuse_prior_computation_result": False,
                "reuse_prior_evidence": has_history,
                "reuse_prior_artifact": False,
                "assumptions": [],
                "confidence": 1.0,
                "audit_summary": "deterministic continuation test resolver",
            }
        )


def _load_database_url() -> str | None:
    value = os.getenv(DATABASE_URL_ENV_VAR)
    if value is not None and value.strip():
        return value
    env_file = Path(".env")
    if not env_file.exists():
        return None
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith(f"{DATABASE_URL_ENV_VAR}="):
            continue
        return line.split("=", maxsplit=1)[1].strip().strip("\"'")
    return None
