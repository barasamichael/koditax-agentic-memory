"""Governance and runtime coverage tests for auth audit taxonomy."""

from __future__ import annotations

from uuid import UUID
from typing import cast
from pathlib import Path

from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.main import AUTH_AUDIT_EVENT_TYPES
from services.auth.app.main import list_auth_audit_events
from services.auth.app.main import reset_auth_audit_events
from services.auth.app.config import get_auth_login_lockout_max_failed_attempts
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.password_reset import InMemoryPasswordResetStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore

GOVERNANCE_PATH = Path("docs/governance/phase-8-auth-audit-taxonomy.md")

REQUIRED_TAXONOMY_IDS = {
    "auth_registration_requested",
    "auth_registration_verified",
    "auth_login_succeeded",
    "auth_login_failed",
    "auth_lockout_applied",
    "auth_session_refreshed",
    "auth_session_revoked",
    "auth_password_reset_requested",
    "auth_password_reset_completed",
    "auth_phone_change_requested",
    "auth_phone_change_confirmed",
    "auth_account_deletion_requested",
    "auth_account_deletion_confirmed",
    "auth_account_deletion_executed",
}


def test_governance_taxonomy_doc_contains_required_event_ids() -> None:
    content = GOVERNANCE_PATH.read_text(encoding="utf-8")
    for event_id in sorted(REQUIRED_TAXONOMY_IDS):
        assert event_id in content


def test_runtime_taxonomy_set_includes_required_event_ids() -> None:
    assert REQUIRED_TAXONOMY_IDS.issubset(AUTH_AUDIT_EVENT_TYPES)


