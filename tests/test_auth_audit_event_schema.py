"""Schema and determinism tests for canonical auth audit evidence payloads."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Any
from typing import cast
from pathlib import Path

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from jsonschema.validators import Draft202012Validator

from services.auth.app.main import build_auth_audit_event_envelope
from shared.determinism.input_hash import canonical_json_dumps

AUTH_AUDIT_SCHEMA_PATH = Path("contracts/tools/schemas/auth_audit_event.schema.json")


def test_auth_audit_schema_parses_and_is_valid_json_schema() -> None:
    schema = _load_schema()
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)


def test_auth_audit_schema_required_fields_and_additional_properties_policy() -> None:
    schema = _load_schema()
    assert set(cast(list[str], schema["required"])) == {
        "schema_version",
        "event_type",
        "event_time",
        "user_id",
        "tenant_id",
        "session_id",
        "correlation_id",
        "trace_id",
        "action_status",
        "reason_code",
        "evidence_hash",
        "details",
    }
    assert schema["additionalProperties"] is False


def test_representative_audit_events_conform_to_schema() -> None:
    first = build_auth_audit_event_envelope(
        event_type="auth_registration_requested",
        event_time="2026-04-01T10:00:00Z",
        user_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        tenant_id="default_tenant",
        session_id=None,
        correlation_id="corr-auth-audit-001",
        trace_id="trace-auth-audit-001",
        action_status="succeeded",
        reason_code=None,
        details={"registration_status": "pending_verification"},
    )
    second = build_auth_audit_event_envelope(
        event_type="auth_login_failed",
        event_time="2026-04-01T10:01:00Z",
        user_id=None,
        tenant_id="default_tenant",
        session_id=None,
        correlation_id="corr-auth-audit-002",
        trace_id="trace-auth-audit-002",
        action_status="failed",
        reason_code="login_invalid_credentials",
        details={},
    )
    validator = _build_validator()
    first_errors = sorted(
        cast(Any, validator).iter_errors(first.model_dump(mode="json")),
        key=lambda item: item.path,
    )
    second_errors = sorted(
        cast(Any, validator).iter_errors(second.model_dump(mode="json")),
        key=lambda item: item.path,
    )
    assert first_errors == []
    assert second_errors == []


def test_missing_required_field_rejected_deterministically() -> None:
    payload = build_auth_audit_event_envelope(
        event_type="auth_session_refreshed",
        event_time="2026-04-01T10:02:00Z",
        user_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        tenant_id="default_tenant",
        session_id=UUID("11111111-2222-3333-4444-555555555555"),
        correlation_id="corr-auth-audit-003",
        trace_id="trace-auth-audit-003",
        action_status="succeeded",
        reason_code=None,
        details={},
    ).model_dump(mode="json")
    payload.pop("tenant_id")
    errors = sorted(cast(Any, _build_validator()).iter_errors(payload), key=lambda item: item.path)
    assert errors
    assert any(error.validator == "required" and "tenant_id" in error.message for error in errors)


def test_sensitive_detail_fields_are_redacted() -> None:
    event = build_auth_audit_event_envelope(
        event_type="auth_password_reset_completed",
        event_time="2026-04-01T10:03:00Z",
        user_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        tenant_id="default_tenant",
        session_id=None,
        correlation_id="corr-auth-audit-004",
        trace_id="trace-auth-audit-004",
        action_status="completed",
        reason_code=None,
        details={
            "password": "NeverLogMe!",
            "otp_code": "123456",
            "refresh_token": "secret-token",
            "safe_field": "allowed",
        },
    )
    serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    assert "NeverLogMe!" not in serialized
    assert "123456" not in serialized
    assert "secret-token" not in serialized
    assert event.details["password"] == "[REDACTED]"
    assert event.details["otp_code"] == "[REDACTED]"
    assert event.details["refresh_token"] == "[REDACTED]"


def test_same_event_input_yields_stable_canonical_serialization_and_hash() -> None:
    first = build_auth_audit_event_envelope(
        event_type="auth_phone_change_requested",
        event_time="2026-04-01T10:04:00Z",
        user_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        tenant_id="default_tenant",
        session_id=None,
        correlation_id="corr-auth-audit-005",
        trace_id="trace-auth-audit-005",
        action_status="requested",
        reason_code=None,
        details={"phone_change_state": "pending_confirmation"},
    ).model_dump(mode="json")
    second = build_auth_audit_event_envelope(
        event_type="auth_phone_change_requested",
        event_time="2026-04-01T10:04:00Z",
        user_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        tenant_id="default_tenant",
        session_id=None,
        correlation_id="corr-auth-audit-005",
        trace_id="trace-auth-audit-005",
        action_status="requested",
        reason_code=None,
        details={"phone_change_state": "pending_confirmation"},
    ).model_dump(mode="json")
    assert first["evidence_hash"] == second["evidence_hash"]
    assert canonical_json_dumps(first) == canonical_json_dumps(second)


@pytest.mark.parametrize(
    ("event_type", "expected_message"),
    [
        ("unsupported-event", "auth_audit_unsupported_event_type:unsupported-event"),
    ],
)
def test_invalid_event_type_fails_deterministically(
    event_type: str,
    expected_message: str,
) -> None:
    with pytest.raises(ValueError) as error_info:
        build_auth_audit_event_envelope(
            event_type=event_type,
            event_time="2026-04-01T10:05:00Z",
            user_id=None,
            tenant_id="default_tenant",
            session_id=None,
            correlation_id="corr-auth-audit-006",
            trace_id="trace-auth-audit-006",
            action_status="failed",
            reason_code="auth_context_missing",
            details={},
        )
    assert str(error_info.value) == expected_message


def _build_validator() -> Draft202012Validator:
    schema = _load_schema()
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return cast(
        Draft202012Validator,
        validator_class(schema, format_checker=FormatChecker()),
    )


def _load_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(AUTH_AUDIT_SCHEMA_PATH.read_text(encoding="utf-8")))
