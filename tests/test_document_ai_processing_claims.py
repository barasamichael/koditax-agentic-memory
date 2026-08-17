"""Live CockroachDB regression coverage for Document AI conditional work claims."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Barrier
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app import persistence_support
from services.document_ai.app.retry_policy import RetryClassifiedFailure
from services.document_ai.app.processing_workers import ProcessingWorkerRepository
from services.document_ai.app.processing_workers import ProcessingFailureDisposition
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository
from services.document_ai.app.processing_work_discovery import ProcessingWorkCandidate

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


def test_processing_work_claim_is_exclusive_across_concurrent_workers(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    barrier = Barrier(2)

    try:
        def _claim(worker_id: str):
            barrier.wait(timeout=10)
            return repository.claim_candidate(candidate=candidate, worker_id=worker_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(_claim, ("worker-a", "worker-b")))

        claims = [result for result in (first, second) if result is not None]
        losses = [result for result in (first, second) if result is None]

        assert len(claims) == 1
        assert len(losses) == 1
        assert claims[0].processing_work_item_id == row["processing_work_item_id"]
        assert claims[0].processing_operation_id == row["processing_operation_id"]
        assert claims[0].tenant_id == row["tenant_id"]
        assert claims[0].fencing_token == 1
        assert claims[0].work_state == "queued"

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, leased_until, current_processing_attempt_id, fencing_token
                    FROM document_ai_processing_work_items
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
                work_row = cursor.fetchone()
                assert work_row is not None
                assert str(work_row[0]) == "leased"
                assert work_row[1] is not None
                assert work_row[2] == claims[0].processing_attempt_id
                assert int(work_row[3]) == 1

                cursor.execute(
                    """
                    SELECT worker_id, fencing_token, attempt_number, state
                    FROM document_ai_processing_attempts
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (row["tenant_id"], claims[0].processing_attempt_id),
                )
                attempt_row = cursor.fetchone()
                assert attempt_row == ("worker-a", 1, 1, "running") or attempt_row == (
                    "worker-b",
                    1,
                    1,
                    "running",
                )
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_work_claim_respects_active_leases_and_reclaims_expired_work(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    try:
        first_claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert first_claim is not None

        active_reclaim = repository.claim_candidate(candidate=candidate, worker_id="worker-b")
        assert active_reclaim is None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET leased_until = now() - interval '1 second'
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
            connection.commit()

        recovered = repository.recover_expired_leases()
        assert recovered >= 1

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, leased_until, current_processing_attempt_id
                    FROM document_ai_processing_work_items
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
                recovered_row = cursor.fetchone()
                assert recovered_row is not None
                assert str(recovered_row[0]) == "queued"
                assert recovered_row[1] is None
                assert recovered_row[2] is None

        reclaimed = repository.claim_candidate(candidate=candidate, worker_id="worker-b")
        assert reclaimed is not None
        assert reclaimed.fencing_token == first_claim.fencing_token + 1
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_work_claim_increments_fencing_token_for_new_ownership_generation(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    try:
        first_claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert first_claim is not None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET state = 'queued', leased_until = NULL,
                        current_processing_attempt_id = NULL
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
            connection.commit()

        second_claim = repository.claim_candidate(candidate=candidate, worker_id="worker-b")
        assert second_claim is not None
        assert second_claim.fencing_token == first_claim.fencing_token + 1
        assert second_claim.processing_attempt_id != first_claim.processing_attempt_id
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_work_claim_reconciles_ambiguous_commit_without_double_claiming(
    monkeypatch: pytest.MonkeyPatch,
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    class _AmbiguousCommitError(psycopg.Error):
        def __init__(self) -> None:
            super().__init__("simulated ambiguous commit")
            self.sqlstate = "40003"

    class _AmbiguousCommitTransaction:
        def __init__(self, inner: object, *, commit_error: BaseException | None) -> None:
            self._inner = inner
            self._commit_error = commit_error

        def __enter__(self) -> object:
            return self._inner.__enter__()  # type: ignore[union-attr]

        def __exit__(
            self,
            exc_type: object | None,
            exc: object | None,
            tb: object | None,
        ) -> bool:
            outcome = self._inner.__exit__(exc_type, exc, tb)  # type: ignore[union-attr]
            if exc_type is None and self._commit_error is not None:
                raise self._commit_error
            return outcome

    class _AmbiguousCommitConnection:
        def __init__(self, inner: object, *, commit_error: BaseException | None) -> None:
            self._inner = inner
            self._commit_error = commit_error

        def transaction(self) -> _AmbiguousCommitTransaction:
            return _AmbiguousCommitTransaction(
                self._inner.transaction(),  # type: ignore[union-attr]
                commit_error=self._commit_error,
            )

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    original_connect = persistence_support.connect_document_ai_database
    commit_state = {"should_raise": True}

    @contextmanager
    def _ambiguous_connection(database_url_value: str):
        with original_connect(database_url_value) as connection:
            commit_error = _AmbiguousCommitError() if commit_state["should_raise"] else None
            commit_state["should_raise"] = False
            yield _AmbiguousCommitConnection(connection, commit_error=commit_error)

    monkeypatch.setattr(persistence_support, "connect_document_ai_database", _ambiguous_connection)

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        assert claim.processing_work_item_id == row["processing_work_item_id"]

        replay = repository.claim_candidate(candidate=candidate, worker_id="worker-b")
        assert replay is None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_attempts
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
                assert cursor.fetchone() == (1,)
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_replayable_checkpoint_and_terminal_commits_are_idempotent(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        assert repository.heartbeat(lease=lease)
        assert repository.checkpoint(
            lease=lease,
            checkpoint_key="phase-one",
            sequence=1,
            payload={"page": 1},
        )
        assert repository.checkpoint(
            lease=lease,
            checkpoint_key="phase-one",
            sequence=1,
            payload={"page": 1},
        )

        latest = repository.latest_checkpoint(
            tenant_id=lease.tenant_id, processing_work_item_id=lease.processing_work_item_id
        )
        assert latest is not None
        assert latest.checkpoint_key == "phase-one"
        assert latest.sequence == 1
        assert latest.payload == {"page": 1}

        assert repository.commit_success(lease=lease, result_reference="result:ok")
        assert repository.commit_success(lease=lease, result_reference="result:ok")
        assert not repository.commit_success(lease=lease, result_reference="result:conflict")
        assert not repository.heartbeat(lease=lease)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, attempt.state, operation.state, operation.result_reference
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (lease.processing_attempt_id, lease.tenant_id, lease.processing_work_item_id),
                )
                terminal_row = cursor.fetchone()
                assert terminal_row == ("succeeded", "succeeded", "succeeded", "result:ok")
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_replayed_failure_commit_is_idempotent_and_rejects_conflicts(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    retryable_failure = RetryClassifiedFailure(
        classification="transient",
        error_code="openai_retryable_failure",
        message="provider timeout",
        reason="upstream_timeout",
        retryable=True,
        details={"retry_after_ms": 10},
    )
    conflicting_failure = RetryClassifiedFailure(
        classification="transient",
        error_code="openai_retryable_failure",
        message="provider timeout",
        reason="upstream_unavailable",
        retryable=True,
        details={"retry_after_ms": 10},
    )

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        disposition = repository.commit_failure(lease=lease, failure=retryable_failure)
        assert disposition == ProcessingFailureDisposition("queued", True)
        replay = repository.commit_failure(lease=lease, failure=retryable_failure)
        assert replay == ProcessingFailureDisposition("queued", True)
        assert repository.commit_failure(lease=lease, failure=conflicting_failure) is None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, attempt.state, operation.state
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (lease.processing_attempt_id, lease.tenant_id, lease.processing_work_item_id),
                )
                terminal_row = cursor.fetchone()
                assert terminal_row == ("queued", "failed", "queued")
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_retry_scheduling_uses_the_real_attempt_number(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    policy_calls: list[int] = []

    class _RecordingRetryPolicy:
        def scheduled_delay_ms(
            self,
            *,
            attempt_count: int,
            retry_after_ms: int | None = None,
            jitter: float = 0.0,
        ) -> int:
            del retry_after_ms, jitter
            policy_calls.append(attempt_count)
            return 100

    try:
        first_claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert first_claim is not None
        first_lease = first_claim.to_lease()
        first_disposition = repository.commit_failure(
            lease=first_lease,
            failure=RetryClassifiedFailure(
                classification="transient",
                error_code="openai_retryable_failure",
                message="provider timeout",
                reason="upstream_timeout",
                retryable=True,
                details={"retry_after_ms": 10},
            ),
            retry_policy=_RecordingRetryPolicy(),
        )
        assert first_disposition == ProcessingFailureDisposition("queued", True)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET available_at = now() - interval '1 second',
                        next_retry_at = now() - interval '1 second'
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (first_lease.tenant_id, first_lease.processing_work_item_id),
                )
            connection.commit()

        second_claim = repository.claim_candidate(candidate=candidate, worker_id="worker-b")
        assert second_claim is not None
        second_lease = second_claim.to_lease()
        second_disposition = repository.commit_failure(
            lease=second_lease,
            failure=RetryClassifiedFailure(
                classification="transient",
                error_code="openai_retryable_failure",
                message="provider timeout",
                reason="upstream_timeout",
                retryable=True,
                details={"retry_after_ms": 10},
            ),
            retry_policy=_RecordingRetryPolicy(),
        )
        assert second_disposition == ProcessingFailureDisposition("queued", True)
        assert policy_calls == [1, 2]
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_recovery_schedules_expired_leases_for_later_retry(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET leased_until = now() - interval '1 second'
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (claim.tenant_id, claim.processing_work_item_id),
                )
            connection.commit()

        recovered = repository.recover_expired_leases()
        assert recovered >= 1

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, work.retry_count, work.available_at, work.next_retry_at,
                           work.failure_category, work.leased_until, work.current_processing_attempt_id,
                           attempt.state, attempt.error_code, operation.state
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (
                        claim.processing_attempt_id,
                        claim.tenant_id,
                        claim.processing_work_item_id,
                    ),
                )
                row_state = cursor.fetchone()

        assert row_state is not None
        assert str(row_state[0]) in {"queued", "failed"}
        assert int(row_state[1]) == 1
        assert row_state[2] is not None
        assert row_state[3] == row_state[2]
        assert str(row_state[4]) == "lease_expired"
        assert row_state[5] is None
        assert row_state[6] is None
        assert str(row_state[7]) == "failed"
        assert str(row_state[8]) == "lease_expired"
        assert str(row_state[9]) in {"queued", "failed"}
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_stale_fence_rejects_heartbeats_checkpoints_and_commits(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET state = 'queued',
                        leased_until = NULL,
                        current_processing_attempt_id = NULL,
                        fencing_token = fencing_token + 1
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (lease.tenant_id, lease.processing_work_item_id),
                )
            connection.commit()

        assert not repository.heartbeat(lease=lease)
        assert not repository.checkpoint(
            lease=lease,
            checkpoint_key="phase-one",
            sequence=1,
            payload={"page": 1},
        )
        assert not repository.commit_success(lease=lease, result_reference="result:ok")
        assert repository.commit_failure(
            lease=lease,
            failure=RetryClassifiedFailure(
                classification="transient",
                error_code="openai_retryable_failure",
                message="provider timeout",
                reason="upstream_timeout",
                retryable=True,
                details={"retry_after_ms": 10},
            ),
        ) is None
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_dead_letter_commit_is_idempotent_and_records_terminal_lineage(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    failure = RetryClassifiedFailure(
        classification="non_retryable",
        error_code="source_inspection_non_retryable_failure",
        message="source inspection could not complete safely.",
        reason="source_inspection_unreadable",
        retryable=False,
        details={"source_artifact_state": "missing"},
    )

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        first = repository.commit_failure(lease=lease, failure=failure)
        replay = repository.commit_failure(lease=lease, failure=failure)

        assert first == ProcessingFailureDisposition("dead_letter", False)
        assert replay == ProcessingFailureDisposition("dead_letter", False)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, work.dead_letter_reason, work.failure_category,
                           attempt.state, attempt.error_code, operation.state,
                           operation.failure_category
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (
                        lease.processing_attempt_id,
                        lease.tenant_id,
                        lease.processing_work_item_id,
                    ),
                )
                work_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT failure_class, reason_code, retry_count, max_attempts,
                           processing_attempt_id, processing_work_item_id,
                           processing_operation_id, correlation_id, error_code, error_detail
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                dead_letter_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                dead_letter_count = cursor.fetchone()

        assert work_row == (
            "dead_letter",
            "source_inspection_unreadable",
            "source_inspection_unreadable",
            "failed",
            "source_inspection_non_retryable_failure",
            "failed",
            "source_inspection_unreadable",
        )
        assert dead_letter_row is not None
        assert dead_letter_row[0] == "non_retryable_failure"
        assert dead_letter_row[1] == "source_inspection_unreadable"
        assert int(dead_letter_row[2]) == 0
        assert int(dead_letter_row[3]) == 3
        assert dead_letter_row[4] == lease.processing_attempt_id
        assert dead_letter_row[5] == lease.processing_work_item_id
        assert dead_letter_row[6] == lease.processing_operation_id
        assert str(dead_letter_row[7]) == lease.correlation_id
        assert dead_letter_row[8] == "source_inspection_non_retryable_failure"
        assert dead_letter_row[9] == {"source_artifact_state": "missing"}
        assert dead_letter_count == (1,)

        discovery = ProcessingWorkDiscoveryRepository(database_url=database_url)
        candidates = discovery.discover_work_candidates(limit=25)
        assert all(candidate.processing_work_item_id != row["processing_work_item_id"] for candidate in candidates)
        assert repository.claim_candidate(candidate=candidate, worker_id="worker-b") is None
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_retry_budget_exhaustion_dead_letters_work_and_records_terminal_reason(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    failure = RetryClassifiedFailure(
        classification="transient",
        error_code="openai_retryable_failure",
        message="provider timeout",
        reason="upstream_timeout",
        retryable=True,
        details={"retry_after_ms": 10},
    )

    try:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET max_attempts = 1
                    WHERE tenant_id = %s AND processing_work_item_id = %s
                    """,
                    (row["tenant_id"], row["processing_work_item_id"]),
                )
            connection.commit()

        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        first = repository.commit_failure(lease=lease, failure=failure)
        replay = repository.commit_failure(lease=lease, failure=failure)

        assert first == ProcessingFailureDisposition("dead_letter", False)
        assert replay == ProcessingFailureDisposition("dead_letter", False)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, work.dead_letter_reason, work.failure_category,
                           attempt.state, attempt.error_code, operation.state,
                           operation.failure_category
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (
                        lease.processing_attempt_id,
                        lease.tenant_id,
                        lease.processing_work_item_id,
                    ),
                )
                work_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT failure_class, reason_code, retry_count, max_attempts,
                           processing_attempt_id, processing_work_item_id,
                           processing_operation_id, correlation_id, error_code, error_detail
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                dead_letter_row = cursor.fetchone()

        assert work_row == (
            "dead_letter",
            "retry_budget_exhausted",
            "upstream_timeout",
            "failed",
            "openai_retryable_failure",
            "failed",
            "retry_budget_exhausted",
        )
        assert dead_letter_row is not None
        assert dead_letter_row[0] == "retry_exhausted"
        assert dead_letter_row[1] == "retry_budget_exhausted"
        assert int(dead_letter_row[2]) == 0
        assert int(dead_letter_row[3]) == 1
        assert dead_letter_row[4] == lease.processing_attempt_id
        assert dead_letter_row[5] == lease.processing_work_item_id
        assert dead_letter_row[6] == lease.processing_operation_id
        assert str(dead_letter_row[7]) == lease.correlation_id
        assert dead_letter_row[8] == "openai_retryable_failure"
        assert dead_letter_row[9] == {"retry_after_ms": 10}
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_dead_letter_transition_is_safe_across_concurrent_committers(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    failure = RetryClassifiedFailure(
        classification="non_retryable",
        error_code="source_inspection_non_retryable_failure",
        message="source inspection could not complete safely.",
        reason="source_inspection_unreadable",
        retryable=False,
        details={"source_artifact_state": "missing"},
    )
    barrier = Barrier(2)

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()

        def _commit() -> ProcessingFailureDisposition | None:
            barrier.wait(timeout=10)
            return repository.commit_failure(lease=lease, failure=failure)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: _commit(), range(2)))

        assert results == [
            ProcessingFailureDisposition("dead_letter", False),
            ProcessingFailureDisposition("dead_letter", False),
        ] or results == [
            ProcessingFailureDisposition("dead_letter", False),
            ProcessingFailureDisposition("dead_letter", False),
        ]

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                assert cursor.fetchone() == (1,)
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def test_processing_worker_dead_letter_persists_across_restart_and_is_excluded_from_recovery(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    discovery = ProcessingWorkDiscoveryRepository(database_url=database_url)
    row = _seed_queued_work_row(database_url=database_url)
    candidate = _candidate_from_row(row)
    failure = RetryClassifiedFailure(
        classification="non_retryable",
        error_code="source_inspection_non_retryable_failure",
        message="source inspection could not complete safely.",
        reason="source_inspection_unreadable",
        retryable=False,
        details={"source_artifact_state": "missing"},
    )

    try:
        claim = repository.claim_candidate(candidate=candidate, worker_id="worker-a")
        assert claim is not None
        lease = claim.to_lease()
        assert repository.commit_failure(lease=lease, failure=failure) == ProcessingFailureDisposition(
            "dead_letter", False
        )

        fresh_repository = ProcessingWorkerRepository(
            database_url=database_url, lease_seconds=60
        )
        assert fresh_repository.recover_expired_leases() == 0
        assert fresh_repository.claim_candidate(candidate=candidate, worker_id="worker-b") is None

        candidates = discovery.discover_work_candidates(limit=25)
        assert all(candidate.processing_work_item_id != row["processing_work_item_id"] for candidate in candidates)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT processing_dead_letter_id, dead_lettered_at, created_at
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                dead_letter_row = cursor.fetchone()
        assert dead_letter_row is not None
        assert isinstance(dead_letter_row[1], datetime)
        assert isinstance(dead_letter_row[2], datetime)
    finally:
        _cleanup_work_row(database_url=database_url, row=row)


def _candidate_from_row(row: dict[str, UUID | str]) -> ProcessingWorkCandidate:
    now = datetime.now(UTC).replace(microsecond=0)
    return ProcessingWorkCandidate(
        processing_work_item_id=row["processing_work_item_id"],
        processing_operation_id=row["processing_operation_id"],
        tenant_id=str(row["tenant_id"]),
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        source_artifact_id=row["source_artifact_id"],
        work_kind="source_inspection",
        operation_kind="source_inspection",
        state="queued",
        priority=10,
        available_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=10),
        retry_count=0,
        max_attempts=3,
        next_retry_at=None,
        failure_category=None,
    )


def _seed_queued_work_row(*, database_url: str) -> dict[str, UUID | str]:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    document_version_id = uuid4()
    source_artifact_id = uuid4()
    processing_operation_id = uuid4()
    processing_work_item_id = uuid4()
    owner_user_id = uuid4()
    created_at = datetime.now(UTC).replace(microsecond=0)
    source_storage_key = f"{tenant_id}/documents/{document_id}/source"
    checksum = "c" * 64

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
    for _attempt in range(3):
        try:
            with psycopg.connect(database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE document_ai_processing_work_items
                        SET current_processing_attempt_id = NULL
                        WHERE processing_work_item_id = %s
                        """,
                        (row["processing_work_item_id"],),
                    )
                    cursor.execute(
                        """
                        DELETE FROM document_ai_processing_checkpoints
                        WHERE processing_attempt_id IN (
                            SELECT processing_attempt_id
                            FROM document_ai_processing_attempts
                            WHERE processing_work_item_id = %s
                        )
                        """,
                        (row["processing_work_item_id"],),
                    )
                    cursor.execute(
                        "DELETE FROM document_ai_processing_outbox WHERE processing_work_item_id = %s",
                        (row["processing_work_item_id"],),
                    )
                    cursor.execute(
                        """
                        DELETE FROM document_ai_processing_dead_letters
                        WHERE tenant_id = %s AND processing_work_item_id = %s
                        """,
                        (row["tenant_id"], row["processing_work_item_id"]),
                    )
                    cursor.execute(
                        "DELETE FROM document_ai_processing_attempts WHERE processing_work_item_id = %s",
                        (row["processing_work_item_id"],),
                    )
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
            return
        except psycopg.errors.SerializationFailure:
            continue
