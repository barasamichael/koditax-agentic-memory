"""Persistence checks for orchestration audit events."""

from __future__ import annotations

import os
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg
from psycopg.abc import Query
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import PersistentOrchestrationAuditEventStore
from services.orchestration.app.audit_events import set_default_orchestration_audit_event_store
from services.orchestration.app.audit_events import reset_default_orchestration_audit_event_store

DATABASE_URL_ENV_VAR = "DATABASE_URL"
MIGRATION_FILE = Path("database/migrations/0019_orchestration_persistence_baseline.sql")


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    database_url = _load_database_url()
    if database_url is None:
        pytest.skip("DATABASE_URL is not set; skipping orchestration audit persistence tests.")
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.Error:
        pytest.skip(
            "DATABASE_URL is not reachable; skipping orchestration audit persistence tests."
        )
    try:
        _ensure_migration_applied(connection=connection)
        yield connection
    finally:
        connection.close()


def test_persistent_audit_event_survives_store_recreation(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentOrchestrationAuditEventStore(database_url=database_url)
    store.clear()
    event = store.append(
        {
            "event_id": "manual-audit-event-001",
            "event_type": "prompt_ingested",
            "event_time": "2026-01-01T00:00:00+00:00",
            "trace_id": "trace-audit-persist-001",
            "correlation_id": "corr-audit-persist-001",
            "tenant_id": "tenant-alpha",
            "user_id": "user-alpha",
            "resource_id": "resource-alpha",
            "status": "accepted",
            "supported_lane_id": None,
            "historical_version_id": None,
            "tax_year": None,
            "context": {"tenant_id": "tenant-alpha", "resource_id": "resource-alpha"},
        }
    )

    recreated = PersistentOrchestrationAuditEventStore(database_url=database_url)
    listed = recreated.list(correlation_id="corr-audit-persist-001")

    assert event["event_id"] == "manual-audit-event-001"
    assert len(listed) == 1
    assert listed[0]["event_id"] == event["event_id"]
    assert listed[0]["tenant_id"] == "tenant-alpha"
    assert listed[0]["resource_id"] == "resource-alpha"


def test_prompt_ingest_and_decide_persist_audit_events_deterministically(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentOrchestrationAuditEventStore(database_url=database_url)
    store.clear()
    set_default_orchestration_audit_event_store(store)
    try:
        client = TestClient(create_app())
        payload = {
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-audit-persist-001",
            "channel": "chat",
            "prompt": {
                "text": (
                    "compute income tax for resident employment lane in tax year 2023 "
                    "under KIT-VER-20230701-A."
                ),
                "format": "plain_text",
            },
        }
        ingest = client.post(
            "/v1/orchestration/prompt/ingest",
            headers={"X-Correlation-ID": "corr-audit-ingest-001"},
            json=payload,
        )
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers={"X-Correlation-ID": "corr-audit-decide-001"},
            json=payload,
        )
        ingest_events = list_income_tax_audit_events(correlation_id="corr-audit-ingest-001")
        decide_events = list_income_tax_audit_events(correlation_id="corr-audit-decide-001")
    finally:
        reset_default_orchestration_audit_event_store()

    assert ingest.status_code == 200
    assert decide.status_code == 200
    assert [event["event_type"] for event in ingest_events] == ["prompt_ingested"]
    assert "prompt_decision_resolved" in [event["event_type"] for event in decide_events]


def test_emit_income_tax_audit_event_is_idempotent_for_identical_payload(
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, _load_database_url())
    store = PersistentOrchestrationAuditEventStore(database_url=database_url)
    store.clear()
    set_default_orchestration_audit_event_store(store)
    try:
        first = emit_income_tax_audit_event(
            event_type="prompt_ingested",
            status="accepted",
            correlation_id="corr-audit-repeat-001",
            trace_id="trace-audit-repeat-001",
            context={"tenant_id": "tenant-alpha", "resource_id": "ingest-repeat-001"},
        )
        second = emit_income_tax_audit_event(
            event_type="prompt_ingested",
            status="accepted",
            correlation_id="corr-audit-repeat-001",
            trace_id="trace-audit-repeat-001",
            context={"tenant_id": "tenant-alpha", "resource_id": "ingest-repeat-001"},
        )
        events = list_income_tax_audit_events(correlation_id="corr-audit-repeat-001")
    finally:
        reset_default_orchestration_audit_event_store()

    assert first["event_id"] == second["event_id"]
    assert len(events) == 1


def _ensure_migration_applied(*, connection: psycopg.Connection) -> None:
    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(cast(Query, sql_text))


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