def test_representative_security_flows_emit_canonical_auth_audit_events() -> None:
    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_store = InMemoryEmailVerificationStore()
    phone_store = InMemoryPhoneVerificationStore()
    reset_store = InMemoryPasswordResetStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_store
    app.state.phone_verification_store = phone_store
    app.state.password_reset_store = reset_store
    reset_auth_audit_events(app_instance=app)

    with TestClient(app) as client:
        register_response = client.post(
            "/v1/auth/register",
            headers={"X-Correlation-ID": "auth-audit-reg-corr"},
            json={
                "email": "audit.taxonomy.user@example.com",
                "phone_number": "+254722660001",
                "password": "StrongPassw0rd!Audit",
                "role": "IndividualTaxpayer",
                "kra_pin": "A123456789Z",
            },
        )
        assert register_response.status_code == 201
        user_id = UUID(cast(str, register_response.json()["user_id"]))

        challenge_response = client.post(
            "/v1/auth/otp/challenges",
            headers={
                "X-Correlation-ID": "auth-audit-otp-issue-corr",
                "Idempotency-Key": "auth-audit-otp-issue-idem",
            },
            json={
                "purpose": "registration_verify",
                "channel": "email",
                "email": "audit.taxonomy.user@example.com",
            },
        )
        assert challenge_response.status_code == 201
        email_challenge_id = UUID(cast(str, challenge_response.json()["challenge_id"]))
        email_otp = email_store.get_otp_code_for_challenge(challenge_id=email_challenge_id)

        verify_response = client.post(
            "/v1/auth/otp/verify",
            headers={"X-Correlation-ID": "auth-audit-otp-verify-corr"},
            json={
                "challenge_id": str(email_challenge_id),
                "otp_code": email_otp,
            },
        )
        assert verify_response.status_code == 200

        pending_login = client.post(
            "/v1/auth/login",
            headers={
                "X-Correlation-ID": "auth-audit-login-pending-corr",
                "X-Forwarded-For": "203.0.113.44",
            },
            json={
                "login_id": "+254722660001",
                "password": "StrongPassw0rd!Audit",
            },
        )
        assert pending_login.status_code == 200
        pending_body = pending_login.json()
        assert pending_body["status"] == "pending_step_up"
        phone_challenge_id = UUID(cast(str, pending_body["step_up_challenge_id"]))
        phone_otp = phone_store.get_otp_code_for_challenge(challenge_id=phone_challenge_id)

        authenticated_login = client.post(
            "/v1/auth/login",
            headers={
                "X-Correlation-ID": "auth-audit-login-authenticated-corr",
                "X-Forwarded-For": "203.0.113.44",
            },
            json={
                "login_id": "+254722660001",
                "password": "StrongPassw0rd!Audit",
                "step_up_challenge_id": str(phone_challenge_id),
                "step_up_otp_code": phone_otp,
            },
        )
        assert authenticated_login.status_code == 200
        login_body = authenticated_login.json()
        session_id = UUID(cast(str, login_body["session"]["session_id"]))
        refresh_token = cast(str, login_body["refresh_token"])

        refresh_response = client.post(
            "/v1/auth/refresh",
            headers={"X-Correlation-ID": "auth-audit-refresh-corr"},
            json={"refresh_token": refresh_token},
        )
        assert refresh_response.status_code == 200

        logout_response = client.post(
            "/v1/auth/logout",
            headers={
                "X-Correlation-ID": "auth-audit-logout-corr",
                "Authorization": f"Bearer user_id={user_id}",
            },
            json={
                "revoke_scope": "single_session",
                "target_session_id": str(session_id),
            },
        )
        assert logout_response.status_code == 200

        reset_issue_response = client.post(
            "/v1/auth/password-reset/initiate",
            headers={
                "X-Correlation-ID": "auth-audit-reset-init-corr",
                "Idempotency-Key": "auth-audit-reset-init-idem",
            },
            json={
                "purpose": "password_reset",
                "channel": "email",
                "email": "audit.taxonomy.user@example.com",
            },
        )
        assert reset_issue_response.status_code == 201
        reset_challenge_id = UUID(cast(str, reset_issue_response.json()["challenge_id"]))
        reset_code = reset_store.get_reset_code_for_challenge(challenge_id=reset_challenge_id)

        reset_confirm_response = client.post(
            "/v1/auth/password-reset/confirm",
            headers={"X-Correlation-ID": "auth-audit-reset-confirm-corr"},
            json={
                "challenge_id": str(reset_challenge_id),
                "reset_code": reset_code,
                "new_password": "B3tterPassw0rd!Audit",
            },
        )
        assert reset_confirm_response.status_code == 200

        invalid_login_headers = {
            "X-Correlation-ID": "auth-audit-login-failure-corr",
            "X-Forwarded-For": "203.0.113.77",
        }
        invalid_login_payload = {"login_id": "+254722660001", "password": "WrongAuditPassw0rd!"}
        max_attempts = get_auth_login_lockout_max_failed_attempts()
        for _ in range(max_attempts):
            client.post("/v1/auth/login", headers=invalid_login_headers, json=invalid_login_payload)

    emitted_events = list_auth_audit_events(app_instance=app)
    emitted_types = {event.event_type for event in emitted_events}
    assert {
        "auth_registration_requested",
        "auth_otp_challenge_issued",
        "auth_otp_challenge_verified",
        "auth_registration_verified",
        "auth_login_succeeded",
        "auth_session_refreshed",
        "auth_session_revoked",
        "auth_password_reset_requested",
        "auth_password_reset_completed",
        "auth_login_failed",
        "auth_lockout_applied",
    }.issubset(emitted_types)

    serialized_events = "\n".join(event.model_dump_json() for event in emitted_events)
    assert "StrongPassw0rd!Audit" not in serialized_events
    assert "WrongAuditPassw0rd!" not in serialized_events
    assert reset_code not in serialized_events
    assert phone_otp not in serialized_events

    for event in emitted_events:
        assert event.schema_version == "1.0.0"
        assert event.correlation_id
        assert event.trace_id
        assert event.tenant_id == "default_tenant"
        assert len(event.evidence_hash) == 64

    reset_default_registration_store()
