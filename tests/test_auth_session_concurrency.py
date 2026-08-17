"""Focused deterministic tests for auth session concurrency policy."""

from __future__ import annotations

from uuid import uuid4
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from services.auth.app.session_issuance import InMemorySessionIssuanceStore


class _FrozenClock:
    """Provide deterministic time controls for concurrency checks."""

    def __init__(self) -> None:
        self._current = datetime(2026, 4, 11, 11, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def test_concurrency_limit_evicts_oldest_session_deterministically() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=1800,
        absolute_lifetime_seconds=28800,
        warning_window_seconds=120,
        max_concurrent_sessions=2,
    )
    user_id = uuid4()
    first = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-1",
    )
    clock.advance(seconds=1)
    second = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-2",
    )
    clock.advance(seconds=1)
    third = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-3",
    )

    assert third.evicted_session_ids == (first.session_id,)
    first_eval = store.evaluate_session(session_id=first.session_id)
    second_eval = store.evaluate_session(session_id=second.session_id)
    third_eval = store.evaluate_session(session_id=third.session_id)
    assert first_eval is not None
    assert first_eval.status == "invalidated"
    assert first_eval.reason_code == "session_concurrency_limit_enforced"
    assert second_eval is not None
    assert second_eval.status in {"active", "warning"}
    assert third_eval is not None
    assert third_eval.status in {"active", "warning"}


def test_concurrency_policy_isolation_by_user_is_deterministic() -> None:
    store = InMemorySessionIssuanceStore(
        inactivity_timeout_seconds=1800,
        absolute_lifetime_seconds=28800,
        warning_window_seconds=120,
        max_concurrent_sessions=1,
    )
    first_user = uuid4()
    second_user = uuid4()

    first_user_first = store.issue_session(
        user_id=first_user,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="first-1",
    )
    first_user_second = store.issue_session(
        user_id=first_user,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="first-2",
    )
    second_user_first = store.issue_session(
        user_id=second_user,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="second-1",
    )

    assert first_user_second.evicted_session_ids == (first_user_first.session_id,)
    assert second_user_first.evicted_session_ids == ()
