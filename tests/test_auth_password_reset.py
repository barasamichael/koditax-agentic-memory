"""Runtime tests for deterministic password setup/reset initiation and confirmation flow."""

from __future__ import annotations

import re
from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.main import InMemoryAuthAuditStore
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.password_reset import InMemoryPasswordResetStore
from services.auth.app.session_issuance import InMemorySessionIssuanceStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPasswordResetStore,
            InMemorySessionIssuanceStore,
        ]
    ]
):
    """Create isolated auth app client with deterministic registration/reset stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    password_reset_store = InMemoryPasswordResetStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    app.state.registration_store = registration_store
    app.state.password_reset_store = password_reset_store
    app.state.session_issuance_store = session_issuance_store
    app.state.auth_audit_store = InMemoryAuthAuditStore()
    with TestClient(app) as test_client:
        yield test_client, registration_store, password_reset_store, session_issuance_store
    reset_default_registration_store()


def test_password_reset_positive_challenge_updates_password(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, registration_store, password_reset_store, _ = client_and_stores
    email = "password.reset.success@example.com"
    _register_user(client=client, email=email, phone_number="+254711440001")
    before = registration_store.get_user_by_email(email_normalized=email.lower())
    assert before is not None

    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-positive-idem",
        correlation_id="password-reset-positive-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-confirm-positive-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "N3wStrongerPassw0rd!",
        },
    )
    confirm_payload = _response_json(confirm_response)
    assert confirm_response.status_code == 200
    assert confirm_payload["status"] == "password_updated"

    after = registration_store.get_user_by_email(email_normalized=email.lower())
    assert after is not None
    assert after.password_hash != before.password_hash
    assert after.password_hash.startswith("$2")
    assert not re.fullmatch(r"[0-9a-f]{64}", after.password_hash)
    assert len(after.password_history_hashes) == 2


def test_password_reset_reuse_of_recent_password_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, password_reset_store, _ = client_and_stores
    email = "password.reset.reuse.blocked@example.com"
    initial_password = "StrongPassw0rd!"
    updated_password = "N3wStrongerPassw0rd!"
    _register_user(client=client, email=email, phone_number="+254711440006")

    first_initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-reuse-first-idem",
        correlation_id="password-reset-reuse-first-corr",
    )
    first_challenge_id = UUID(cast(str, first_initiated["challenge_id"]))
    first_reset_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=first_challenge_id
    )
    first_confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-reuse-first-confirm-corr"},
        json={
            "challenge_id": str(first_challenge_id),
            "reset_code": first_reset_code,
            "new_password": updated_password,
        },
    )
    assert first_confirm_response.status_code == 200

    second_initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-reuse-second-idem",
        correlation_id="password-reset-reuse-second-corr",
    )
    second_challenge_id = UUID(cast(str, second_initiated["challenge_id"]))
    second_reset_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=second_challenge_id
    )
    second_confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-reuse-second-confirm-corr"},
        json={
            "challenge_id": str(second_challenge_id),
            "reset_code": second_reset_code,
            "new_password": initial_password,
        },
    )

    second_error = _extract_error_detail(second_confirm_response)
    assert second_confirm_response.status_code == 409
    assert second_error["error_code"] == "password_reuse_not_allowed"
    assert second_error["reason"] == "password_reuse_not_allowed"


def test_password_reset_expired_challenge_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, password_reset_store, _ = client_and_stores
    email = "password.reset.expired@example.com"
    _register_user(client=client, email=email, phone_number="+254711440002")
    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-expired-idem",
        correlation_id="password-reset-expired-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    password_reset_store.force_expire_challenge(challenge_id=challenge_id)
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-expired-confirm-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "N3wStrongerPassw0rd!",
        },
    )

    error = _extract_error_detail(confirm_response)
    assert confirm_response.status_code == 409
    assert error["error_code"] == "password_reset_token_expired"
    assert error["reason"] == "password_reset_token_expired"


def test_password_reset_invalid_challenge_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-invalid-confirm-corr"},
        json={
            "challenge_id": str(uuid4()),
            "reset_code": "123456",
            "new_password": "N3wStrongerPassw0rd!",
        },
    )

    error = _extract_error_detail(confirm_response)
    assert confirm_response.status_code == 409
    assert error["error_code"] == "password_reset_token_invalid"
    assert error["reason"] == "password_reset_token_invalid"


def test_password_reset_consumed_challenge_replay_is_rejected(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, password_reset_store, _ = client_and_stores
    email = "password.reset.replay@example.com"
    _register_user(client=client, email=email, phone_number="+254711440003")
    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-replay-idem",
        correlation_id="password-reset-replay-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    first = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-replay-confirm-first-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "N3wStrongerPassw0rd!",
        },
    )
    second = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-replay-confirm-second-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "AnotherStr0ngPass!",
        },
    )

    second_error = _extract_error_detail(second)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second_error["error_code"] == "password_reset_token_already_used"
    assert second_error["reason"] == "password_reset_token_already_used"


def test_password_reset_weak_new_password_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, password_reset_store, _ = client_and_stores
    email = "password.reset.weak@example.com"
    _register_user(client=client, email=email, phone_number="+254711440004")
    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-weak-idem",
        correlation_id="password-reset-weak-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    weak_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-weak-confirm-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "weak",
        },
    )
    weak_error = _extract_error_detail(weak_response)
    assert weak_response.status_code == 409
    assert weak_error["error_code"] == "password_policy_violation"
    assert weak_error["reason"] == "password_policy_violation"


def test_password_reset_initiation_is_existence_neutral(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    existing_email = "password.reset.known@example.com"
    unknown_email = "password.reset.unknown@example.com"
    _register_user(client=client, email=existing_email, phone_number="+254711440005")

    existing_response = _initiate_reset(
        client=client,
        email=existing_email,
        idempotency_key="password-reset-known-idem",
        correlation_id="password-reset-known-corr",
    )
    unknown_response = _initiate_reset(
        client=client,
        email=unknown_email,
        idempotency_key="password-reset-unknown-idem",
        correlation_id="password-reset-unknown-corr",
    )

    assert existing_response["status"] == "challenge_issued"
    assert unknown_response["status"] == "challenge_issued"
    assert set(existing_response.keys()) == set(unknown_response.keys())
    assert "account_exists" not in existing_response
    assert "account_exists" not in unknown_response


def test_password_reset_history_depth_enforces_last_five_passwords(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, registration_store, password_reset_store, _ = client_and_stores
    email = "password.reset.history.depth@example.com"
    phone_number = "+254711440010"
    baseline_password = "StrongPassw0rd!"
    _register_user(client=client, email=email, phone_number=phone_number)

    password_sequence = [
        "N3wStrongerPassw0rd!1",
        "N3wStrongerPassw0rd!2",
        "N3wStrongerPassw0rd!3",
        "N3wStrongerPassw0rd!4",
        "N3wStrongerPassw0rd!5",
    ]
    for index, next_password in enumerate(password_sequence):
        initiated = _initiate_reset(
            client=client,
            email=email,
            idempotency_key=f"password-reset-history-idem-{index}",
            correlation_id=f"password-reset-history-corr-{index}",
        )
        challenge_id = UUID(cast(str, initiated["challenge_id"]))
        reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)
        response = client.post(
            "/v1/auth/password-reset/confirm",
            headers={"X-Correlation-ID": f"password-reset-history-confirm-{index}"},
            json={
                "challenge_id": str(challenge_id),
                "reset_code": reset_code,
                "new_password": next_password,
            },
        )
        assert response.status_code == 200

    user_record = registration_store.get_user_by_email(email_normalized=email.lower())
    assert user_record is not None
    assert len(user_record.password_history_hashes) == 5
    assert user_record.password_hash == user_record.password_history_hashes[0]

    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-history-final-idem",
        correlation_id="password-reset-history-final-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    reuse_blocked_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-history-reuse-blocked-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": password_sequence[0],
        },
    )
    reuse_error = _extract_error_detail(reuse_blocked_response)
    assert reuse_blocked_response.status_code == 409
    assert reuse_error["error_code"] == "password_reuse_not_allowed"
    assert reuse_error["reason"] == "password_reuse_not_allowed"

    old_password_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-history-old-allowed-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": baseline_password,
        },
    )
    old_password_payload = _response_json(old_password_response)
    assert old_password_response.status_code == 200
    assert old_password_payload["status"] == "password_updated"


def test_password_reset_success_invalidates_active_sessions_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, registration_store, password_reset_store, session_issuance_store = client_and_stores
    email = "password.reset.session.invalidation@example.com"
    _register_user(client=client, email=email, phone_number="+254711440099")
    user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert user is not None

    first_session = session_issuance_store.issue_session(
        user_id=user.user_id,
        tenant_id="default_tenant",
        role=user.role,
        device_fingerprint="device-one",
    )
    second_session = session_issuance_store.issue_session(
        user_id=user.user_id,
        tenant_id="default_tenant",
        role=user.role,
        device_fingerprint="device-two",
    )

    initiated = _initiate_reset(
        client=client,
        email=email,
        idempotency_key="password-reset-session-invalidation-idem",
        correlation_id="password-reset-session-invalidation-corr",
    )
    challenge_id = UUID(cast(str, initiated["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)
    confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "password-reset-session-invalidation-confirm-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "N3wResetSessionPassw0rd!",
        },
    )
    assert confirm_response.status_code == 200

    first_eval = session_issuance_store.evaluate_session(session_id=first_session.session_id)
    second_eval = session_issuance_store.evaluate_session(session_id=second_session.session_id)
    assert first_eval is not None
    assert second_eval is not None
    assert first_eval.status == "invalidated"
    assert second_eval.status == "invalidated"
    assert first_eval.reason_code == "session_revoked"
    assert second_eval.reason_code == "session_revoked"


def _register_user(*, client: TestClient, email: str, phone_number: str) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"password-reset-register-{uuid4()}"},
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
    return payload


def _initiate_reset(
    *,
    client: TestClient,
    email: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/password-reset/initiate",
        headers={
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
        },
        json={
            "purpose": "password_reset",
            "channel": "email",
            "email": email,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
