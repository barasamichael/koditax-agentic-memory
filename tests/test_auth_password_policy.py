"""Focused deterministic tests for password hashing and reuse-policy controls."""

from __future__ import annotations

import re
from uuid import UUID
from typing import Any
from typing import cast
from hashlib import sha256
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import build_password_hash
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
    """Create isolated auth app client with deterministic password stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    password_reset_store = InMemoryPasswordResetStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    app.state.registration_store = registration_store
    app.state.password_reset_store = password_reset_store
    app.state.session_issuance_store = session_issuance_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, password_reset_store, session_issuance_store
    reset_default_registration_store()


def test_bcrypt_cost_minimum_is_enforced_even_when_env_is_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_PASSWORD_BCRYPT_COST", "4")
    password_hash = build_password_hash(password="StrongPassw0rd!")
    bcrypt_match = re.match(r"^\$2[aby]\$(\d{2})\$", password_hash)
    assert bcrypt_match is not None
    assert int(bcrypt_match.group(1)) >= 12


def test_password_history_reuse_window_enforces_last_five_passwords() -> None:
    store = InMemoryRegistrationStore()
    user = store.register_user(
        email_normalized="policy.window@example.com",
        phone_number_normalized="+254700555000",
        kra_pin_hash=sha256(b"A123456789Z").hexdigest(),
        password_hash=build_password_hash(password="StrongPassw0rd!"),
        role="IndividualTaxpayer",
        created_at="2026-04-11T08:00:00Z",
    )
    store.update_user_password_hash(
        user_id=user.user_id,
        password_hash=build_password_hash(password="N3wPassw0rd!1"),
    )
    store.update_user_password_hash(
        user_id=user.user_id,
        password_hash=build_password_hash(password="N3wPassw0rd!2"),
    )
    store.update_user_password_hash(
        user_id=user.user_id,
        password_hash=build_password_hash(password="N3wPassw0rd!3"),
    )
    store.update_user_password_hash(
        user_id=user.user_id,
        password_hash=build_password_hash(password="N3wPassw0rd!4"),
    )
    store.update_user_password_hash(
        user_id=user.user_id,
        password_hash=build_password_hash(password="N3wPassw0rd!5"),
    )

    assert store.is_password_reused(user_id=user.user_id, password="N3wPassw0rd!1")
    assert not store.is_password_reused(user_id=user.user_id, password="StrongPassw0rd!")


def test_repeated_invalid_password_reset_confirmation_is_deterministic(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPasswordResetStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    payload = {
        "challenge_id": str(UUID("11111111-1111-1111-1111-111111111111")),
        "reset_code": "123456",
        "new_password": "weak",
    }
    headers = {"X-Correlation-ID": "password-policy-determinism-corr"}

    first = client.post("/v1/auth/password-reset/confirm", headers=headers, json=payload)
    second = client.post("/v1/auth/password-reset/confirm", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 409
    assert second.status_code == 409
    assert first_error["error_code"] == "password_reset_token_invalid"
    assert first_error["reason"] == "password_reset_token_invalid"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
