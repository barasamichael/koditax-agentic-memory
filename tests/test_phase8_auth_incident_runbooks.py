"""Integrity checks for phase 8.7.5 auth incident runbook artifacts."""

from __future__ import annotations

from pathlib import Path

ACCOUNT_TAKEOVER_RUNBOOK_PATH = Path("docs/operations/runbooks/auth-account-takeover-runbook.md")
OTP_ABUSE_RUNBOOK_PATH = Path("docs/operations/runbooks/auth-otp-abuse-runbook.md")
OAUTH_COMPROMISE_RUNBOOK_PATH = Path(
    "docs/operations/runbooks/auth-oauth-provider-compromise-runbook.md"
)
AUTH_OUTAGE_RUNBOOK_PATH = Path("docs/operations/runbooks/auth-outage-runbook.md")
ESCALATION_MATRIX_PATH = Path("docs/operations/incident-response/auth-escalation-matrix.md")

RUNBOOK_PATHS = (
    ACCOUNT_TAKEOVER_RUNBOOK_PATH,
    OTP_ABUSE_RUNBOOK_PATH,
    OAUTH_COMPROMISE_RUNBOOK_PATH,
    AUTH_OUTAGE_RUNBOOK_PATH,
)

REQUIRED_RUNBOOK_SECTIONS = {
    "## trigger_conditions",
    "## detection_signals",
    "## immediate_containment_actions",
    "## evidence_collection",
    "## service_recovery_steps",
    "## customer_communication_guidance",
    "## post_incident_actions",
}

REQUIRED_ESCALATION_FIELDS = {
    "severity",
    "on_call_owner",
    "max_response_time",
    "max_containment_start_time",
    "handoff_criteria",
    "linked_runbook",
}


def test_auth_incident_runbook_artifacts_exist_and_have_markdown_titles() -> None:
    for path in (*RUNBOOK_PATHS, ESCALATION_MATRIX_PATH):
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert content.strip().startswith("# ")


def test_each_auth_runbook_contains_required_sections() -> None:
    for path in RUNBOOK_PATHS:
        content = path.read_text(encoding="utf-8").lower()
        for section in REQUIRED_RUNBOOK_SECTIONS:
            assert section in content


def test_escalation_matrix_contains_mandatory_severity_ownership_and_sla_fields() -> None:
    content = ESCALATION_MATRIX_PATH.read_text(encoding="utf-8").lower()
    assert "## severity_matrix" in content
    assert "`sev1`" in content
    assert "`sev2`" in content
    assert "`sev3`" in content
    for field_name in REQUIRED_ESCALATION_FIELDS:
        assert field_name in content


def test_runbooks_reference_canonical_auth_evidence_fields_and_reason_codes() -> None:
    for path in RUNBOOK_PATHS:
        content = path.read_text(encoding="utf-8").lower()
        assert "trace_id" in content
        assert "correlation_id" in content
        assert "reason_code" in content
        assert "error_code" in content


def test_runbooks_reference_auth_audit_taxonomy_and_metrics_signals() -> None:
    account_takeover_content = ACCOUNT_TAKEOVER_RUNBOOK_PATH.read_text(encoding="utf-8").lower()
    otp_abuse_content = OTP_ABUSE_RUNBOOK_PATH.read_text(encoding="utf-8").lower()
    oauth_compromise_content = OAUTH_COMPROMISE_RUNBOOK_PATH.read_text(encoding="utf-8").lower()
    auth_outage_content = AUTH_OUTAGE_RUNBOOK_PATH.read_text(encoding="utf-8").lower()

    assert "auth_login_failed" in account_takeover_content
    assert "auth_password_reset_completed" in account_takeover_content
    assert "auth.login.failure_total" in account_takeover_content

    assert "auth_otp_challenge_issued" in otp_abuse_content
    assert "auth.otp.verify_failure_total" in otp_abuse_content
    assert "otp_attempt_limit_exceeded" in otp_abuse_content

    assert "oauth_token_validation_failed" in oauth_compromise_content
    assert "auth.oauth.failure_total" in oauth_compromise_content
    assert "oauth_invalid_signature" in oauth_compromise_content

    assert "auth_login_failed" in auth_outage_content
    assert "auth.session.refresh_failure_total" in auth_outage_content
    assert "session_absolute_expiry" in auth_outage_content
