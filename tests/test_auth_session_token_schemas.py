"""Contract tests for Phase 8 auth session/token schema baselines."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from jsonschema.validators import Draft202012Validator

SESSION_SCHEMA_PATH = Path("contracts/tools/schemas/auth_session_envelope.schema.json")
TOKEN_SCHEMA_PATH = Path("contracts/tools/schemas/auth_token_envelope.schema.json")
AUTH_CONTEXT_SCHEMA_PATH = Path("contracts/tools/schemas/auth_context_envelope.schema.json")

SESSION_REQUIRED_KEYS = {
    "schema_version",
    "session_id",
    "user_id",
    "tenant_id",
    "role",
    "delegation_context",
    "issued_at",
    "expires_at",
    "is_invalidated",
    "correlation_id",
}

TOKEN_REQUIRED_KEYS = {
    "schema_version",
    "token_type",
    "access_token",
    "expires_in_seconds",
    "refresh_token",
    "session_id",
    "user_id",
    "tenant_id",
    "role",
    "delegation_context",
    "scope",
    "issued_at",
}

AUTH_CONTEXT_REQUIRED_KEYS = {
    "schema_version",
    "user_id",
    "tenant_id",
    "role",
    "session_id",
    "delegation_context",
}


def test_session_token_and_context_schemas_parse_and_are_valid_json_schema() -> None:
    for schema_path in (
        SESSION_SCHEMA_PATH,
        TOKEN_SCHEMA_PATH,
        AUTH_CONTEXT_SCHEMA_PATH,
    ):
        schema = _load_schema(schema_path)
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)


def test_session_token_and_context_schemas_include_required_top_level_fields() -> None:
    session_schema = _load_schema(SESSION_SCHEMA_PATH)
    token_schema = _load_schema(TOKEN_SCHEMA_PATH)
    auth_context_schema = _load_schema(AUTH_CONTEXT_SCHEMA_PATH)

    assert set(cast(list[str], session_schema["required"])) == SESSION_REQUIRED_KEYS
    assert set(cast(list[str], token_schema["required"])) == TOKEN_REQUIRED_KEYS
    assert set(cast(list[str], auth_context_schema["required"])) == AUTH_CONTEXT_REQUIRED_KEYS
    assert session_schema["additionalProperties"] is False
    assert token_schema["additionalProperties"] is False
    assert auth_context_schema["additionalProperties"] is False


def test_positive_minimal_payloads_validate() -> None:
    session_errors = sorted(
        _build_validator(SESSION_SCHEMA_PATH).iter_errors(_valid_session_payload()),
        key=lambda item: item.path,
    )
    token_errors = sorted(
        _build_validator(TOKEN_SCHEMA_PATH).iter_errors(_valid_token_payload()),
        key=lambda item: item.path,
    )
    auth_context_errors = sorted(
        _build_validator(AUTH_CONTEXT_SCHEMA_PATH).iter_errors(_valid_auth_context_payload()),
        key=lambda item: item.path,
    )

    assert session_errors == []
    assert token_errors == []
    assert auth_context_errors == []


def test_negative_missing_required_field_fails_validation() -> None:
    invalid_session = _valid_session_payload()
    invalid_session.pop("user_id")
    invalid_token = _valid_token_payload()
    invalid_token.pop("access_token")
    invalid_auth_context = _valid_auth_context_payload()
    invalid_auth_context.pop("tenant_id")

    session_errors = sorted(
        _build_validator(SESSION_SCHEMA_PATH).iter_errors(invalid_session),
        key=lambda item: item.path,
    )
    token_errors = sorted(
        _build_validator(TOKEN_SCHEMA_PATH).iter_errors(invalid_token),
        key=lambda item: item.path,
    )
    auth_context_errors = sorted(
        _build_validator(AUTH_CONTEXT_SCHEMA_PATH).iter_errors(invalid_auth_context),
        key=lambda item: item.path,
    )

    assert session_errors
    assert token_errors
    assert auth_context_errors
    assert any(
        error.validator == "required" and "user_id" in error.message for error in session_errors
    )
    assert any(
        error.validator == "required" and "access_token" in error.message for error in token_errors
    )
    assert any(
        error.validator == "required" and "tenant_id" in error.message
        for error in auth_context_errors
    )


def test_negative_disallowed_extra_field_fails_validation() -> None:
    invalid_session = _valid_session_payload()
    invalid_session["unexpected_field"] = "not-allowed"
    invalid_token = _valid_token_payload()
    invalid_token["unexpected_field"] = "not-allowed"
    invalid_auth_context = _valid_auth_context_payload()
    invalid_auth_context["unexpected_field"] = "not-allowed"

    session_errors = sorted(
        _build_validator(SESSION_SCHEMA_PATH).iter_errors(invalid_session),
        key=lambda item: item.path,
    )
    token_errors = sorted(
        _build_validator(TOKEN_SCHEMA_PATH).iter_errors(invalid_token),
        key=lambda item: item.path,
    )
    auth_context_errors = sorted(
        _build_validator(AUTH_CONTEXT_SCHEMA_PATH).iter_errors(invalid_auth_context),
        key=lambda item: item.path,
    )

    assert session_errors
    assert token_errors
    assert auth_context_errors
    assert any(error.validator == "additionalProperties" for error in session_errors)
    assert any(error.validator == "additionalProperties" for error in token_errors)
    assert any(error.validator == "additionalProperties" for error in auth_context_errors)


def _build_validator(schema_path: Path) -> Draft202012Validator:
    schema = _load_schema(schema_path)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return cast(
        Draft202012Validator,
        validator_class(schema, format_checker=FormatChecker()),
    )


def _load_schema(schema_path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(schema_path.read_text(encoding="utf-8")))


def _valid_session_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tenant_id": "default_tenant",
        "role": "IndividualTaxpayer",
        "delegation_context": {
            "is_delegated": False,
            "principal_user_id": None,
            "delegate_user_id": None,
            "delegation_id": None,
            "granted_at": None,
            "revoked_at": None,
        },
        "issued_at": "2026-03-28T12:00:00Z",
        "expires_at": "2026-03-28T14:00:00Z",
        "is_invalidated": False,
        "correlation_id": "b" * 64,
    }


def _valid_token_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "token_type": "Bearer",
        "access_token": "opaque_access_token_value",
        "expires_in_seconds": 3600,
        "refresh_token": "opaque_refresh_token_value",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tenant_id": "default_tenant",
        "role": "IndividualTaxpayer",
        "delegation_context": {
            "is_delegated": False,
            "principal_user_id": None,
            "delegate_user_id": None,
            "delegation_id": None,
            "granted_at": None,
            "revoked_at": None,
        },
        "scope": ["auth:session:read", "auth:session:refresh"],
        "issued_at": "2026-03-28T12:00:00Z",
    }


def _valid_auth_context_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
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
