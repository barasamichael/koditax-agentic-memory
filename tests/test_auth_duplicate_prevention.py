"""Runtime tests for deterministic duplicate-account prevention hardening."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from dataclasses import dataclass
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import reset_default_registration_store


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """Create isolated auth app client with deterministic registration store reset."""

    reset_default_registration_store()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_default_registration_store()


def test_unique_registration_still_succeeds(client: TestClient) -> None:
    response = _register(
        client=client,
        correlation_id="dup-positive-corr",
        email="unique.user@example.com",
        phone_number="+254700220001",
    )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["registration_status"] == "pending_verification"
    UUID(cast(str, payload["user_id"]))


def test_duplicate_email_is_rejected_deterministically(client: TestClient) -> None:
    first = _register(
        client=client,
        correlation_id="dup-email-first-corr",
        email="duplicate.email@example.com",
        phone_number="+254700220002",
    )
    second = _register(
        client=client,
        correlation_id="dup-email-second-corr",
        email="duplicate.email@example.com",
        phone_number="+254700220003",
    )

    error = _extract_error_detail(second)
    assert first.status_code == 201
    assert second.status_code == 409
    assert error["error_code"] == "registration_duplicate_email"
    assert error["message"] == "Registration request conflicts with an existing account."
    assert error["reason"] == "registration_duplicate_email"


def test_duplicate_phone_is_rejected_deterministically(client: TestClient) -> None:
    first = _register(
        client=client,
        correlation_id="dup-phone-first-corr",
        email="phone.dup.one@example.com",
        phone_number="+254700220004",
    )
    second = _register(
        client=client,
        correlation_id="dup-phone-second-corr",
        email="phone.dup.two@example.com",
        phone_number="+254700220004",
    )

    error = _extract_error_detail(second)
    assert first.status_code == 201
    assert second.status_code == 409
    assert error["error_code"] == "registration_duplicate_phone"
    assert error["message"] == "Registration request conflicts with an existing account."
    assert error["reason"] == "registration_duplicate_phone"


def test_race_like_duplicate_attempts_converge_to_deterministic_conflict_mapping() -> None:
    app = create_app()
    app.state.registration_store = _RaceLikeEmailConstraintStore()
    with TestClient(app) as client:
        first = _register(
            client=client,
            correlation_id="dup-race-first-corr",
            email="race.user@example.com",
            phone_number="+254700220005",
        )
        second = _register(
            client=client,
            correlation_id="dup-race-second-corr",
            email="race.user@example.com",
            phone_number="+254700220005",
        )
        third = _register(
            client=client,
            correlation_id="dup-race-second-corr",
            email="race.user@example.com",
            phone_number="+254700220005",
        )

    second_error = _extract_error_detail(second)
    third_error = _extract_error_detail(third)
    assert first.status_code == 201
    assert second.status_code == 409
    assert third.status_code == 409
    assert second_error["error_code"] == "registration_duplicate_email"
    assert second_error["reason"] == "registration_duplicate_email"
    assert canonical_json_dumps(third_error) == canonical_json_dumps(second_error)


def test_repeated_duplicate_input_returns_identical_error_payload_shape_and_content(
    client: TestClient,
) -> None:
    _register(
        client=client,
        correlation_id="dup-determinism-seed-corr",
        email="deterministic.dup@example.com",
        phone_number="+254700220006",
    )

    first_duplicate = _register(
        client=client,
        correlation_id="dup-determinism-corr",
        email="deterministic.dup@example.com",
        phone_number="+254700220007",
    )
    second_duplicate = _register(
        client=client,
        correlation_id="dup-determinism-corr",
        email="deterministic.dup@example.com",
        phone_number="+254700220007",
    )

    first_error = _extract_error_detail(first_duplicate)
    second_error = _extract_error_detail(second_duplicate)
    assert first_duplicate.status_code == 409
    assert second_duplicate.status_code == 409
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


@dataclass(frozen=True)
class _FakeUniqueViolationError(Exception):
    """Mimic DB unique-constraint exception shape for deterministic mapping tests."""

    message: str
    constraint_name: str

    def __str__(self) -> str:  # pragma: no cover - trivial wrapper
        return self.message


class _RaceLikeEmailConstraintStore:
    """Simulate stale pre-check visibility with DB unique conflict on insert."""

    def __init__(self) -> None:
        self._created_user: RegisteredUserRecord | None = None
        self._insert_attempts = 0

    def register_user(
        self,
        *,
        email_normalized: str,
        phone_number_normalized: str,
        kra_pin_hash: str,
        password_hash: str,
        role: str,
        created_at: str,
    ) -> RegisteredUserRecord:
        self._insert_attempts += 1
        if self._insert_attempts == 1:
            self._created_user = RegisteredUserRecord(
                user_id=uuid4(),
                email_normalized=email_normalized,
                phone_number_normalized=phone_number_normalized,
                kra_pin_hash=kra_pin_hash,
                password_hash=password_hash,
                role=role,
                created_at=created_at,
                verification_state="pending_verification",
                verified_at=None,
            )
            return cast(RegisteredUserRecord, self._created_user)
        raise _FakeUniqueViolationError(
            message="duplicate key value violates unique constraint uq_users_email_encrypted",
            constraint_name="uq_users_email_encrypted",
        )

    def get_user_by_email(self, *, email_normalized: str) -> RegisteredUserRecord | None:
        return None

    def get_user_by_phone(self, *, phone_number_normalized: str) -> RegisteredUserRecord | None:
        return None

    def mark_user_email_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        return cast(RegisteredUserRecord, self._created_user)

    def mark_user_phone_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        return cast(RegisteredUserRecord, self._created_user)


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
            "kra_pin": _kra_pin_for_phone(phone_number),
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )


def _kra_pin_for_phone(phone_number: str) -> str:
    digits_only = "".join(ch for ch in phone_number if ch.isdigit())
    serial = digits_only[-9:].rjust(9, "0")
    return f"A{serial}B"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
