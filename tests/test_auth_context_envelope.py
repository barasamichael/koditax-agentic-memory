"""Tests for canonical auth-context envelope validation boundary."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from shared.authz.rbac import AuthContextValidationError
from shared.authz.rbac import parse_auth_context_envelope


def test_valid_auth_context_envelope_parses_successfully() -> None:
    envelope = parse_auth_context_envelope(_valid_auth_context_payload())
    assert envelope.user_id == UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert envelope.tenant_id == "default_tenant"
    assert envelope.role == "IndividualTaxpayer"
    assert envelope.session_id == UUID("11111111-2222-3333-4444-555555555555")
    assert envelope.delegation_context.is_delegated is False
    assert envelope.delegation_context.principal_user_id is None
    assert envelope.delegation_context.delegate_user_id is None
    assert envelope.delegation_context.delegation_id is None
    assert envelope.delegation_context.granted_at is None
    assert envelope.delegation_context.revoked_at is None


def test_missing_required_claim_is_rejected_deterministically() -> None:
    payload = _valid_auth_context_payload()
    payload.pop("role")

    with pytest.raises(AuthContextValidationError) as error_info:
        parse_auth_context_envelope(payload)

    error = error_info.value.to_error_detail()
    assert error["error_code"] == "auth_context_missing_required_claim"
    assert error["reason"] == "auth_context_missing_required_claim"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["claim"] == "role"


def test_invalid_claim_type_is_rejected_deterministically() -> None:
    payload = _valid_auth_context_payload()
    payload["user_id"] = 123  # type: ignore[assignment]

    with pytest.raises(AuthContextValidationError) as error_info:
        parse_auth_context_envelope(payload)

    error = error_info.value.to_error_detail()
    assert error["error_code"] == "auth_context_invalid_claim_type"
    assert error["reason"] == "auth_context_invalid_claim_type"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["claim"] == "user_id"


def test_invalid_role_is_rejected_deterministically() -> None:
    payload = _valid_auth_context_payload()
    payload["role"] = "UnsupportedRole"

    with pytest.raises(AuthContextValidationError) as error_info:
        parse_auth_context_envelope(payload)

    error = error_info.value.to_error_detail()
    assert error["error_code"] == "auth_context_invalid_role"
    assert error["reason"] == "auth_context_invalid_role"


def test_invalid_session_id_is_rejected_deterministically() -> None:
    payload = _valid_auth_context_payload()
    payload["session_id"] = "not-a-uuid"

    with pytest.raises(AuthContextValidationError) as error_info:
        parse_auth_context_envelope(payload)

    error = error_info.value.to_error_detail()
    assert error["error_code"] == "auth_context_invalid_session_id"
    assert error["reason"] == "auth_context_invalid_session_id"


def test_invalid_delegation_context_is_rejected_deterministically() -> None:
    payload = _valid_auth_context_payload()
    payload["delegation_context"] = {"is_delegated": True}

    with pytest.raises(AuthContextValidationError) as error_info:
        parse_auth_context_envelope(payload)

    error = error_info.value.to_error_detail()
    assert error["error_code"] == "auth_context_invalid_delegation_context"
    assert error["reason"] == "auth_context_invalid_delegation_context"


def test_repeated_invalid_input_has_stable_error_envelope_shape() -> None:
    payload = _valid_auth_context_payload()
    payload["session_id"] = "not-a-uuid"

    def _detail() -> dict[str, object]:
        with pytest.raises(AuthContextValidationError) as error_info:
            parse_auth_context_envelope(deepcopy(payload))
        return error_info.value.to_error_detail()

    first = _detail()
    second = _detail()
    assert first["error_code"] == second["error_code"]
    assert first["reason"] == second["reason"]
    assert set(first.keys()) == set(second.keys())


def test_repeated_missing_claim_input_has_stable_error_envelope_shape() -> None:
    payload = _valid_auth_context_payload()
    payload.pop("role")

    def _detail() -> dict[str, object]:
        with pytest.raises(AuthContextValidationError) as error_info:
            parse_auth_context_envelope(deepcopy(payload))
        return error_info.value.to_error_detail()

    first = _detail()
    second = _detail()
    assert first["error_code"] == second["error_code"]
    assert first["reason"] == second["reason"]
    assert set(first.keys()) == set(second.keys())


def _valid_auth_context_payload() -> dict[str, object]:
    return {
        "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tenant_id": "default_tenant",
        "role": "IndividualTaxpayer",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "delegation_context": {
            "is_delegated": False,
            "principal_user_id": None,
            "delegate_user_id": None,
            "delegation_id": None,
            "granted_at": None,
            "revoked_at": None,
        },
    }
