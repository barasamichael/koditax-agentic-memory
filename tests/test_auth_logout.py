"""Runtime tests for deterministic logout revocation semantics."""

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
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore


class _FrozenClock:
    """Provide deterministic time controls for logout/session tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def logout_client_and_stores(
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
    """Create isolated auth app client with deterministic stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    clock = _FrozenClock()
    monkeypatch.setattr("services.auth.app.login._utc_now", clock.now)
    session_issuance_store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=600,
        absolute_lifetime_seconds=600,
        warning_window_seconds=120,
        max_concurrent_sessions=5,
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


def test_logout_single_session_revokes_target_only(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_store, phone_store, clock = logout_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.single@example.com",
        phone_number="+254733500001",
    )
    first_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733500001",
        source_ip="203.0.113.71",
        correlation_prefix="logout-single-first",
    )
    _advance_for_next_login_step_up(clock=clock)
    second_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733500001",
        source_ip="203.0.113.72",
        correlation_prefix="logout-single-second",
    )
    first_session_id = UUID(cast(str, cast(dict[str, Any], first_login["session"])["session_id"]))
    second_session_id = UUID(cast(str, cast(dict[str, Any], second_login["session"])["session_id"]))

    logout_response = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-single-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={
            "revoke_scope": "single_session",
            "target_session_id": str(first_session_id),
        },
    )
    logout_payload = _response_json(logout_response)
    assert logout_response.status_code == 200
    assert logout_payload["status"] == "revoked"
    assert logout_payload["revoke_scope"] == "single_session"
    assert logout_payload["revoked_session_count"] == 1
    assert set(cast(dict[str, Any], logout_payload["traceability"]).keys()) == {
        "trace_id",
        "correlation_id",
    }

    first_eval = session_store.evaluate_session(session_id=first_session_id)
    second_eval = session_store.evaluate_session(session_id=second_session_id)
    assert first_eval is not None and first_eval.status == "invalidated"
    assert second_eval is not None and second_eval.status in {"active", "warning"}


def test_logout_global_revokes_all_active_sessions_for_user(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_store, phone_store, clock = logout_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.global@example.com",
        phone_number="+254733500002",
    )
    session_ids: list[UUID] = []
    for index in range(3):
        if index > 0:
            _advance_for_next_login_step_up(clock=clock)
        login_payload = _complete_login(
            client=client,
            phone_store=phone_store,
            login_id="+254733500002",
            source_ip=f"203.0.113.8{index}",
            correlation_prefix=f"logout-global-{index}",
        )
        session_ids.append(
            UUID(cast(str, cast(dict[str, Any], login_payload["session"])["session_id"]))
        )

    logout_response = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-global-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={"revoke_scope": "all_sessions"},
    )
    logout_payload = _response_json(logout_response)
    assert logout_response.status_code == 200
    assert logout_payload["status"] == "revoked"
    assert logout_payload["revoke_scope"] == "all_sessions"
    assert logout_payload["revoked_session_count"] == 3
    for session_id in session_ids:
        evaluation = session_store.evaluate_session(session_id=session_id)
        assert evaluation is not None
        assert evaluation.status == "invalidated"


def test_logout_non_owned_session_rejected_deterministically(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = logout_client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.owner@example.com",
        phone_number="+254733500003",
    )
    other_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.other@example.com",
        phone_number="+254733500004",
    )
    other_login = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733500004",
        source_ip="203.0.113.90",
        correlation_prefix="logout-non-owned",
    )
    other_session_id = cast(dict[str, Any], other_login["session"])["session_id"]

    response = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-non-owned-corr",
            "Authorization": _build_authorization_header(user_id=owner_user_id),
        },
        json={"revoke_scope": "single_session", "target_session_id": other_session_id},
    )
    error = _extract_error_detail(response)
    assert response.status_code == 404
    assert error["error_code"] == "logout_session_not_found_or_not_owned"
    assert error["reason"] == "logout_session_not_found_or_not_owned"
    assert _build_authorization_header(user_id=other_user_id) is not None


def test_logout_repeated_request_is_idempotent_in_outcome_semantics(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = logout_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.idempotent@example.com",
        phone_number="+254733500005",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733500005",
        source_ip="203.0.113.91",
        correlation_prefix="logout-idempotent",
    )
    session_id = cast(dict[str, Any], login_payload["session"])["session_id"]

    first = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-idempotent-first-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={"revoke_scope": "single_session", "target_session_id": session_id},
    )
    second = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-idempotent-second-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={"revoke_scope": "single_session", "target_session_id": session_id},
    )
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["status"] == "revoked"
    assert second_payload["status"] == "revoked"
    assert first_payload["revoke_scope"] == "single_session"
    assert second_payload["revoke_scope"] == "single_session"
    assert first_payload["revoked_session_count"] == 1
    assert second_payload["revoked_session_count"] == 0


def test_logout_revoked_session_cannot_refresh_afterward(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = logout_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.refresh.block@example.com",
        phone_number="+254733500006",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733500006",
        source_ip="203.0.113.92",
        correlation_prefix="logout-refresh-block",
    )
    session_id = cast(dict[str, Any], login_payload["session"])["session_id"]
    refresh_token = cast(str, login_payload["refresh_token"])

    logout_response = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-refresh-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={"revoke_scope": "single_session", "target_session_id": session_id},
    )
    assert logout_response.status_code == 200

    refresh_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "logout-refresh-followup-corr"},
        json={"refresh_token": refresh_token},
    )
    refresh_error = _extract_error_detail(refresh_response)
    assert refresh_response.status_code == 401
    assert refresh_error["error_code"] == "refresh_token_session_revoked"
    assert refresh_error["reason"] == "refresh_token_session_revoked"


def test_logout_requires_authenticated_principal(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _ = logout_client_and_stores
    response = client.post(
        "/v1/auth/logout",
        headers={"X-Correlation-ID": "logout-unauthorized-corr"},
        json={"revoke_scope": "all_sessions"},
    )
    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["error_code"] == "logout_unauthorized"
    assert error["reason"] == "logout_unauthorized"


def test_logout_invalid_payload_is_rejected_deterministically(
    logout_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _ = logout_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="logout.invalid.payload@example.com",
        phone_number="+254733500007",
    )

    response = client.post(
        "/v1/auth/logout",
        headers={
            "X-Correlation-ID": "logout-invalid-payload-corr",
            "Authorization": _build_authorization_header(user_id=user_id),
        },
        json={"revoke_scope": "single_session"},
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "logout_invalid_request"
    assert error["reason"] == "logout_invalid_request"


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "logout-register-corr"},
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
        verified_at="2026-03-31T12:00:00Z",
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


def _build_authorization_header(*, user_id: UUID) -> str:
    return f"Bearer user_id={user_id};tenant_id=default_tenant;role=IndividualTaxpayer"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
