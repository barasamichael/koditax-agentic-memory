"""FR-mapped acceptance matrix for auth requirements FR-AUTH-001..013."""

from __future__ import annotations

import re
import sys
from pathlib import Path
import subprocess
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class FRAcceptanceCase:
    """Represent explicit positive/negative acceptance evidence per FR."""

    requirement_id: str
    positive_nodeids: tuple[str, ...]
    negative_nodeids: tuple[str, ...]


FR_AUTH_ACCEPTANCE_CASES: tuple[FRAcceptanceCase, ...] = (
    FRAcceptanceCase(
        requirement_id="FR-AUTH-001",
        positive_nodeids=(
            "tests/test_auth_registration.py::test_registration_positive_request_succeeds",
        ),
        negative_nodeids=(
            "tests/test_auth_registration.py::test_registration_invalid_kra_pin_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-002",
        positive_nodeids=(
            "tests/test_auth_phone_verification.py::test_phone_verification_positive_flow_updates_registration_state",
        ),
        negative_nodeids=(
            "tests/test_auth_phone_verification.py::test_phone_verification_invalid_otp_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-003",
        positive_nodeids=(
            "tests/test_auth_phone_verification.py::test_phone_verification_resend_after_min_interval_succeeds",
        ),
        negative_nodeids=(
            "tests/test_auth_phone_verification.py::test_phone_verification_resend_throttle_is_enforced_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-004",
        positive_nodeids=(
            "tests/test_auth_login.py::test_login_requires_step_up_before_session_issuance",
        ),
        negative_nodeids=(
            "tests/test_auth_login.py::test_login_unknown_phone_identifier_returns_canonical_invalid_credentials",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-005",
        positive_nodeids=(
            "tests/test_auth_password_reset.py::test_password_reset_positive_challenge_updates_password",
        ),
        negative_nodeids=(
            "tests/test_auth_password_reset.py::test_password_reset_invalid_challenge_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-006",
        positive_nodeids=(
            "tests/test_auth_email_verification.py::test_email_verification_positive_flow_updates_registration_state",
        ),
        negative_nodeids=(
            "tests/test_auth_registration.py::test_registration_invalid_email_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-007",
        positive_nodeids=(
            "tests/test_auth_provider_resilience.py::test_sms_failure_with_allowed_email_fallback_succeeds",
        ),
        negative_nodeids=(
            "tests/test_auth_provider_resilience.py::test_sms_permanent_failure_maps_to_canonical_error",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-008",
        positive_nodeids=(
            "tests/test_auth_phone_change.py::test_phone_change_positive_flow_updates_phone_and_login_identifier",
        ),
        negative_nodeids=(
            "tests/test_auth_phone_change.py::test_phone_change_invalid_step_up_proof_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-009",
        positive_nodeids=(
            "tests/test_auth_password_reset.py::test_password_reset_positive_challenge_updates_password",
        ),
        negative_nodeids=(
            "tests/test_auth_password_reset.py::test_password_reset_weak_new_password_is_rejected_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-010",
        positive_nodeids=(
            "tests/test_auth_login.py::test_login_with_valid_credentials_and_step_up_otp_issues_authenticated_session",
        ),
        negative_nodeids=(
            "tests/test_auth_login.py::test_login_requires_step_up_before_session_issuance",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-011",
        positive_nodeids=(
            "tests/test_auth_sessions.py::test_session_within_inactivity_and_absolute_bounds_is_active",
        ),
        negative_nodeids=(
            "tests/test_auth_sessions.py::test_session_inactivity_timeout_is_enforced_deterministically",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-012",
        positive_nodeids=(
            "tests/test_auth_login_lockout.py::test_login_lockout_expires_and_returns_to_pending_step_up",
        ),
        negative_nodeids=(
            "tests/test_auth_login_lockout.py::test_login_lockout_threshold_and_active_state_are_deterministic",
        ),
    ),
    FRAcceptanceCase(
        requirement_id="FR-AUTH-013",
        positive_nodeids=(
            "tests/test_auth_role_change_governance.py::test_admin_role_change_succeeds_and_emits_immutable_audit_record",
        ),
        negative_nodeids=(
            "tests/test_auth_role_change_governance.py::test_unauthorized_non_admin_role_change_is_rejected_deterministically",
        ),
    ),
)

EXPECTED_FR_AUTH_IDS: frozenset[str] = frozenset(
    {
        "FR-AUTH-001",
        "FR-AUTH-002",
        "FR-AUTH-003",
        "FR-AUTH-004",
        "FR-AUTH-005",
        "FR-AUTH-006",
        "FR-AUTH-007",
        "FR-AUTH-008",
        "FR-AUTH-009",
        "FR-AUTH-010",
        "FR-AUTH-011",
        "FR-AUTH-012",
        "FR-AUTH-013",
    }
)


def test_fr_auth_acceptance_matrix_has_exact_requirement_coverage() -> None:
    observed = {case.requirement_id for case in FR_AUTH_ACCEPTANCE_CASES}
    assert observed == EXPECTED_FR_AUTH_IDS


@pytest.mark.parametrize("case", FR_AUTH_ACCEPTANCE_CASES, ids=lambda case: case.requirement_id)
def test_fr_auth_acceptance_matrix_declares_positive_and_negative_paths(
    case: FRAcceptanceCase,
) -> None:
    assert case.positive_nodeids
    assert case.negative_nodeids
    all_nodeids = case.positive_nodeids + case.negative_nodeids
    assert len(all_nodeids) == len(set(all_nodeids))


@pytest.mark.parametrize(
    "nodeid",
    tuple(
        nodeid
        for case in FR_AUTH_ACCEPTANCE_CASES
        for nodeid in (case.positive_nodeids + case.negative_nodeids)
    ),
)
def test_fr_auth_acceptance_matrix_nodeids_exist(nodeid: str) -> None:
    test_file, separator, test_name = nodeid.partition("::")
    assert separator == "::"
    assert test_name
    test_path = Path(test_file)
    assert test_path.exists()
    test_content = test_path.read_text(encoding="utf-8")
    assert re.search(rf"^def {re.escape(test_name)}\(", test_content, re.MULTILINE)


def test_fr_auth_acceptance_matrix_runs_mapped_acceptance_evidence() -> None:
    unique_nodeids = sorted(
        {
            nodeid
            for case in FR_AUTH_ACCEPTANCE_CASES
            for nodeid in (case.positive_nodeids + case.negative_nodeids)
        }
    )
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *unique_nodeids],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 0, (
        "FR-AUTH mapped acceptance evidence execution failed.\n"
        f"STDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
    )
