"""Bounded polling-loop coverage for Document AI durable work discovery."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Event

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.worker_polling import ProcessingWorkPollingPolicy
from services.document_ai.app.worker_polling import BoundedProcessingWorkPollingLoop
from services.document_ai.app.worker_polling import DocumentAIWorkerPollingController
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.processing_work_discovery import ProcessingWorkCandidate
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


def test_worker_polling_loop_applies_bounded_delays_and_handles_each_candidate() -> None:
    first = _candidate(priority=70)
    second = _candidate(priority=40)
    repository = _Repository([ (first, second), () ])
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=2,
            poll_interval_seconds=3.0,
            empty_queue_backoff_seconds=11.0,
            discovery_failure_backoff_seconds=17.0,
        ),
    )
    delays: list[float] = []

    loop.run_forever(
        stop_event=Event(),
        wait_fn=lambda delay_seconds: _record_and_stop_after_two_waits(
            delays=delays,
            delay_seconds=delay_seconds,
            stop_after=2,
        ),
    )

    assert repository.calls == 2
    assert handoff.candidates == [first, second]
    assert delays == [3.0, 11.0]


def test_worker_polling_loop_recovers_after_a_temporary_discovery_failure() -> None:
    candidate = _candidate(priority=25)
    repository = _Repository([RuntimeError("temporary cockroachdb failure"), (candidate,), ()])
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=1,
            poll_interval_seconds=2.0,
            empty_queue_backoff_seconds=5.0,
            discovery_failure_backoff_seconds=13.0,
        ),
    )
    delays: list[float] = []

    loop.run_forever(
        stop_event=Event(),
        wait_fn=lambda delay_seconds: _record_and_stop_after_two_waits(
            delays=delays,
            delay_seconds=delay_seconds,
            stop_after=3,
        ),
    )

    assert repository.calls == 3
    assert handoff.candidates == [candidate]
    assert delays == [13.0, 2.0, 5.0]


def test_worker_polling_loop_isolates_candidate_handoff_failures() -> None:
    first = _candidate(priority=90)
    second = _candidate(priority=10)
    repository = _Repository([(first, second)])
    handoff = _Handoff(failing_processing_work_item_id=first.processing_work_item_id)
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=2,
            poll_interval_seconds=4.0,
            empty_queue_backoff_seconds=7.0,
            discovery_failure_backoff_seconds=9.0,
        ),
    )

    iteration = loop.run_once()

    assert iteration.outcome == "discovered"
    assert iteration.discovered_candidates == 2
    assert iteration.handed_off_candidates == 1
    assert iteration.claim_lost_candidates == 0
    assert iteration.candidate_failures == 1
    assert handoff.candidates == [second]


def test_worker_polling_loop_counts_clean_claim_losses() -> None:
    first = _candidate(priority=65)
    second = _candidate(priority=15)
    repository = _Repository([(first, second)])
    handoff = _Handoff(lost_processing_work_item_id=first.processing_work_item_id)
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=2,
            poll_interval_seconds=4.0,
            empty_queue_backoff_seconds=7.0,
            discovery_failure_backoff_seconds=9.0,
        ),
    )

    iteration = loop.run_once()

    assert iteration.outcome == "discovered"
    assert iteration.discovered_candidates == 2
    assert iteration.handed_off_candidates == 1
    assert iteration.claim_lost_candidates == 1
    assert iteration.candidate_failures == 0
    assert handoff.candidates == [second]


def test_worker_polling_controller_stops_cleanly() -> None:
    repository = _Repository([()])
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=1,
            poll_interval_seconds=30.0,
            empty_queue_backoff_seconds=30.0,
            discovery_failure_backoff_seconds=30.0,
        ),
    )
    controller = DocumentAIWorkerPollingController(loop=loop)

    controller.start()
    assert repository.started.wait(timeout=1)
    controller.stop()

    assert not controller.is_running
    assert repository.calls >= 1


def test_worker_polling_loop_discovers_real_cockroachdb_candidates(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkDiscoveryRepository(database_url=database_url, max_batch_size=2)
    row = _seed_work_row(database_url=database_url)
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,
        candidate_handoff=handoff,
        policy=ProcessingWorkPollingPolicy(
            batch_size=2,
            poll_interval_seconds=1.0,
            empty_queue_backoff_seconds=1.0,
            discovery_failure_backoff_seconds=1.0,
        ),
    )

    try:
        iteration = loop.run_once()

        assert iteration.outcome == "discovered"
        assert iteration.discovered_candidates >= 1
        assert handoff.candidates
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_worker_polling_loop_uses_empty_queue_backoff_when_no_candidates_exist() -> None:
    repository = _Repository([()])
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,
        policy=ProcessingWorkPollingPolicy(
            batch_size=1,
            poll_interval_seconds=2.0,
            empty_queue_backoff_seconds=19.0,
            discovery_failure_backoff_seconds=23.0,
        ),
    )
    delays: list[float] = []

    loop.run_forever(
        stop_event=Event(),
        wait_fn=lambda delay_seconds: _record_and_stop_after_two_waits(
            delays=delays,
            delay_seconds=delay_seconds,
            stop_after=1,
        ),
    )

    assert handoff.candidates == []
    assert delays == [19.0]


def test_worker_polling_loop_recovers_from_a_temporary_real_database_error(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = _FailOnceThenDiscoverRepository(database_url=database_url)
    row = _seed_work_row(database_url=database_url)
    handoff = _Handoff()
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,  # type: ignore[arg-type]
        candidate_handoff=handoff,  # type: ignore[arg-type]
        policy=ProcessingWorkPollingPolicy(
            batch_size=1,
            poll_interval_seconds=1.0,
            empty_queue_backoff_seconds=1.0,
            discovery_failure_backoff_seconds=1.0,
        ),
    )

    delays: list[float] = []
    try:
        loop.run_forever(
            stop_event=Event(),
            wait_fn=lambda delay_seconds: _record_and_stop_after_two_waits(
                delays=delays,
                delay_seconds=delay_seconds,
                stop_after=2,
            ),
        )

        assert repository.calls == 2
        assert handoff.candidates
        assert delays == [1.0, 1.0]
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


class _Repository:
    def __init__(self, results: list[object]) -> None:
        self._results = results
        self.calls = 0
        self.started = Event()

    def discover_work_candidates(self, *, limit: int) -> tuple[ProcessingWorkCandidate, ...]:
        del limit
        self.calls += 1
        self.started.set()
        if not self._results:
            return ()
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FailOnceThenDiscoverRepository:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url
        self.calls = 0

    def discover_work_candidates(self, *, limit: int) -> tuple[ProcessingWorkCandidate, ...]:
        self.calls += 1
        if self.calls == 1:
            raise psycopg.OperationalError("temporary cockroachdb failure")
        repository = ProcessingWorkDiscoveryRepository(
            database_url=self._database_url,
            max_batch_size=1,
        )
        return repository.discover_work_candidates(limit=limit)


class _Handoff:
    def __init__(
        self,
        *,
        failing_processing_work_item_id: UUID | None = None,
        lost_processing_work_item_id: UUID | None = None,
    ) -> None:
        self._failing_processing_work_item_id = failing_processing_work_item_id
        self._lost_processing_work_item_id = lost_processing_work_item_id
        self.candidates: list[ProcessingWorkCandidate] = []

    def handoff(self, *, candidate: ProcessingWorkCandidate) -> bool:
        if candidate.processing_work_item_id == self._failing_processing_work_item_id:
            raise RuntimeError("candidate_handoff_failed")
        if candidate.processing_work_item_id == self._lost_processing_work_item_id:
            return False
        self.candidates.append(candidate)
        return True


def _candidate(*, priority: int) -> ProcessingWorkCandidate:
    now = datetime.now(UTC).replace(microsecond=0)
    identifier = uuid4()
    return ProcessingWorkCandidate(
        processing_work_item_id=identifier,
        processing_operation_id=uuid4(),
        tenant_id=f"tenant-{identifier.hex[:8]}",
        document_id=uuid4(),
        document_version_id=uuid4(),
        source_artifact_id=uuid4(),
        work_kind="source_inspection",
        operation_kind="source_inspection",
        state="queued",
        priority=priority,
        available_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=10),
        retry_count=0,
        max_attempts=3,
        next_retry_at=None,
        failure_category=None,
    )


def _record_and_stop_after_two_waits(
    *,
    delays: list[float],
    delay_seconds: float,
    stop_after: int,
    stop_event: Event | None = None,
) -> bool:
    delays.append(delay_seconds)
    if len(delays) >= stop_after and stop_event is not None:
        stop_event.set()
    return len(delays) >= stop_after


def _seed_work_row(*, database_url: str) -> dict[str, UUID | str]:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    document_version_id = uuid4()
    source_artifact_id = uuid4()
    processing_operation_id = uuid4()
    processing_work_item_id = uuid4()
    owner_user_id = uuid4()
    created_at = datetime.now(UTC).replace(microsecond=0)
    source_storage_key = f"{tenant_id}/documents/{document_id}/source"
    checksum = "b" * 64

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_documents (
                    document_id, tenant_id, owner_user_id, state, storage_key,
                    uploaded_at, checksum_sha256, size_bytes, content_type
                ) VALUES (%s, %s, %s, 'processing', %s, %s, %s, 1024, 'application/pdf')
                """,
                (
                    document_id,
                    tenant_id,
                    owner_user_id,
                    source_storage_key,
                    created_at,
                    checksum,
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
                ) VALUES (%s, %s, %s, %s, %s, 'application/pdf', 1024, 'active',
                          'verified', %s)
                """,
                (
                    source_artifact_id,
                    tenant_id,
                    document_version_id,
                    source_storage_key,
                    checksum,
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
                    %s, %s, %s, 'source_inspection', 'v1', 'test-worker-v1', 'queued',
                    %s, NULL, %s, '{}'::jsonb
                )
                """,
                (
                    processing_operation_id,
                    tenant_id,
                    document_version_id,
                    created_at,
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
                    %s, %s, %s, 'source_inspection', 'queued', 10, %s, NULL, %s,
                    NULL, 0, NULL, NULL, 'background', 0, 3, NULL, 900,
                    NULL, NULL, NULL, NULL, 0
                )
                """,
                (
                    processing_work_item_id,
                    tenant_id,
                    processing_operation_id,
                    created_at - timedelta(minutes=1),
                    created_at,
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


def _cleanup_work_row(*, database_url: str, row: dict[str, UUID | str]) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM document_ai_processing_work_items WHERE processing_work_item_id = %s",
                (row["processing_work_item_id"],),
            )
            cursor.execute(
                "DELETE FROM document_ai_processing_operations WHERE processing_operation_id = %s",
                (row["processing_operation_id"],),
            )
            cursor.execute(
                "DELETE FROM document_ai_source_artifacts WHERE source_artifact_id = %s",
                (row["source_artifact_id"],),
            )
            cursor.execute(
                "DELETE FROM document_ai_document_versions WHERE document_version_id = %s",
                (row["document_version_id"],),
            )
            cursor.execute(
                "DELETE FROM document_ai_documents WHERE document_id = %s",
                (row["document_id"],),
            )
        connection.commit()
