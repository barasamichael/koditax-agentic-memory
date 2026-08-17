"""Bounded polling loop for Document AI durable work discovery.

The loop is intentionally narrow: it discovers advisory candidates from
CockroachDB, hands each candidate to a later claim boundary, and applies
bounded backoff between iterations.  It does not claim, lease, or process
work by itself.
"""

from __future__ import annotations

from typing import Literal
from typing import Protocol
from typing import runtime_checkable
import logging
from threading import Event
from threading import Thread
from dataclasses import dataclass
from collections.abc import Callable

from services.document_ai.app.config import get_document_ai_worker_poll_interval_seconds
from services.document_ai.app.config import get_document_ai_work_discovery_max_batch_size
from services.document_ai.app.config import get_document_ai_worker_empty_queue_backoff_seconds
from services.document_ai.app.config import get_document_ai_worker_discovery_failure_backoff_seconds
from services.document_ai.app.processing_work_discovery import ProcessingWorkCandidate
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository

ProcessingWorkPollingOutcome = Literal["discovered", "empty", "discovery_failed"]


@runtime_checkable
class ProcessingWorkCandidateHandoffProtocol(Protocol):
    """Define the future claim boundary used by the polling loop."""

    def handoff(self, *, candidate: ProcessingWorkCandidate) -> bool:
        """Accept one advisory candidate and return whether ownership was won."""

        ...


@dataclass(frozen=True)
class ProcessingWorkPollingPolicy:
    """Represent bounded worker polling cadence and batch settings."""

    batch_size: int
    poll_interval_seconds: float
    empty_queue_backoff_seconds: float
    discovery_failure_backoff_seconds: float


@dataclass(frozen=True)
class ProcessingWorkPollingIteration:
    """Capture the outcome of one polling iteration for deterministic tests."""

    outcome: ProcessingWorkPollingOutcome
    discovered_candidates: int
    handed_off_candidates: int
    claim_lost_candidates: int
    candidate_failures: int


class BoundedProcessingWorkPollingLoop:
    """Repeatedly discover candidate work and pass it to the handoff boundary."""

    def __init__(
        self,
        *,
        repository: ProcessingWorkDiscoveryRepository,
        candidate_handoff: ProcessingWorkCandidateHandoffProtocol,
        policy: ProcessingWorkPollingPolicy | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._candidate_handoff = candidate_handoff
        self._policy = policy or ProcessingWorkPollingPolicy(
            batch_size=get_document_ai_work_discovery_max_batch_size(),
            poll_interval_seconds=float(get_document_ai_worker_poll_interval_seconds()),
            empty_queue_backoff_seconds=float(get_document_ai_worker_empty_queue_backoff_seconds()),
            discovery_failure_backoff_seconds=float(
                get_document_ai_worker_discovery_failure_backoff_seconds()
            ),
        )
        self._logger = logger or logging.getLogger("document_ai.worker_polling")
        self._validate_policy()

    def run_once(self) -> ProcessingWorkPollingIteration:
        """Run one discovery iteration and isolate failures to that iteration."""

        try:
            candidates = self._repository.discover_work_candidates(limit=self._policy.batch_size)
        except Exception:
            self._logger.warning(
                "document_ai worker discovery failed",
                exc_info=True,
            )
            return ProcessingWorkPollingIteration(
                outcome="discovery_failed",
                discovered_candidates=0,
                handed_off_candidates=0,
                claim_lost_candidates=0,
                candidate_failures=0,
            )

        if not candidates:
            return ProcessingWorkPollingIteration(
                outcome="empty",
                discovered_candidates=0,
                handed_off_candidates=0,
                claim_lost_candidates=0,
                candidate_failures=0,
            )

        handed_off = 0
        claim_lost = 0
        candidate_failures = 0
        for candidate in candidates:
            try:
                if not self._candidate_handoff.handoff(candidate=candidate):
                    claim_lost += 1
                    continue
            except Exception:
                candidate_failures += 1
                self._logger.warning(
                    "document_ai worker candidate handoff failed",
                    exc_info=True,
                )
                continue
            handed_off += 1

        return ProcessingWorkPollingIteration(
            outcome="discovered",
            discovered_candidates=len(candidates),
            handed_off_candidates=handed_off,
            claim_lost_candidates=claim_lost,
            candidate_failures=candidate_failures,
        )

    def run_forever(
        self,
        *,
        stop_event: Event,
        wait_fn: Callable[[float], bool] | None = None,
    ) -> None:
        """Continuously poll until the supplied stop event is set."""

        waiter = wait_fn or stop_event.wait
        while not stop_event.is_set():
            iteration = self.run_once()
            delay_seconds = self._delay_for(iteration=iteration)
            if delay_seconds <= 0:
                continue
            if waiter(delay_seconds):
                break

    def _delay_for(self, *, iteration: ProcessingWorkPollingIteration) -> float:
        if iteration.outcome == "discovery_failed":
            return self._policy.discovery_failure_backoff_seconds
        if iteration.outcome == "empty":
            return self._policy.empty_queue_backoff_seconds
        return self._policy.poll_interval_seconds

    def _validate_policy(self) -> None:
        if self._policy.batch_size < 1:
            raise ValueError("document_ai_worker_poll_batch_size_must_be_positive")
        for field_name, value in (
            ("poll_interval_seconds", self._policy.poll_interval_seconds),
            ("empty_queue_backoff_seconds", self._policy.empty_queue_backoff_seconds),
            ("discovery_failure_backoff_seconds", self._policy.discovery_failure_backoff_seconds),
        ):
            if value <= 0 or value > 3600:
                raise ValueError(f"document_ai_worker_{field_name}_out_of_range")


class DocumentAIWorkerPollingController:
    """Own the worker polling thread and its shutdown signal."""

    def __init__(self, *, loop: BoundedProcessingWorkPollingLoop) -> None:
        self._loop = loop
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """Start the worker thread once."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(
            target=self._loop.run_forever,
            kwargs={"stop_event": self._stop_event},
            name="document-ai-worker-polling",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Request shutdown and wait briefly for the thread to exit."""

        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout_seconds)

    @property
    def is_running(self) -> bool:
        """Return whether the polling thread is active."""

        thread = self._thread
        return thread is not None and thread.is_alive()
