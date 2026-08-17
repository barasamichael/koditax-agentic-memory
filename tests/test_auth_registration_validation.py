"""Focused deterministic validation tests for auth registration."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import get_default_registration_store
from services.auth.app.registration import reset_default_registration_store


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """Create isolated auth app client with deterministic registration store reset."""

    reset_default_registration_store()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_default_registration_store()


def test_registration_normalizes_accepted_kenya_phone_variants(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-phone-normalize-corr"},
        json={
            "email": "normalize.phone@example.com",
            "phone_number": "0722 300 120",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    payload = _response_json(response)
    assert response.status_code == 201
    user_id = UUID(cast(str, payload["user_id"]))
    stored = get_default_registration_store().get_user_by_id(user_id=user_id)
    assert stored is not None
    assert stored.phone_number_normalized == "+254722300120"


def test_registration_invalid_phone_format_is_rejected_deterministically(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-invalid-phone-corr"},
        json={
            "email": "invalid.phone@example.com",
            "phone_number": "+255700300121",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_invalid_phone"
    assert error["message"] == "Registration phone-number format is invalid."
    assert error["reason"] == "registration_invalid_phone"


def test_registration_lowercase_kra_pin_is_rejected_deterministically(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-invalid-kra-case-corr"},
        json={
            "email": "invalid.kra.case@example.com",
            "phone_number": "+254700300122",
            "kra_pin": "a123456789z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_invalid_kra_pin"
    assert error["message"] == "Registration KRA PIN format is invalid."
    assert error["reason"] == "registration_invalid_kra_pin"


def test_repeated_identical_invalid_registration_has_stable_error_shape(
    client: TestClient,
) -> None:
    payload = {
        "email": "det.invalid.phone@example.com",
        "phone_number": "700300123",
        "kra_pin": "A123456789Z",
        "password": "StrongPassw0rd!",
        "role": "IndividualTaxpayer",
    }
    headers = {"X-Correlation-ID": "reg-invalid-determinism-corr"}

    first = client.post("/v1/auth/register", headers=headers, json=payload)
    second = client.post("/v1/auth/register", headers=headers, json=payload)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
