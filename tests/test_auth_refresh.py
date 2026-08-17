"""Runtime tests for deterministic refresh-token rotation and reuse detection."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.login import InMemoryLoginStepUpStore
from services.auth.app.login import InMemoryLoginLockoutStore
from services.auth.app.config import get_auth_otp_policy_for_purpose
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore


class _FrozenClock:
    """Provide deterministic clock controls for refresh token policy tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 31, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def refresh_client_and_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[
    tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ]
]:
    """Create isolated auth app client with deterministic refresh-capable stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    clock = _FrozenClock()
    monkeypatch.setattr("services.auth.app.login._utc_now", clock.now)
    session_issuance_store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=300,
        absolute_lifetime_seconds=300,
        warning_window_seconds=60,
        max_concurrent_sessions=3,
    )
    app.state.registration_store = registration_store
    app.state.session_issuance_store = session_issuance_store
    app.state.email_verification_store = email_verification_store
    app.state.phone_verification_store = phone_verification_store
    app.state.login_lockout_store = InMemoryLoginLockoutStore()
    app.state.login_step_up_store = InMemoryLoginStepUpStore()
    with TestClient(app) as client:
        yield client, registration_store, session_issuance_store, phone_verification_store, clock
    reset_default_registration_store()


def test_refresh_rotates_tokens_and_returns_new_context(
    refresh_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = refresh_client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="refresh.rotation@example.com",
        phone_number="+254722550001",
    )

    initial_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550001",
        source_ip="203.0.113.50",
        correlation_prefix="refresh-rotation",
    )
    refresh_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-rotation-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    refresh_payload = _response_json(refresh_response)

    assert refresh_response.status_code == 200
    assert refresh_payload["status"] == "refreshed"
    assert refresh_payload["access_token"] != initial_login["access_token"]
    assert refresh_payload["refresh_token"] != initial_login["refresh_token"]
    assert (
        cast(dict[str, Any], refresh_payload["session"])["session_id"]
        == cast(dict[str, Any], initial_login["session"])["session_id"]
    )


def test_refresh_reused_token_is_rejected_deterministically(
    refresh_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = refresh_client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="refresh.reuse@example.com",
        phone_number="+254722550002",
    )
    initial_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550002",
        source_ip="203.0.113.51",
        correlation_prefix="refresh-reuse-login",
    )

    first_rotation = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-reuse-first-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    assert first_rotation.status_code == 200

    reused_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-reuse-second-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    reused_repeat = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-reuse-second-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    reused_error = _extract_error_detail(reused_response)
    reused_repeat_error = _extract_error_detail(reused_repeat)
    assert reused_response.status_code == 409
    assert reused_error["error_code"] == "refresh_token_reused"
    assert reused_error["reason"] == "refresh_token_reused"
    assert reused_repeat.status_code == 409
    assert canonical_json_dumps(reused_error) == canonical_json_dumps(reused_repeat_error)


def test_refresh_malformed_token_is_rejected_deterministically(
    refresh_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _ = refresh_client_and_stores
    first = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-malformed-corr"},
        json={"refresh_token": "not-a-valid-refresh-token"},
    )
    second = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-malformed-corr"},
        json={"refresh_token": "not-a-valid-refresh-token"},
    )
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 401
    assert second.status_code == 401
    assert first_error["error_code"] == "refresh_token_malformed"
    assert first_error["reason"] == "refresh_token_malformed"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_refresh_expired_token_is_rejected_deterministically(
    refresh_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, clock = refresh_client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="refresh.expired@example.com",
        phone_number="+254722550003",
    )
    initial_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550003",
        source_ip="203.0.113.52",
        correlation_prefix="refresh-expired-login",
    )
    clock.advance(seconds=301)

    expired_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-expired-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    expired_repeat = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-expired-corr"},
        json={"refresh_token": initial_login["refresh_token"]},
    )
    expired_error = _extract_error_detail(expired_response)
    expired_repeat_error = _extract_error_detail(expired_repeat)
    assert expired_response.status_code == 401
    assert expired_error["error_code"] == "refresh_token_expired"
    assert expired_error["reason"] == "refresh_token_expired"
    assert expired_repeat.status_code == 401
    assert canonical_json_dumps(expired_error) == canonical_json_dumps(expired_repeat_error)


def test_refresh_for_concurrency_revoked_session_is_rejected_deterministically(
    refresh_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, clock = refresh_client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="refresh.revoked@example.com",
        phone_number="+254722550004",
    )
    first_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550004",
        source_ip="203.0.113.53",
        correlation_prefix="refresh-revoked-login-one",
    )
    _advance_for_next_login_step_up(clock=clock)
    _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550004",
        source_ip="203.0.113.54",
        correlation_prefix="refresh-revoked-login-two",
    )
    _advance_for_next_login_step_up(clock=clock)
    _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550004",
        source_ip="203.0.113.55",
        correlation_prefix="refresh-revoked-login-three",
    )
    _advance_for_next_login_step_up(clock=clock)
    _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254722550004",
        source_ip="203.0.113.56",
        correlation_prefix="refresh-revoked-login-four",
    )

    revoked_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-revoked-corr"},
        json={"refresh_token": first_login["refresh_token"]},
    )
    revoked_repeat = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "refresh-revoked-corr"},
        json={"refresh_token": first_login["refresh_token"]},
    )
    revoked_error = _extract_error_detail(revoked_response)
    revoked_repeat_error = _extract_error_detail(revoked_repeat)
    assert revoked_response.status_code == 401
    assert revoked_error["error_code"] == "refresh_token_session_revoked"
    assert revoked_error["reason"] == "refresh_token_session_revoked"
    assert revoked_repeat.status_code == 401
    assert canonical_json_dumps(revoked_error) == canonical_json_dumps(revoked_repeat_error)


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "refresh-register-corr"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    user_id = UUID(cast(str, payload["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-31T10:00:00Z",
    )
    return user_id


def _complete_login(
    *,
    client: TestClient,
    phone_store: InMemoryPhoneVerificationStore,
    login_id: str,
    source_ip: str,
    correlation_prefix: str,
) -> dict[str, Any]:
    pending_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-pending",
            "X-Forwarded-For": source_ip,
        },
        json={"login_id": login_id, "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending_response)
    assert pending_response.status_code == 200
    assert pending_payload["status"] == "pending_step_up"

    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    final_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-final",
            "X-Forwarded-For": source_ip,
        },
        json={
            "login_id": login_id,
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    final_payload = _response_json(final_response)
    assert final_response.status_code == 200
    assert final_payload["status"] == "authenticated"
    return final_payload


def _advance_for_next_login_step_up(*, clock: _FrozenClock) -> None:
    resend_min_interval = get_auth_otp_policy_for_purpose(
        "login_step_up"
    ).resend_min_interval_seconds
    clock.advance(seconds=resend_min_interval + 1)


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "refresh_token" not in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
