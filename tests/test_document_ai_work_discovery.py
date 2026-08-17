"""CockroachDB-backed work discovery coverage for Document AI."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.processing_work_discovery import PROCESSING_WORK_DISCOVERY_SQL
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(scope="session")
def cockroach_document_ai_database() -> str:
    database_url = load_document_ai_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for Document AI CockroachDB tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.DocumentAITargetError:
            pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")

    assert runner.main() == 0
    return database_url


def test_discovery_query_uses_the_durable_work_index(cockroach_document_ai_database: str) -> None:
    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"EXPLAIN {PROCESSING_WORK_DISCOVERY_SQL}",
                (["uploaded", "processing", "validated", "active"], 25),
            )
            plan = "\n".join(str(row[0]) for row in cursor.fetchall())

    assert "limit" in plan.lower()
    assert "available_at" in plan
    assert "next_retry_at" in plan


def test_discovery_returns_bounded_deterministic_candidates_with_lineage(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkDiscoveryRepository(database_url=database_url, max_batch_size=25)
    rows: list[dict[str, UUID | str]] = []

    try:
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() - timedelta(minutes=30),
                created_at=_utc_now() - timedelta(minutes=40),
                priority=100,
                workload_class="interactive",
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() - timedelta(minutes=10),
                created_at=_utc_now() - timedelta(minutes=10),
                priority=75,
                workload_class="near_interactive",
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() - timedelta(minutes=10),
                created_at=_utc_now() - timedelta(minutes=5),
                priority=10,
                workload_class="background",
            )
        )

        candidates = repository.discover_work_candidates(limit=25)
        repeated = repository.discover_work_candidates(limit=25)
        candidate_ids = [candidate.processing_work_item_id for candidate in candidates]

        repeated_ids = [candidate.processing_work_item_id for candidate in repeated]
        assert rows[0]["processing_work_item_id"] in candidate_ids
        assert rows[1]["processing_work_item_id"] in candidate_ids
        assert candidate_ids.index(rows[0]["processing_work_item_id"]) < candidate_ids.index(
            rows[1]["processing_work_item_id"]
        )
        assert rows[0]["processing_work_item_id"] in repeated_ids
        assert rows[1]["processing_work_item_id"] in repeated_ids

        first = candidates[candidate_ids.index(rows[0]["processing_work_item_id"])]
        second = candidates[candidate_ids.index(rows[1]["processing_work_item_id"])]
        assert first.tenant_id == str(rows[0]["tenant_id"])
        assert first.document_id == rows[0]["document_id"]
        assert first.document_version_id == rows[0]["document_version_id"]
        assert first.source_artifact_id == rows[0]["source_artifact_id"]
        assert second.tenant_id == str(rows[1]["tenant_id"])
        assert second.priority == 75
        assert second.work_kind == "source_inspection"
        assert second.operation_kind == "source_inspection"

        assert repository.discover_work_candidates(limit=0) == ()
        with pytest.raises(ValueError, match="limit_exceeds_maximum"):
            repository.discover_work_candidates(limit=26)
    finally:
        _cleanup_work_rows(database_url=database_url, rows=rows)


def test_discovery_excludes_ineligible_and_terminal_work_without_mutating_state(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkDiscoveryRepository(database_url=database_url)
    rows: list[dict[str, UUID | str]] = []

    try:
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=6),
                priority=10,
                workload_class="background",
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() + timedelta(hours=1),
                created_at=_utc_now() - timedelta(minutes=4),
                priority=10,
                workload_class="background",
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="leased",
                operation_state="running",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=4),
                leased_until=_utc_now() + timedelta(minutes=20),
                priority=10,
                workload_class="background",
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="succeeded",
                operation_state="succeeded",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=3),
                priority=10,
                workload_class="background",
                completed_at=_utc_now() - timedelta(minutes=1),
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="dead_letter",
                operation_state="failed",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=2),
                priority=10,
                workload_class="background",
                dead_lettered_at=_utc_now() - timedelta(minutes=1),
                completed_at=_utc_now() - timedelta(minutes=1),
            )
        )
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="cancelled",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=1),
                priority=10,
                workload_class="background",
                completed_at=_utc_now() - timedelta(minutes=1),
            )
        )

        before = _load_work_rows(
            database_url=database_url,
            work_ids=tuple(row["processing_work_item_id"] for row in rows),
        )
        candidates = repository.discover_work_candidates(limit=25)
        after = _load_work_rows(
            database_url=database_url,
            work_ids=tuple(row["processing_work_item_id"] for row in rows),
        )

        candidate_ids = [candidate.processing_work_item_id for candidate in candidates]
        assert rows[0]["processing_work_item_id"] in candidate_ids
        assert before == after
    finally:
        _cleanup_work_rows(database_url=database_url, rows=rows)


def test_discovery_defers_retry_eligible_work_until_next_retry_is_due(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkDiscoveryRepository(database_url=database_url)
    rows: list[dict[str, UUID | str]] = []

    try:
        rows.append(
            _seed_work_row(
                database_url=database_url,
                tenant_id=f"tenant-{uuid4().hex[:8]}",
                document_state="processing",
                work_state="queued",
                operation_state="queued",
                available_at=_utc_now() - timedelta(minutes=5),
                created_at=_utc_now() - timedelta(minutes=6),
                priority=10,
                workload_class="background",
            )
        )
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET available_at = now() - interval '1 second',
                        next_retry_at = now() + interval '5 seconds'
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (
                        rows[0]["tenant_id"],
                        rows[0]["processing_work_item_id"],
                    ),
                )
            connection.commit()

        candidates = repository.discover_work_candidates(limit=25)
        assert all(
            candidate.processing_work_item_id != rows[0]["processing_work_item_id"]
            for candidate in candidates
        )
    finally:
        _cleanup_work_rows(database_url=database_url, rows=rows)


def _seed_work_row(
    *,
    database_url: str,
    tenant_id: str,
    document_state: str,
    work_state: str,
    operation_state: str,
    available_at: datetime,
    created_at: datetime,
    priority: int,
    workload_class: str,
    leased_until: datetime | None = None,
    dead_lettered_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, UUID | str]:
    document_id = uuid4()
    document_version_id = uuid4()
    source_artifact_id = uuid4()
    processing_operation_id = uuid4()
    processing_work_item_id = uuid4()
    owner_user_id = uuid4()
    source_storage_key = f"{tenant_id}/documents/{document_id}/source"
    checksum = "a" * 64
    operation_completed_at = (
        completed_at if operation_state in {"succeeded", "failed", "cancelled"} else None
    )

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_documents (
                    document_id, tenant_id, owner_user_id, state, storage_key,
                    uploaded_at, checksum_sha256, size_bytes, content_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    tenant_id,
                    owner_user_id,
                    document_state,
                    source_storage_key,
                    created_at,
                    checksum,
                    1_024,
                    "application/pdf",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_document_versions (
                    document_version_id, tenant_id, document_id, version_number,
                    version_state, created_at
                ) VALUES (%s, %s, %s, 1, 'current', %s)
                """,
                (document_version_id, tenant_id, document_id, created_at),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_source_artifacts (
                    source_artifact_id, tenant_id, document_version_id, storage_key,
                    checksum_sha256, content_type, size_bytes, retention_state,
                    integrity_state, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', 'verified', %s)
                """,
                (
                    source_artifact_id,
                    tenant_id,
                    document_version_id,
                    source_storage_key,
                    checksum,
                    "application/pdf",
                    1_024,
                    created_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_processing_operations (
                    processing_operation_id, tenant_id, document_version_id, operation_kind,
                    processing_policy_version, processor_version, state, requested_at,
                    completed_at, correlation_id, request_payload
                ) VALUES (
                    %s, %s, %s, 'source_inspection', 'v1', 'test-worker-v1', %s, %s,
                    %s, %s, '{}'::jsonb
                )
                """,
                (
                    processing_operation_id,
                    tenant_id,
                    document_version_id,
                    operation_state,
                    created_at,
                    operation_completed_at,
                    f"corr-{processing_operation_id}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_processing_work_items (
                    processing_work_item_id, tenant_id, processing_operation_id, work_kind,
                    state, priority, available_at, leased_until, created_at,
                    current_processing_attempt_id, fencing_token, lease_issued_at,
                    last_heartbeat_at, workload_class, retry_count, max_attempts,
                    first_attempted_at, max_retry_elapsed_seconds, next_retry_at,
                    failure_category, dead_lettered_at, dead_letter_reason,
                    manual_recovery_count
                ) VALUES (
                    %s, %s, %s, 'source_inspection', %s, %s, %s, %s, %s,
                    NULL, 0, NULL, NULL, %s, 0, 3, NULL, 900,
                    NULL, NULL, %s, NULL, 0
                )
                """,
                (
                    processing_work_item_id,
                    tenant_id,
                    processing_operation_id,
                    work_state,
                    priority,
                    available_at,
                    leased_until,
                    created_at,
                    workload_class,
                    dead_lettered_at,
                ),
            )
        connection.commit()

    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "source_artifact_id": source_artifact_id,
        "processing_operation_id": processing_operation_id,
        "processing_work_item_id": processing_work_item_id,
    }


def _load_work_rows(
    *, database_url: str, work_ids: tuple[UUID, ...]
) -> tuple[tuple[object, ...], ...]:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, available_at, leased_until, current_processing_attempt_id,
                       fencing_token, retry_count, max_attempts, dead_lettered_at
                FROM document_ai_processing_work_items
                WHERE processing_work_item_id = ANY(%s)
                ORDER BY processing_work_item_id ASC
                """,
                (list(work_ids),),
            )
            return tuple(cursor.fetchall())


