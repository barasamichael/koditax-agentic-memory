"""Provide deterministic step-up auth challenge and verification interfaces for pilot workflow."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
import hashlib
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.trace_context import build_optional_trace_id

TEST_STEP_UP_PROOF_CODE = "246810"
CHALLENGE_TTL_MINUTES = 5
DEFAULT_ALLOWED_ATTEMPTS = 3

ChallengeStatus = Literal["issued", "verified", "failed", "expired"]
VerificationStatus = Literal["verified", "failed", "expired", "invalid"]


class StepUpChallengeContext(TypedDict):
    """Represent deterministic context bound to one issued step-up challenge."""

    principal_user_id: str | None
    tenant_id: str | None
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    action_type: str | None
    risk_class: str | None
    action_reference_id: str | None
    step_up_purpose: str | None


class StepUpChallengeRecord(TypedDict):
    """Represent deterministic step-up challenge lifecycle record."""

    challenge_id: str
    challenge_status: ChallengeStatus
    issued_at: str
    expires_at: str
    allowed_attempts: int
    attempts_used: int
    context: StepUpChallengeContext
    correlation_id: str | None
    trace_id: str | None


class StepUpChallengeIssueEnvelope(TypedDict):
    """Represent deterministic challenge issuance output for step-up-required actions."""

    challenge_id: str
    challenge_status: str
    expires_at: str
    allowed_attempts: int
    attempts_used: int
    reason_code: str
    message: str
    challenge_record: StepUpChallengeRecord
    trace_id: str | None


class StepUpVerificationEnvelope(TypedDict):
    """Represent deterministic verification output for one submitted proof."""

    verification_status: VerificationStatus
    challenge_id: str | None
    reason_code: str
    message: str
    challenge_record: StepUpChallengeRecord | None
    trace_id: str | None


def issue_income_tax_step_up_challenge(
    *,
    policy_decision: Mapping[str, object],
    issued_at: str,
    allowed_attempts: int = DEFAULT_ALLOWED_ATTEMPTS,
) -> StepUpChallengeIssueEnvelope:
    """Issue deterministic challenge record for one step-up-required policy decision."""

    context = _extract_context(policy_decision)
    correlation_id = _optional_string(policy_decision.get("correlation_id"))
    trace_id = _optional_string(policy_decision.get("trace_id")) or build_optional_trace_id(
        correlation_id
    )
    issued_at_dt = _parse_timestamp(issued_at)
    expires_at = (issued_at_dt + timedelta(minutes=CHALLENGE_TTL_MINUTES)).isoformat()
    challenge_id = _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "income_tax_step_up_challenge",
                "context": context,
                "correlation_id": correlation_id,
                "issued_at": issued_at,
                "allowed_attempts": allowed_attempts,
            }
        )
    )
    challenge_record: StepUpChallengeRecord = {
        "challenge_id": challenge_id,
        "challenge_status": "issued",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "allowed_attempts": allowed_attempts,
        "attempts_used": 0,
        "context": context,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    envelope: StepUpChallengeIssueEnvelope = {
        "challenge_id": challenge_id,
        "challenge_status": "issued",
        "expires_at": expires_at,
        "allowed_attempts": allowed_attempts,
        "attempts_used": 0,
        "reason_code": "step_up_challenge_issued",
        "message": "Step-up challenge issued. Submit proof code to continue.",
        "challenge_record": challenge_record,
        "trace_id": trace_id,
    }
    emit_income_tax_audit_event(
        event_type="step_up_challenge_issued",
        status="issued",
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=context["supported_lane_id"],
        historical_version_id=context["historical_version_id"],
        tax_year=context["tax_year"],
        context={
            "challenge_id": challenge_id,
            "allowed_attempts": allowed_attempts,
            "principal_user_id": context["principal_user_id"],
            "tenant_id": context["tenant_id"],
            "action_type": context["action_type"],
            "risk_class": context["risk_class"],
            "action_reference_id": context["action_reference_id"],
            "step_up_purpose": context["step_up_purpose"],
        },
        event_time=issued_at,
    )
    return envelope


def verify_income_tax_step_up_challenge(
    *,
    challenge_record: Mapping[str, object] | None,
    proof_code: str,
    verified_at: str,
) -> StepUpVerificationEnvelope:
    """Verify deterministic proof against local step-up test mechanism."""

    if challenge_record is None:
        return _verification_response(
            verification_status="invalid",
            challenge_id=None,
            reason_code="challenge_missing",
            message="Step-up challenge record is missing.",
            challenge_record=None,
            trace_id=None,
            correlation_id=None,
        )

    challenge_id = _optional_string(challenge_record.get("challenge_id"))
    challenge_status = _optional_string(challenge_record.get("challenge_status"))
    issued_at = _optional_string(challenge_record.get("issued_at"))
    expires_at = _optional_string(challenge_record.get("expires_at"))
    allowed_attempts = _optional_int(challenge_record.get("allowed_attempts"))
    attempts_used = _optional_int(challenge_record.get("attempts_used"))
    context_raw = challenge_record.get("context")
    context = (
        cast(StepUpChallengeContext, context_raw) if isinstance(context_raw, Mapping) else None
    )
    trace_id = _optional_string(challenge_record.get("trace_id")) or build_optional_trace_id(
        _optional_string(challenge_record.get("correlation_id"))
    )

    if (
        challenge_id is None
        or challenge_status not in {"issued", "verified", "failed", "expired"}
        or issued_at is None
        or expires_at is None
        or allowed_attempts is None
        or attempts_used is None
        or context is None
    ):
        return _verification_response(
            verification_status="invalid",
            challenge_id=challenge_id,
            reason_code="challenge_invalid",
            message="Step-up challenge record is malformed.",
            challenge_record=None,
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    try:
        verified_at_dt = _parse_timestamp(verified_at)
        expires_at_dt = _parse_timestamp(expires_at)
    except ValueError:
        return _verification_response(
            verification_status="invalid",
            challenge_id=challenge_id,
            reason_code="timestamp_invalid",
            message="Verification timestamp is invalid.",
            challenge_record=None,
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    if challenge_status != "issued":
        return _verification_response(
            verification_status="invalid",
            challenge_id=challenge_id,
            reason_code="challenge_not_issued",
            message="Step-up challenge is not in issued state.",
            challenge_record=_as_step_up_challenge_record(challenge_record),
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    if verified_at_dt > expires_at_dt:
        expired_record = _updated_record(
            challenge_record=challenge_record,
            challenge_status="expired",
            attempts_used=attempts_used,
        )
        return _verification_response(
            verification_status="expired",
            challenge_id=challenge_id,
            reason_code="challenge_expired",
            message="Step-up challenge has expired.",
            challenge_record=expired_record,
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    if attempts_used >= allowed_attempts:
        failed_record = _updated_record(
            challenge_record=challenge_record,
            challenge_status="failed",
            attempts_used=attempts_used,
        )
        return _verification_response(
            verification_status="failed",
            challenge_id=challenge_id,
            reason_code="attempts_exceeded",
            message="Step-up verification attempts have been exceeded.",
            challenge_record=failed_record,
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    if proof_code != TEST_STEP_UP_PROOF_CODE:
        next_attempts_used = attempts_used + 1
        status: ChallengeStatus = "issued"
        reason_code = "proof_invalid"
        message = "Step-up proof code is invalid."
        if next_attempts_used >= allowed_attempts:
            status = "failed"
            reason_code = "attempts_exceeded"
            message = "Step-up verification attempts have been exceeded."
        failed_record = _updated_record(
            challenge_record=challenge_record,
            challenge_status=status,
            attempts_used=next_attempts_used,
        )
        return _verification_response(
            verification_status="failed",
            challenge_id=challenge_id,
            reason_code=reason_code,
            message=message,
            challenge_record=failed_record,
            trace_id=trace_id,
            correlation_id=_optional_string(challenge_record.get("correlation_id")),
        )

    verified_record = _updated_record(
        challenge_record=challenge_record,
        challenge_status="verified",
        attempts_used=attempts_used,
    )
    return _verification_response(
        verification_status="verified",
        challenge_id=challenge_id,
        reason_code="proof_verified",
        message="Step-up proof verified successfully.",
        challenge_record=verified_record,
        trace_id=trace_id,
        correlation_id=_optional_string(challenge_record.get("correlation_id")),
    )


def _extract_context(policy_decision: Mapping[str, object]) -> StepUpChallengeContext:
    decision_context = policy_decision.get("decision_context")
    if not isinstance(decision_context, Mapping):
        decision_context = {}
    typed_context = cast(Mapping[str, object], decision_context)
    return {
        "principal_user_id": _optional_string(typed_context.get("principal_user_id")),
        "tenant_id": _optional_string(typed_context.get("tenant_id")),
        "supported_lane_id": _optional_string(typed_context.get("supported_lane_id")),
        "historical_version_id": _optional_string(typed_context.get("historical_version_id")),
        "tax_year": _optional_int(typed_context.get("tax_year")),
        "action_type": _optional_string(typed_context.get("action_type")),
        "risk_class": _optional_string(typed_context.get("risk_class")),
        "action_reference_id": _optional_string(typed_context.get("action_reference_id")),
        "step_up_purpose": _optional_string(typed_context.get("step_up_purpose")),
    }


def _updated_record(
    *,
    challenge_record: Mapping[str, object],
    challenge_status: ChallengeStatus,
    attempts_used: int,
) -> StepUpChallengeRecord:
    record = _as_step_up_challenge_record(challenge_record)
    return {
        **record,
        "challenge_status": challenge_status,
        "attempts_used": attempts_used,
    }


def _as_step_up_challenge_record(challenge_record: Mapping[str, object]) -> StepUpChallengeRecord:
    context_raw = challenge_record.get("context")
    if isinstance(context_raw, Mapping):
        context_map = cast(Mapping[str, object], context_raw)
    else:
        context_map = cast(Mapping[str, object], {})
    context: StepUpChallengeContext = {
        "principal_user_id": _optional_string(context_map.get("principal_user_id")),
        "tenant_id": _optional_string(context_map.get("tenant_id")),
        "supported_lane_id": _optional_string(context_map.get("supported_lane_id")),
        "historical_version_id": _optional_string(context_map.get("historical_version_id")),
        "tax_year": _optional_int(context_map.get("tax_year")),
        "action_type": _optional_string(context_map.get("action_type")),
        "risk_class": _optional_string(context_map.get("risk_class")),
        "action_reference_id": _optional_string(context_map.get("action_reference_id")),
        "step_up_purpose": _optional_string(context_map.get("step_up_purpose")),
    }
    return {
        "challenge_id": _optional_string(challenge_record.get("challenge_id")) or "",
        "challenge_status": cast(
            ChallengeStatus,
            _optional_string(challenge_record.get("challenge_status")) or "issued",
        ),
        "issued_at": _optional_string(challenge_record.get("issued_at")) or "",
        "expires_at": _optional_string(challenge_record.get("expires_at")) or "",
        "allowed_attempts": _optional_int(challenge_record.get("allowed_attempts")) or 0,
        "attempts_used": _optional_int(challenge_record.get("attempts_used")) or 0,
        "context": context,
        "correlation_id": _optional_string(challenge_record.get("correlation_id")),
        "trace_id": _optional_string(challenge_record.get("trace_id"))
        or build_optional_trace_id(_optional_string(challenge_record.get("correlation_id"))),
    }


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verification_response(
    *,
    verification_status: VerificationStatus,
    challenge_id: str | None,
    reason_code: str,
    message: str,
    challenge_record: StepUpChallengeRecord | None,
    trace_id: str | None,
    correlation_id: str | None,
) -> StepUpVerificationEnvelope:
    context = challenge_record["context"] if challenge_record is not None else None
    emit_income_tax_audit_event(
        event_type="step_up_verification_result",
        status=verification_status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=(context["supported_lane_id"] if context is not None else None),
        historical_version_id=(context["historical_version_id"] if context is not None else None),
        tax_year=(context["tax_year"] if context is not None else None),
        context={
            "challenge_id": challenge_id,
            "reason_code": reason_code,
            "principal_user_id": (context["principal_user_id"] if context is not None else None),
            "tenant_id": (context["tenant_id"] if context is not None else None),
            "action_type": (context["action_type"] if context is not None else None),
            "risk_class": (context["risk_class"] if context is not None else None),
            "action_reference_id": (
                context["action_reference_id"] if context is not None else None
            ),
            "step_up_purpose": (context["step_up_purpose"] if context is not None else None),
        },
    )
    return {
        "verification_status": verification_status,
        "challenge_id": challenge_id,
        "reason_code": reason_code,
        "message": message,
        "challenge_record": challenge_record,
        "trace_id": trace_id,
    }
