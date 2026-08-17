"""Bind verified step-up proof to deterministic action execution context."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
import hashlib
from datetime import datetime
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.trace_context import build_optional_trace_id

ProofStatus = Literal["bound", "consumed", "expired", "invalid"]


class StepUpProofBindingContext(TypedDict):
    """Represent deterministic context fields required for one bound proof."""

    principal_user_id: str | None
    tenant_id: str | None
    action_type: str | None
    risk_class: str | None
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    action_reference_id: str | None
    step_up_purpose: str | None
    correlation_id: str | None
    trace_id: str | None


class StepUpProofBindingRecord(TypedDict):
    """Represent one deterministic bound proof record."""

    proof_binding_id: str
    challenge_id: str
    proof_status: ProofStatus
    issued_at: str
    bound_at: str
    expires_at: str
    consumed_at: str | None
    context_hash: str
    context: StepUpProofBindingContext


class StepUpProofRejectionEnvelope(TypedDict):
    """Represent canonical deterministic rejection envelope for proof-binding failures."""

    error_code: str
    message: str
    reason_code: str
    reason: str
    rejected_context: StepUpProofBindingContext
    required_controls: list[str]
    next_allowed_actions: list[str]
    correlation_id: str | None
    trace_id: str | None


class StepUpProofBindResult(TypedDict):
    """Represent deterministic bind result for verified step-up proof."""

    binding_status: str
    reason_code: str
    reason: str
    proof_binding: StepUpProofBindingRecord | None
    error: StepUpProofRejectionEnvelope | None


class StepUpProofAuthorizationResult(TypedDict):
    """Represent deterministic authorization check result using bound step-up proof."""

    authorization_status: str
    reason_code: str
    reason: str
    execution_authorized: bool
    proof_binding: StepUpProofBindingRecord | None
    error: StepUpProofRejectionEnvelope | None


def bind_income_tax_verified_step_up_proof(
    *,
    policy_decision: Mapping[str, object],
    verification_result: Mapping[str, object],
    bound_at: str,
) -> StepUpProofBindResult:
    """Bind a verified proof to exact action context with deterministic proof-binding identity."""

    expected_context = _context_from_policy_decision(policy_decision)
    if policy_decision.get("policy_decision") != "step_up_required":
        error = _proof_rejection(
            reason_code="step_up_not_required_for_action",
            reason=(
                "Step-up proof binding is only allowed for step-up-required "
                "action policy decisions."
            ),
            context=expected_context,
            required_controls=[],
            next_allowed_actions=["revise_input", "reject"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }

    if verification_result.get("verification_status") != "verified":
        error = _proof_rejection(
            reason_code="step_up_verification_not_verified",
            reason="Step-up proof cannot be bound because verification did not succeed.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }

    challenge_record_raw = verification_result.get("challenge_record")
    if not isinstance(challenge_record_raw, Mapping):
        error = _proof_rejection(
            reason_code="step_up_proof_missing",
            reason="Verified step-up proof is missing challenge record context.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }
    challenge_record = cast(Mapping[str, object], challenge_record_raw)
    challenge_context = _context_from_challenge_record(challenge_record)
    if challenge_context != expected_context:
        error = _proof_rejection(
            reason_code="step_up_proof_context_mismatch",
            reason=(
                "Verified step-up proof context does not match pending action execution context."
            ),
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }

    challenge_id = _optional_string(challenge_record.get("challenge_id"))
    issued_at = _optional_string(challenge_record.get("issued_at"))
    expires_at = _optional_string(challenge_record.get("expires_at"))
    challenge_status = _optional_string(challenge_record.get("challenge_status"))
    if (
        challenge_id is None
        or issued_at is None
        or expires_at is None
        or challenge_status != "verified"
        or not _context_has_required_binding_fields(challenge_context)
    ):
        error = _proof_rejection(
            reason_code="step_up_proof_invalid",
            reason="Verified step-up challenge record is malformed for proof binding.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }

    try:
        bound_at_dt = _parse_timestamp(bound_at)
        expires_at_dt = _parse_timestamp(expires_at)
        _parse_timestamp(issued_at)
    except ValueError:
        error = _proof_rejection(
            reason_code="step_up_proof_invalid",
            reason="Verified step-up challenge timestamps are invalid for proof binding.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": None,
            "error": error,
        }

    if bound_at_dt > expires_at_dt:
        expired_proof = _build_proof_binding(
            challenge_id=challenge_id,
            proof_status="expired",
            issued_at=issued_at,
            bound_at=bound_at,
            expires_at=expires_at,
            consumed_at=None,
            context=expected_context,
        )
        error = _proof_rejection(
            reason_code="step_up_proof_expired",
            reason="Verified step-up proof is already expired at binding time.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "binding_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "proof_binding": expired_proof,
            "error": error,
        }

    proof_binding = _build_proof_binding(
        challenge_id=challenge_id,
        proof_status="bound",
        issued_at=issued_at,
        bound_at=bound_at,
        expires_at=expires_at,
        consumed_at=None,
        context=expected_context,
    )
    return {
        "binding_status": "bound",
        "reason_code": "step_up_proof_bound",
        "reason": "Verified step-up proof is now bound to deterministic action execution context.",
        "proof_binding": proof_binding,
        "error": None,
    }


def authorize_income_tax_action_with_bound_step_up_proof(
    *,
    policy_decision: Mapping[str, object],
    proof_binding: Mapping[str, object] | None,
    authorized_at: str,
) -> StepUpProofAuthorizationResult:
    """Authorize side-effect-capable progression only when valid bound proof context is present."""

    expected_context = _context_from_policy_decision(policy_decision)
    if policy_decision.get("policy_decision") != "step_up_required":
        return {
            "authorization_status": "authorized",
            "reason_code": "step_up_not_required_for_action",
            "reason": "Step-up proof is not required for this action policy decision.",
            "execution_authorized": True,
            "proof_binding": None,
            "error": None,
        }

    if proof_binding is None:
        error = _proof_rejection(
            reason_code="step_up_proof_missing",
            reason="Step-up-required action cannot proceed without bound step-up proof.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": None,
            "error": error,
        }

    typed_binding = _as_proof_binding_record(proof_binding)
    if typed_binding is None:
        error = _proof_rejection(
            reason_code="step_up_proof_invalid",
            reason="Bound step-up proof record is malformed.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": None,
            "error": error,
        }

    if typed_binding["proof_status"] == "consumed":
        error = _proof_rejection(
            reason_code="step_up_proof_already_consumed",
            reason="Bound step-up proof has already been consumed and cannot be reused.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": typed_binding,
            "error": error,
        }

    if typed_binding["proof_status"] == "expired":
        error = _proof_rejection(
            reason_code="step_up_proof_expired",
            reason="Bound step-up proof has expired and cannot authorize action progression.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": typed_binding,
            "error": error,
        }

    if typed_binding["proof_status"] != "bound":
        error = _proof_rejection(
            reason_code="step_up_proof_invalid",
            reason="Bound step-up proof status is invalid for authorization.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": typed_binding,
            "error": error,
        }

    try:
        authorized_at_dt = _parse_timestamp(authorized_at)
        binding_expires_at_dt = _parse_timestamp(typed_binding["expires_at"])
        _parse_timestamp(typed_binding["issued_at"])
    except ValueError:
        error = _proof_rejection(
            reason_code="step_up_proof_invalid",
            reason="Bound step-up proof timestamps are invalid for authorization.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": typed_binding,
            "error": error,
        }

    if authorized_at_dt > binding_expires_at_dt:
        expired_binding = _build_proof_binding(
            challenge_id=typed_binding["challenge_id"],
            proof_status="expired",
            issued_at=typed_binding["issued_at"],
            bound_at=typed_binding["bound_at"],
            expires_at=typed_binding["expires_at"],
            consumed_at=typed_binding["consumed_at"],
            context=typed_binding["context"],
        )
        error = _proof_rejection(
            reason_code="step_up_proof_expired",
            reason="Bound step-up proof expired before authorization was attempted.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": expired_binding,
            "error": error,
        }

    if typed_binding["context"] != expected_context or typed_binding[
        "context_hash"
    ] != _context_hash(expected_context):
        error = _proof_rejection(
            reason_code="step_up_proof_context_mismatch",
            reason="Bound step-up proof context does not match action authorization context.",
            context=expected_context,
            required_controls=["step_up_auth"],
            next_allowed_actions=["request_step_up_auth", "reject", "revise_input"],
        )
        return {
            "authorization_status": "rejected",
            "reason_code": error["reason_code"],
            "reason": error["reason"],
            "execution_authorized": False,
            "proof_binding": typed_binding,
            "error": error,
        }

    consumed_binding = _build_proof_binding(
        challenge_id=typed_binding["challenge_id"],
        proof_status="consumed",
        issued_at=typed_binding["issued_at"],
        bound_at=typed_binding["bound_at"],
        expires_at=typed_binding["expires_at"],
        consumed_at=authorized_at,
        context=typed_binding["context"],
    )
    return {
        "authorization_status": "authorized",
        "reason_code": "step_up_proof_authorized",
        "reason": "Bound step-up proof validated and consumed for action authorization.",
        "execution_authorized": True,
        "proof_binding": consumed_binding,
        "error": None,
    }


def _build_proof_binding(
    *,
    challenge_id: str,
    proof_status: ProofStatus,
    issued_at: str,
    bound_at: str,
    expires_at: str,
    consumed_at: str | None,
    context: StepUpProofBindingContext,
) -> StepUpProofBindingRecord:
    context_hash = _context_hash(context)
    binding_id = _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "income_tax_step_up_proof_binding",
                "challenge_id": challenge_id,
                "context_hash": context_hash,
                "bound_at": bound_at,
            }
        )
    )
    return {
        "proof_binding_id": binding_id,
        "challenge_id": challenge_id,
        "proof_status": proof_status,
        "issued_at": issued_at,
        "bound_at": bound_at,
        "expires_at": expires_at,
        "consumed_at": consumed_at,
        "context_hash": context_hash,
        "context": context,
    }


def _context_hash(context: StepUpProofBindingContext) -> str:
    return _sha256_hex(canonical_json_dumps(context))


def _context_from_policy_decision(
    policy_decision: Mapping[str, object],
) -> StepUpProofBindingContext:
    raw_context = policy_decision.get("decision_context")
    if isinstance(raw_context, Mapping):
        context_map = cast(Mapping[str, object], raw_context)
    else:
        context_map = cast(Mapping[str, object], {})
    return {
        "principal_user_id": _optional_string(context_map.get("principal_user_id")),
        "tenant_id": _optional_string(context_map.get("tenant_id")),
        "action_type": _optional_string(context_map.get("action_type")),
        "risk_class": _optional_string(context_map.get("risk_class")),
        "supported_lane_id": _optional_string(context_map.get("supported_lane_id")),
        "historical_version_id": _optional_string(context_map.get("historical_version_id")),
        "tax_year": _optional_int(context_map.get("tax_year")),
        "action_reference_id": _optional_string(context_map.get("action_reference_id")),
        "step_up_purpose": _optional_string(context_map.get("step_up_purpose")),
        "correlation_id": _optional_string(policy_decision.get("correlation_id")),
        "trace_id": _optional_string(policy_decision.get("trace_id"))
        or build_optional_trace_id(_optional_string(policy_decision.get("correlation_id"))),
    }


def _context_from_challenge_record(
    challenge_record: Mapping[str, object],
) -> StepUpProofBindingContext:
    raw_context = challenge_record.get("context")
    if isinstance(raw_context, Mapping):
        context_map = cast(Mapping[str, object], raw_context)
    else:
        context_map = cast(Mapping[str, object], {})
    return {
        "principal_user_id": _optional_string(context_map.get("principal_user_id")),
        "tenant_id": _optional_string(context_map.get("tenant_id")),
        "action_type": _optional_string(context_map.get("action_type")),
        "risk_class": _optional_string(context_map.get("risk_class")),
        "supported_lane_id": _optional_string(context_map.get("supported_lane_id")),
        "historical_version_id": _optional_string(context_map.get("historical_version_id")),
        "tax_year": _optional_int(context_map.get("tax_year")),
        "action_reference_id": _optional_string(context_map.get("action_reference_id")),
        "step_up_purpose": _optional_string(context_map.get("step_up_purpose")),
        "correlation_id": _optional_string(challenge_record.get("correlation_id")),
        "trace_id": _optional_string(challenge_record.get("trace_id"))
        or build_optional_trace_id(_optional_string(challenge_record.get("correlation_id"))),
    }


def _as_proof_binding_record(
    value: Mapping[str, object],
) -> StepUpProofBindingRecord | None:
    context_raw = value.get("context")
    if not isinstance(context_raw, Mapping):
        return None
    context_map = cast(Mapping[str, object], context_raw)
    context: StepUpProofBindingContext = {
        "principal_user_id": _optional_string(context_map.get("principal_user_id")),
        "tenant_id": _optional_string(context_map.get("tenant_id")),
        "action_type": _optional_string(context_map.get("action_type")),
        "risk_class": _optional_string(context_map.get("risk_class")),
        "supported_lane_id": _optional_string(context_map.get("supported_lane_id")),
        "historical_version_id": _optional_string(context_map.get("historical_version_id")),
        "tax_year": _optional_int(context_map.get("tax_year")),
        "action_reference_id": _optional_string(context_map.get("action_reference_id")),
        "step_up_purpose": _optional_string(context_map.get("step_up_purpose")),
        "correlation_id": _optional_string(context_map.get("correlation_id")),
        "trace_id": _optional_string(context_map.get("trace_id"))
        or build_optional_trace_id(_optional_string(context_map.get("correlation_id"))),
    }
    proof_binding_id = _optional_string(value.get("proof_binding_id"))
    challenge_id = _optional_string(value.get("challenge_id"))
    proof_status = _optional_string(value.get("proof_status"))
    issued_at = _optional_string(value.get("issued_at"))
    bound_at = _optional_string(value.get("bound_at"))
    expires_at = _optional_string(value.get("expires_at"))
    consumed_at = _optional_string(value.get("consumed_at"))
    context_hash = _optional_string(value.get("context_hash"))
    if (
        proof_binding_id is None
        or challenge_id is None
        or proof_status not in {"bound", "consumed", "expired", "invalid"}
        or issued_at is None
        or bound_at is None
        or expires_at is None
        or context_hash is None
        or not _context_has_required_binding_fields(context)
    ):
        return None
    return {
        "proof_binding_id": proof_binding_id,
        "challenge_id": challenge_id,
        "proof_status": cast(ProofStatus, proof_status),
        "issued_at": issued_at,
        "bound_at": bound_at,
        "expires_at": expires_at,
        "consumed_at": consumed_at,
        "context_hash": context_hash,
        "context": context,
    }


def _proof_rejection(
    *,
    reason_code: str,
    reason: str,
    context: StepUpProofBindingContext,
    required_controls: list[str],
    next_allowed_actions: list[str],
) -> StepUpProofRejectionEnvelope:
    return {
        "error_code": "action_rejected_step_up_proof",
        "message": "Action request failed deterministic step-up proof authorization checks.",
        "reason_code": reason_code,
        "reason": reason,
        "rejected_context": context,
        "required_controls": required_controls,
        "next_allowed_actions": next_allowed_actions,
        "correlation_id": context["correlation_id"],
        "trace_id": context["trace_id"],
    }


def _context_has_required_binding_fields(context: StepUpProofBindingContext) -> bool:
    required_values = (
        context["principal_user_id"],
        context["tenant_id"],
        context["action_type"],
        context["action_reference_id"],
        context["step_up_purpose"],
    )
    return all(isinstance(value, str) and bool(value.strip()) for value in required_values)


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
