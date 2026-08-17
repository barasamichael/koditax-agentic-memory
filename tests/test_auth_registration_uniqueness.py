"""Focused deterministic uniqueness tests for auth registration."""

from __future__ import annotations

from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import reset_default_registration_store


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """Create isolated auth app client with deterministic registration store reset."""

    reset_default_registration_store()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_default_registration_store()


def test_duplicate_email_is_rejected_deterministically(client: TestClient) -> None:
    first = _register(
        client=client,
        correlation_id="reg-dup-email-first-corr",
        email="duplicate.identity@example.com",
        phone_number="+254700300111",
    )
    duplicate = _register(
        client=client,
        correlation_id="reg-dup-email-second-corr",
        email="duplicate.identity@example.com",
        phone_number="+254700300112",
    )

    assert first.status_code == 201
    error = _extract_error_detail(duplicate)
    assert duplicate.status_code == 409
    assert error["error_code"] == "registration_duplicate_email"
    assert error["message"] == "Registration request conflicts with an existing account."
    assert error["reason"] == "registration_duplicate_email"


def test_duplicate_phone_is_rejected_after_normalization_deterministically(
    client: TestClient,
) -> None:
    first = _register(
        client=client,
        correlation_id="reg-dup-phone-first-corr",
        email="phone.dup.one@example.com",
        phone_number="0700300113",
    )
    duplicate = _register(
        client=client,
        correlation_id="reg-dup-phone-second-corr",
        email="phone.dup.two@example.com",
        phone_number="254700300113",
    )

    assert first.status_code == 201
    error = _extract_error_detail(duplicate)
    assert duplicate.status_code == 409
    assert error["error_code"] == "registration_duplicate_phone"
    assert error["message"] == "Registration request conflicts with an existing account."
    assert error["reason"] == "registration_duplicate_phone"


def test_repeated_duplicate_registration_returns_identical_error_payload(
    client: TestClient,
) -> None:
    _register(
        client=client,
        correlation_id="reg-dup-seed-corr",
        email="seed.dup@example.com",
        phone_number="+254700300114",
    )

    first_duplicate = _register(
        client=client,
        correlation_id="reg-dup-determinism-corr",
        email="seed.dup@example.com",
        phone_number="+254700300115",
    )
    second_duplicate = _register(
        client=client,
        correlation_id="reg-dup-determinism-corr",
        email="seed.dup@example.com",
        phone_number="+254700300115",
    )

    first_error = _extract_error_detail(first_duplicate)
    second_error = _extract_error_detail(second_duplicate)
    assert first_duplicate.status_code == 409
    assert second_duplicate.status_code == 409
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def _register(
    *,
    client: TestClient,
    correlation_id: str,
    email: str,
    phone_number: str,
) -> object:
    return client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": correlation_id},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
