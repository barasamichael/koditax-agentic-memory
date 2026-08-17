"""Runtime tests for deterministic auth session-introspection security behavior."""

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
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore


class _FrozenClock:
    """Provide deterministic time controls for session-introspection tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 31, 14, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def introspection_client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemorySessionIssuanceStore,
            InMemoryPhoneVerificationStore,
            _FrozenClock,
        ]
    ]
):
    """Create isolated auth app client with deterministic session policy store."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    clock = _FrozenClock()
    session_issuance_store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=120,
        absolute_lifetime_seconds=300,
        warning_window_seconds=30,
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


def test_session_introspection_returns_active_owned_session(
    introspection_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = introspection_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="session.introspection.active@example.com",
        phone_number="+254733700001",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733700001",
        source_ip="203.0.113.200",
        correlation_prefix="session-introspection-active",
    )
    session_id = UUID(cast(str, cast(dict[str, Any], login_payload["session"])["session_id"]))

    response = client.get(
        f"/v1/auth/sessions/{session_id}",
        headers={
            "Authorization": _build_authorization_header(user_id=user_id),
            "X-Correlation-ID": "session-introspection-active-corr",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "active"
    session = cast(dict[str, Any], payload["session"])
    assert session["session_id"] == str(session_id)
    assert session["user_id"] == str(user_id)
    assert isinstance(payload["inactivity_expires_at"], str)
    assert isinstance(payload["absolute_expires_at"], str)
    assert isinstance(payload["expires_at"], str)
    assert "access_token" not in payload
    assert "refresh_token" not in payload


def test_session_introspection_unauthorized_is_rejected_deterministically(
    introspection_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _ = introspection_client_and_stores
    session_id = UUID("11111111-1111-1111-1111-111111111111")

    first = client.get(
        f"/v1/auth/sessions/{session_id}",
        headers={"X-Correlation-ID": "session-introspection-unauthorized-corr"},
    )
    second = client.get(
        f"/v1/auth/sessions/{session_id}",
        headers={"X-Correlation-ID": "session-introspection-unauthorized-corr"},
    )
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 401
    assert second.status_code == 401
    assert first_error["error_code"] == "session_introspection_unauthorized"
    assert first_error["reason"] == "session_introspection_unauthorized"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_session_introspection_non_owned_session_is_rejected_deterministically(
    introspection_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = introspection_client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="session.introspection.owner@example.com",
        phone_number="+254733700002",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="session.introspection.attacker@example.com",
        phone_number="+254733700003",
    )
    owner_login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733700002",
        source_ip="203.0.113.201",
        correlation_prefix="session-introspection-owner",
    )
    owner_session_id = UUID(
        cast(str, cast(dict[str, Any], owner_login_payload["session"])["session_id"])
    )

    response = client.get(
        f"/v1/auth/sessions/{owner_session_id}",
        headers={
            "Authorization": _build_authorization_header(user_id=attacker_user_id),
            "X-Correlation-ID": "session-introspection-non-owned-corr",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 404
    assert error["error_code"] == "session_not_found_or_not_owned"
    assert error["reason"] == "session_not_found_or_not_owned"
    assert _build_authorization_header(user_id=owner_user_id) is not None


def test_session_introspection_after_logout_is_not_active(
    introspection_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, _ = introspection_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="session.introspection.revoked@example.com",
        phone_number="+254733700004",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733700004",
        source_ip="203.0.113.202",
        correlation_prefix="session-introspection-revoked",
    )
    session_id = cast(dict[str, Any], login_payload["session"])["session_id"]

    logout_response = client.post(
        "/v1/auth/logout",
        headers={
            "Authorization": _build_authorization_header(user_id=user_id),
            "X-Correlation-ID": "session-introspection-revoked-logout-corr",
        },
        json={"revoke_scope": "single_session", "target_session_id": session_id},
    )
    assert logout_response.status_code == 200

    introspection_response = client.get(
        f"/v1/auth/sessions/{session_id}",
        headers={
            "Authorization": _build_authorization_header(user_id=user_id),
            "X-Correlation-ID": "session-introspection-revoked-get-corr",
        },
    )
    introspection_payload = _response_json(introspection_response)
    assert introspection_response.status_code == 200
    assert introspection_payload["status"] == "invalidated"
    assert introspection_payload["is_invalidated"] is True


def test_session_introspection_inactivity_and_absolute_expiry_are_distinguishable(
    introspection_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryPhoneVerificationStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, phone_store, clock = introspection_client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="session.introspection.expiry@example.com",
        phone_number="+254733700005",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733700005",
        source_ip="203.0.113.203",
        correlation_prefix="session-introspection-expiry",
    )
    session_id = cast(dict[str, Any], login_payload["session"])["session_id"]

    clock.advance(seconds=121)
    inactivity_response = client.get(
        f"/v1/auth/sessions/{session_id}",
        headers={
            "Authorization": _build_authorization_header(user_id=user_id),
            "X-Correlation-ID": "session-introspection-expiry-inactivity-corr",
        },
    )
    inactivity_payload = _response_json(inactivity_response)
    assert inactivity_response.status_code == 200
    assert inactivity_payload["status"] == "expired"
    assert inactivity_payload["expires_at"] == inactivity_payload["inactivity_expires_at"]
    assert inactivity_payload["expires_at"] != inactivity_payload["absolute_expires_at"]

    clock = _FrozenClock()
    absolute_store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=300,
        absolute_lifetime_seconds=120,
        warning_window_seconds=30,
        max_concurrent_sessions=5,
    )
    # Use store directly to keep deterministic absolute-cap distinction coverage.
    issued = absolute_store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )
    clock.advance(seconds=121)
    absolute_eval = absolute_store.evaluate_session(session_id=issued.session_id)
    assert absolute_eval is not None
    assert absolute_eval.status == "expired"
    assert absolute_eval.reason_code == "session_absolute_expiry"
    assert absolute_eval.expires_at == absolute_eval.absolute_expires_at
    assert absolute_eval.expires_at != absolute_eval.inactivity_expires_at


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "session-introspection-register-corr"},
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
        verified_at="2026-03-31T14:00:00Z",
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