def _cleanup_work_rows(*, database_url: str, rows: list[dict[str, UUID | str]]) -> None:
    if not rows:
        return
    work_ids = [row["processing_work_item_id"] for row in rows]
    operation_ids = [row["processing_operation_id"] for row in rows]
    version_ids = [row["document_version_id"] for row in rows]
    document_ids = [row["document_id"] for row in rows]
    source_ids = [row["source_artifact_id"] for row in rows]

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                (
                    "DELETE FROM document_ai_processing_work_items "
                    "WHERE processing_work_item_id = ANY(%s)"
                ),
                (work_ids,),
            )
            cursor.execute(
                (
                    "DELETE FROM document_ai_processing_operations "
                    "WHERE processing_operation_id = ANY(%s)"
                ),
                (operation_ids,),
            )
            cursor.execute(
                "DELETE FROM document_ai_source_artifacts WHERE source_artifact_id = ANY(%s)",
                (source_ids,),
            )
            cursor.execute(
                "DELETE FROM document_ai_document_versions WHERE document_version_id = ANY(%s)",
                (version_ids,),
            )
            cursor.execute(
                "DELETE FROM document_ai_documents WHERE document_id = ANY(%s)",
                (document_ids,),
            )
        connection.commit()


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def test_discovery_repository_is_read_only_and_does_not_use_skip_locked() -> None:
    source = Path("services/document_ai/app/processing_work_discovery.py").read_text(
        encoding="utf-8"
    )
    assert "discover_work_candidates" in source
    assert "SKIP LOCKED" not in source
    assert "work.leased_until IS NULL" in source
    assert "work.dead_lettered_at IS NULL" in source
