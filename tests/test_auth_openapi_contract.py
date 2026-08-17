"""Contract checks for Phase 8 auth OpenAPI baseline."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

CONTRACT_PATH = Path("contracts/openapi/auth.yaml")

REQUIRED_PATHS = {
    "/v1/auth/register",
    "/v1/auth/login",
    "/v1/auth/refresh",
    "/v1/auth/logout",
    "/v1/auth/otp/challenges",
    "/v1/auth/otp/verify",
    "/v1/auth/sessions/{session_id}",
    "/v1/auth/oauth/{provider}/start",
    "/v1/auth/oauth/{provider}/callback",
    "/v1/auth/account-deletion/requests",
    "/v1/auth/account-deletion/confirm",
    "/v1/auth/account-deletion/cancel",
}

REQUIRED_METHODS_BY_PATH = {
    "/v1/auth/register": {"post"},
    "/v1/auth/login": {"post"},
    "/v1/auth/refresh": {"post"},
    "/v1/auth/logout": {"post"},
    "/v1/auth/otp/challenges": {"post"},
    "/v1/auth/otp/verify": {"post"},
    "/v1/auth/sessions/{session_id}": {"get"},
    "/v1/auth/oauth/{provider}/start": {"post"},
    "/v1/auth/oauth/{provider}/callback": {"get"},
    "/v1/auth/account-deletion/requests": {"post"},
    "/v1/auth/account-deletion/confirm": {"post"},
    "/v1/auth/account-deletion/cancel": {"post"},
}


def test_auth_openapi_parses() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"


def test_auth_openapi_contains_required_paths_and_methods() -> None:
    paths = _load_paths(_load_contract())

    assert REQUIRED_PATHS.issubset(set(paths))
    for path, methods in REQUIRED_METHODS_BY_PATH.items():
        assert methods.issubset(set(paths[path]))


def test_auth_openapi_error_schema_contains_required_fields() -> None:
    document = _load_contract()
    error_schema = _load_schema(document, "ErrorEnvelope")
    assert error_schema["type"] == "object"
    assert error_schema["additionalProperties"] is False
    required = set(cast(list[str], error_schema["required"]))
    assert {"error_code", "message", "reason"}.issubset(required)
    properties = cast(dict[str, object], error_schema["properties"])
    assert "trace_id" in properties


def test_auth_openapi_contains_otp_and_oauth_endpoint_families() -> None:
    paths = _load_paths(_load_contract())
    assert "/v1/auth/otp/challenges" in paths
    assert "/v1/auth/otp/verify" in paths
    assert "/v1/auth/oauth/{provider}/start" in paths
    assert "/v1/auth/oauth/{provider}/callback" in paths


def test_auth_openapi_otp_challenge_schema_is_canonical_and_purpose_scoped() -> None:
    document = _load_contract()
    challenge_request = _load_schema(document, "OtpChallengeRequest")
    required = set(cast(list[str], challenge_request["required"]))
    assert {"purpose", "channel"}.issubset(required)
    request_properties = cast(dict[str, object], challenge_request["properties"])
    purpose_schema = cast(dict[str, object], request_properties["purpose"])
    channel_schema = cast(dict[str, object], request_properties["channel"])
    purpose_values = set(cast(list[str], purpose_schema["enum"]))
    assert {
        "registration_verify",
        "login_step_up",
        "recovery",
        "phone_change_confirm",
    }.issubset(purpose_values)
    assert set(cast(list[str], channel_schema["enum"])) == {"sms", "email"}
    assert "phone_number" in request_properties
    assert "email" in request_properties

    challenge_response = _load_schema(document, "OtpChallengeResponse")
    response_required = set(cast(list[str], challenge_response["required"]))
    assert {"status", "challenge_id", "expires_at"}.issubset(response_required)
    response_properties = cast(dict[str, object], challenge_response["properties"])
    assert "otp_code" not in response_properties


def test_auth_openapi_documents_idempotency_and_step_up_requirements() -> None:
    document = _load_contract()
    paths = _load_paths(document)

    otp_challenge_post = cast(dict[str, object], paths["/v1/auth/otp/challenges"]["post"])
    otp_parameters = cast(list[object], otp_challenge_post["parameters"])
    assert any(
        cast(dict[str, object], parameter).get("$ref")
        == "#/components/parameters/IdempotencyKeyHeader"
        for parameter in otp_parameters
    )

    deletion_request_post = cast(
        dict[str, object], paths["/v1/auth/account-deletion/requests"]["post"]
    )
    deletion_parameters = cast(list[object], deletion_request_post["parameters"])
    assert any(
        cast(dict[str, object], parameter).get("$ref")
        == "#/components/parameters/IdempotencyKeyHeader"
        for parameter in deletion_parameters
    )

    deletion_confirm_post = cast(
        dict[str, object], paths["/v1/auth/account-deletion/confirm"]["post"]
    )
    description = cast(str, deletion_confirm_post["description"]).lower()
    assert "re-auth" in description
    assert "otp" in description
    assert "step-up" in description


def test_auth_openapi_logout_contract_includes_scope_and_revocation_summary() -> None:
    document = _load_contract()
    logout_schema = _load_schema(document, "LogoutResponse")
    required = set(cast(list[str], logout_schema["required"]))
    assert {"status", "revoke_scope", "revoked_session_count", "traceability"}.issubset(required)
    properties = cast(dict[str, object], logout_schema["properties"])
    revoke_scope = cast(dict[str, object], properties["revoke_scope"])
    assert cast(list[str], revoke_scope["enum"]) == ["single_session", "all_sessions"]


def test_auth_openapi_login_pending_step_up_schema_is_mfa_safe() -> None:
    document = _load_contract()
    pending_schema = _load_schema(document, "LoginPendingStepUpResponse")
    required = set(cast(list[str], pending_schema["required"]))
    assert {
        "login_status",
        "status",
        "step_up_required",
        "step_up_challenge_id",
        "step_up_channel",
        "step_up_expires_at",
    }.issubset(required)
    properties = cast(dict[str, object], pending_schema["properties"])
    assert "access_token" not in properties
    assert "refresh_token" not in properties


def test_auth_openapi_login_identifier_is_phone_only_kenya_pattern() -> None:
    document = _load_contract()
    login_request_schema = _load_schema(document, "LoginRequest")
    properties = cast(dict[str, object], login_request_schema["properties"])
    login_id_schema = cast(dict[str, object], properties["login_id"])
    assert login_id_schema["pattern"] == "^(\\+254|254|0)[17][0-9]{8}$"


def test_auth_openapi_registration_phone_and_kra_pin_patterns_are_governed() -> None:
    document = _load_contract()
    register_request_schema = _load_schema(document, "RegisterRequest")
    properties = cast(dict[str, object], register_request_schema["properties"])
    phone_schema = cast(dict[str, object], properties["phone_number"])
    kra_pin_schema = cast(dict[str, object], properties["kra_pin"])
    assert phone_schema["pattern"] == "^(\\+254|254|0)[17][0-9]{8}$"
    assert kra_pin_schema["pattern"] == "^[A-Z][0-9]{9}[A-Z]$"


def test_auth_openapi_phone_change_contract_enforces_kenyan_phone_and_response_field() -> None:
    document = _load_contract()
    request_schema = _load_schema(document, "PhoneChangeRequestCreateRequest")
    request_properties = cast(dict[str, object], request_schema["properties"])
    new_phone_schema = cast(dict[str, object], request_properties["new_phone_number"])
    assert new_phone_schema["pattern"] == "^(\\+254|254|0)[17][0-9]{8}$"

    response_schema = _load_schema(document, "PhoneChangeConfirmResponse")
    response_required = set(cast(list[str], response_schema["required"]))
    assert "updated_phone_number" in response_required


def test_auth_openapi_session_envelope_contract_is_security_aligned() -> None:
    document = _load_contract()
    session_schema = _load_schema(document, "SessionEnvelope")
    required = set(cast(list[str], session_schema["required"]))
    assert {"status", "session"}.issubset(required)
    properties = cast(dict[str, object], session_schema["properties"])
    assert {
        "issued_at",
        "expires_at",
        "inactivity_expires_at",
        "absolute_expires_at",
        "warning_window_started_at",
        "extension_allowed",
        "is_invalidated",
    }.issubset(set(properties))
    assert "access_token" not in properties
    assert "refresh_token" not in properties


def _load_contract() -> dict[str, object]:
    assert CONTRACT_PATH.exists()
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _load_paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    paths_value = document.get("paths")
    assert isinstance(paths_value, dict)
    typed_paths = cast(dict[str, object], paths_value)
    output: dict[str, dict[str, object]] = {}
    for path, path_item in typed_paths.items():
        assert isinstance(path, str)
        assert isinstance(path_item, dict)
        output[path] = cast(dict[str, object], path_item)
    return output


def _load_schema(document: dict[str, object], name: str) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    schemas = cast(dict[str, object], components.get("schemas", {}))
    schema = schemas.get(name)
    assert isinstance(schema, dict)
    return cast(dict[str, object], schema)
