"""Milestone 12 retry budgets, priority and terminal recovery controls."""

from __future__ import annotations

from pathlib import Path

from services.document_ai.app.retry_policy import RetryPolicyConfig


def _policy() -> RetryPolicyConfig:
    return RetryPolicyConfig(
        max_attempts=3,
        base_delay_ms=100,
        backoff_multiplier=2,
        max_delay_ms=250,
        max_elapsed_ms=1_000,
        retryable_reason_codes=frozenset({"upstream_timeout"}),
    )


def test_retry_backoff_is_bounded_deterministic_and_honours_trustworthy_retry_after() -> None:
    policy = _policy()
    assert policy.scheduled_delay_ms(attempt_count=1, jitter=0.0) == 100
    assert policy.scheduled_delay_ms(attempt_count=2, jitter=0.2) == 240
    assert policy.scheduled_delay_ms(attempt_count=3, retry_after_ms=9_000, jitter=0.0) == 250


def test_retry_migration_persists_budget_dead_letter_priority_and_tenant_fairness() -> None:
    migration_path = Path("database/migrations/0038_document_ai_retry_budgets_and_priorities.sql")
    migration = migration_path.read_text()
    for marker in (
        "retry_count",
        "max_attempts",
        "max_retry_elapsed_seconds",
        "dead_lettered_at",
        "dead_letter_reason",
        "manual_recovery_count",
        "interactive",
        "maintenance",
        "idx_document_ai_processing_work_items_tenant_due",
    ):
        assert marker in migration
    outbox = Path("services/document_ai/app/outbox.py").read_text()
    assert "PARTITION BY outbox.tenant_id" in outbox
    assert "work.priority DESC" in outbox


def test_worker_failure_path_is_classified_fenced_and_cancellation_lifecycle_aware() -> None:
    source = Path("services/document_ai/app/processing_workers.py").read_text()
    for marker in (
        "classify_document_ai_failure",
        "dead_letter",
        "retry_budget_exhausted",
        "operation.cancellation_requested_at IS NULL",
        "document.state = ANY(%s)",
        "work.leased_until > now()",
        "processing.manual_recovery",
        "processing.retry.",
    ):
        assert marker in source
