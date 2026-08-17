"""Live CockroachDB coverage for bounded Document AI processing reconciliation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID
from uuid import uuid4
from datetime import UTC
from datetime import datetime

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.processing_state_reconciler import ProcessingStateReconciler
from services.document_ai.migrations.cockroachdb import runner
from tests.test_document_ai_work_discovery import _seed_work_row
from tests.test_document_ai_processing_claims import _cleanup_work_row

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


def test_processing_state_reconciler_repairs_retry_schedule_and_rebuilds_missing_outbox(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    reconciler = ProcessingStateReconciler(database_url=database_url, batch_size=8)
    ancient = datetime(2000, 1, 1, tzinfo=UTC)
    row = _seed_work_row(
        database_url=database_url,
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        document_state="processing",
        work_state="failed",
        operation_state="queued",
        available_at=ancient,
        created_at=ancient,
        priority=10,
        workload_class="background",
    )
    attempt_id = uuid4()

    try:
        _seed_failed_attempt(
            database_url=database_url,
            tenant_id=str(row["tenant_id"]),
            processing_work_item_id=UUID(str(row["processing_work_item_id"])),
            processing_attempt_id=attempt_id,
        )
        _mark_retryable_failed_state(
            database_url=database_url,
            row=row,
        )

        retry_repaired = reconciler._repair_missing_retry_schedules(limit=1)  # type: ignore[attr-defined]
        outbox_repaired = reconciler._repair_missing_outbox_rows(limit=1)  # type: ignore[attr-defined]
        assert retry_repaired >= 1
        assert outbox_repaired >= 1

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, retry_count, next_retry_at, failure_category
                    FROM document_ai_processing_work_items
                    WHERE processing_work_item_id = %s
                    """,
                    (row["processing_work_item_id"],),
                )
                work_row = cursor.fetchone()
                assert work_row is not None
                assert str(work_row[0]) == "queued"
                assert int(work_row[1]) == 1
                assert work_row[2] is not None
                assert str(work_row[3]) == "upstream_timeout"

                cursor.execute(
                    """
                    SELECT event_type
                    FROM document_ai_processing_outbox
                    WHERE processing_work_item_id = %s
                    ORDER BY created_at ASC
                    """,
                    (row["processing_work_item_id"],),
                )
                event_types = [str(event_row[0]) for event_row in cursor.fetchall()]
        assert any(event_type.startswith("processing.retry.") for event_type in event_types)
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_state_reconciler_leaves_non_retryable_failed_work_untouched(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    reconciler = ProcessingStateReconciler(database_url=database_url, batch_size=4)
    ancient = datetime(2000, 1, 1, tzinfo=UTC)
    row = _seed_work_row(
        database_url=database_url,
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        document_state="processing",
        work_state="failed",
        operation_state="queued",
        available_at=ancient,
        created_at=ancient,
        priority=10,
        workload_class="background",
    )

    try:
        _seed_failed_attempt(
            database_url=database_url,
            tenant_id=str(row["tenant_id"]),
            processing_work_item_id=UUID(str(row["processing_work_item_id"])),
            processing_attempt_id=uuid4(),
        )
        _mark_non_retryable_failed_state(database_url=database_url, row=row)

        repaired = reconciler._repair_missing_retry_schedules(limit=1)  # type: ignore[attr-defined]
        assert repaired == 0

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, retry_count, next_retry_at, failure_category
                    FROM document_ai_processing_work_items
                    WHERE processing_work_item_id = %s
                    """,
                    (row["processing_work_item_id"],),
                )
                work_row = cursor.fetchone()
                assert work_row is not None
                assert str(work_row[0]) == "failed"
                assert int(work_row[1]) == 0
                assert work_row[2] is None
                assert str(work_row[3]) == "validation"

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_outbox
                    WHERE processing_work_item_id = %s
                    """,
                    (row["processing_work_item_id"],),
                )
                assert int(cursor.fetchone()[0]) == 0
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def _seed_failed_attempt(
    *,
    database_url: str,
    tenant_id: str,
    processing_work_item_id: UUID,
    processing_attempt_id: UUID,
) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_processing_attempts (
                    processing_attempt_id, tenant_id, processing_work_item_id,
                    attempt_number, state, started_at, finished_at, error_code,
                    error_detail
                ) VALUES (%s, %s, %s, 1, 'failed', now() - interval '1 minute',
                          now() - interval '30 seconds', %s, %s::jsonb)
                """,
                (
                    processing_attempt_id,
                    tenant_id,
                    processing_work_item_id,
                    "storage_retryable_failure",
                    '{"retry_after_ms": 1000}',
                ),
            )
        connection.commit()


def _mark_retryable_failed_state(*, database_url: str, row: dict[str, UUID | str]) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_ai_processing_work_items
                SET state = 'failed',
                    retry_count = 0,
                    max_attempts = 3,
                    first_attempted_at = NULL,
                    next_retry_at = NULL,
                    failure_category = 'upstream_timeout',
                    dead_lettered_at = NULL,
                    dead_letter_reason = NULL
                WHERE processing_work_item_id = %s
                """,
                (row["processing_work_item_id"],),
            )
        connection.commit()


def _mark_non_retryable_failed_state(*, database_url: str, row: dict[str, UUID | str]) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_ai_processing_work_items
                SET state = 'failed',
                    retry_count = 0,
                    max_attempts = 3,
                    first_attempted_at = NULL,
                    next_retry_at = NULL,
                    failure_category = 'validation',
                    dead_lettered_at = NULL,
                    dead_letter_reason = NULL
                WHERE processing_work_item_id = %s
                """,
                (row["processing_work_item_id"],),
            )
        connection.commit()
