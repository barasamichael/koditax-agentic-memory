"""Handle deterministic confirmation-state transitions for income-tax draft outcomes."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event

ConfirmationState = Literal["draft_ready", "awaiting_confirmation", "confirmed", "rejected"]
TransitionStatus = Literal["applied", "rejected"]
ALLOWED_CONFIRMATION_TRANSITIONS: dict[ConfirmationState, set[ConfirmationState]] = {
    "draft_ready": {"awaiting_confirmation"},
    "awaiting_confirmation": {"confirmed", "rejected"},
    "confirmed": set(),
    "rejected": set(),
}


class ConfirmationDraftContext(TypedDict):
    """Represent deterministic confirmation context for one draft outcome."""

    supported_lane_id: str
    historical_version_id: str
    tax_year: int


class ConfirmationLineageRefs(TypedDict):
    """Represent deterministic lineage anchors for confirmation lifecycle handling."""

    prompt_id: str
    computation_id: str
    input_hash: str
    rule_version: str


class ConfirmationTransitionEntry(TypedDict):
    """Represent one deterministic transition entry in confirmation state history."""

    transition_id: str
    transition_index: int
    previous_state: str | None
    next_state: str
    transition_status: TransitionStatus
    reason: str


class ConfirmationStateRecord(TypedDict):
    """Represent one deterministic confirmation-state record for one draft outcome."""

    confirmation_record_id: str
    current_state: ConfirmationState
    draft_context: ConfirmationDraftContext
    lineage: ConfirmationLineageRefs
    state_history: list[ConfirmationTransitionEntry]


class ConfirmationTransitionRejectedContext(TypedDict):
    """Represent deterministic rejected context for transition failures."""

    previous_state: str | None
    requested_next_state: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    prompt_id: str | None


class ConfirmationTransitionError(TypedDict):
    """Represent deterministic confirmation-transition rejection payload."""

    error_code: str
    message: str
    reason: str
    rejected_context: ConfirmationTransitionRejectedContext


class ConfirmationTransitionResult(TypedDict):
    """Represent deterministic confirmation-transition outcome envelope."""

    previous_state: str | None
    next_state: str
    transition_status: TransitionStatus
    reason: str
    correlation_id: str | None
    lineage: ConfirmationLineageRefs
    transition: ConfirmationTransitionEntry
    state_record: ConfirmationStateRecord
    error: ConfirmationTransitionError | None


class IncomeTaxConfirmationStateError(RuntimeError):
    """Represent deterministic confirmation-state lifecycle failures."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic payload for confirmation-state failures."""

        return {
            "error_code": "invalid_confirmation_record",
            "message": self.message,
            "reason": self.reason,
            "details": self.details,
        }


def initialize_income_tax_confirmation_state(
    *,
    draft_outcome: Mapping[str, object],
) -> ConfirmationStateRecord:
    """Initialize deterministic confirmation-state record from one draft-ready outcome."""

    status = _require_string(draft_outcome, "status")
    if status != "draft_ready":
        raise IncomeTaxConfirmationStateError(
            reason="invalid_initial_state",
            message="Confirmation state initialization requires draft_ready outcome status.",
            details={"status": status},
        )

    prompt_id = _require_string(draft_outcome, "prompt_id")
    draft_context = _require_object(draft_outcome, "draft_context")
    lineage = _require_object(draft_outcome, "lineage")

    confirmation_context: ConfirmationDraftContext = {
        "supported_lane_id": _require_string(draft_context, "supported_lane_id"),
        "historical_version_id": _require_string(draft_context, "historical_version_id"),
        "tax_year": _require_int(draft_context, "tax_year"),
    }
    lineage_refs: ConfirmationLineageRefs = {
        "prompt_id": prompt_id,
        "computation_id": _require_string(lineage, "computation_id"),
        "input_hash": _require_string(lineage, "input_hash"),
        "rule_version": _require_string(lineage, "rule_version"),
    }

    identity_payload = {
        "scope": "income_tax_confirmation_state",
        "draft_context": confirmation_context,
        "lineage": lineage_refs,
    }
    confirmation_record_id = _sha256_hex(canonical_json_dumps(identity_payload))
    initial_transition = _build_transition_entry(
        confirmation_record_id=confirmation_record_id,
        transition_index=1,
        previous_state=None,
        next_state="draft_ready",
        transition_status="applied",
        reason="draft_state_initialized",
    )
    return {
        "confirmation_record_id": confirmation_record_id,
        "current_state": "draft_ready",
        "draft_context": confirmation_context,
        "lineage": lineage_refs,
        "state_history": [initial_transition],
    }


def transition_income_tax_confirmation_state(
    *,
    confirmation_record: Mapping[str, object],
    target_state: str,
) -> ConfirmationTransitionResult:
    """Transition confirmation state with deterministic apply/reject envelope output."""

    parsed_record = _as_confirmation_state_record(confirmation_record)
    current_state = parsed_record["current_state"]
    correlation_id = parsed_record["lineage"]["prompt_id"]
    transition_index = len(parsed_record["state_history"]) + 1

    normalized_target = target_state.strip()
    if normalized_target not in {"draft_ready", "awaiting_confirmation", "confirmed", "rejected"}:
        return _rejected_transition(
            parsed_record=parsed_record,
            previous_state=current_state,
            requested_next_state=normalized_target,
            transition_index=transition_index,
            reason="invalid_target_state",
            message="Requested confirmation target state is not supported.",
            correlation_id=correlation_id,
        )

    target = cast(ConfirmationState, normalized_target)
    if target not in ALLOWED_CONFIRMATION_TRANSITIONS[current_state]:
        return _rejected_transition(
            parsed_record=parsed_record,
            previous_state=current_state,
            requested_next_state=target,
            transition_index=transition_index,
            reason="invalid_state_transition",
            message="Confirmation state transition is not allowed for current state.",
            correlation_id=correlation_id,
        )

    transition = _build_transition_entry(
        confirmation_record_id=parsed_record["confirmation_record_id"],
        transition_index=transition_index,
        previous_state=current_state,
        next_state=target,
        transition_status="applied",
        reason="transition_applied",
    )
    updated_record: ConfirmationStateRecord = {
        **parsed_record,
        "current_state": target,
        "state_history": [*parsed_record["state_history"], transition],
    }
    emit_income_tax_audit_event(
        event_type="confirmation_transition",
        status="applied",
        correlation_id=correlation_id,
        supported_lane_id=parsed_record["draft_context"]["supported_lane_id"],
        historical_version_id=parsed_record["draft_context"]["historical_version_id"],
        tax_year=parsed_record["draft_context"]["tax_year"],
        context={
            "previous_state": current_state,
            "next_state": target,
            "transition_id": transition["transition_id"],
        },
    )
    return {
        "previous_state": current_state,
        "next_state": target,
        "transition_status": "applied",
        "reason": "transition_applied",
        "correlation_id": correlation_id,
        "lineage": parsed_record["lineage"],
        "transition": transition,
        "state_record": updated_record,
        "error": None,
    }


def _rejected_transition(
    *,
    parsed_record: ConfirmationStateRecord,
    previous_state: str | None,
    requested_next_state: str,
    transition_index: int,
    reason: str,
    message: str,
    correlation_id: str | None,
) -> ConfirmationTransitionResult:
    transition = _build_transition_entry(
        confirmation_record_id=parsed_record["confirmation_record_id"],
        transition_index=transition_index,
        previous_state=previous_state,
        next_state=requested_next_state,
        transition_status="rejected",
        reason=reason,
    )
    rejected_context: ConfirmationTransitionRejectedContext = {
        "previous_state": previous_state,
        "requested_next_state": requested_next_state,
        "supported_lane_id": parsed_record["draft_context"]["supported_lane_id"],
        "historical_version_id": parsed_record["draft_context"]["historical_version_id"],
        "tax_year": parsed_record["draft_context"]["tax_year"],
        "prompt_id": parsed_record["lineage"]["prompt_id"],
    }
    emit_income_tax_audit_event(
        event_type="confirmation_transition",
        status="rejected",
        correlation_id=correlation_id,
        supported_lane_id=parsed_record["draft_context"]["supported_lane_id"],
        historical_version_id=parsed_record["draft_context"]["historical_version_id"],
        tax_year=parsed_record["draft_context"]["tax_year"],
        context={
            "previous_state": previous_state,
            "requested_next_state": requested_next_state,
            "reason": reason,
            "transition_id": transition["transition_id"],
        },
    )
    return {
        "previous_state": previous_state,
        "next_state": requested_next_state,
        "transition_status": "rejected",
        "reason": reason,
        "correlation_id": correlation_id,
        "lineage": parsed_record["lineage"],
        "transition": transition,
        "state_record": parsed_record,
        "error": {
            "error_code": "invalid_confirmation_transition",
            "message": message,
            "reason": reason,
            "rejected_context": rejected_context,
        },
    }


def _as_confirmation_state_record(
    value: Mapping[str, object],
) -> ConfirmationStateRecord:
    confirmation_record_id = _require_string(value, "confirmation_record_id")
    current_state_str = _require_string(value, "current_state")
    if current_state_str not in {"draft_ready", "awaiting_confirmation", "confirmed", "rejected"}:
        raise IncomeTaxConfirmationStateError(
            reason="invalid_current_state",
            message="Confirmation record current_state is not supported.",
            details={"current_state": current_state_str},
        )
    current_state = cast(ConfirmationState, current_state_str)

    draft_context_raw = _require_object(value, "draft_context")
    draft_context: ConfirmationDraftContext = {
        "supported_lane_id": _require_string(draft_context_raw, "supported_lane_id"),
        "historical_version_id": _require_string(draft_context_raw, "historical_version_id"),
        "tax_year": _require_int(draft_context_raw, "tax_year"),
    }
    lineage_raw = _require_object(value, "lineage")
    lineage: ConfirmationLineageRefs = {
        "prompt_id": _require_string(lineage_raw, "prompt_id"),
        "computation_id": _require_string(lineage_raw, "computation_id"),
        "input_hash": _require_string(lineage_raw, "input_hash"),
        "rule_version": _require_string(lineage_raw, "rule_version"),
    }
    history = _list_of_transition_entries(value, "state_history")
    if not history:
        raise IncomeTaxConfirmationStateError(
            reason="missing_state_history",
            message="Confirmation record must contain at least one state transition entry.",
            details={"field_name": "state_history"},
        )
    return {
        "confirmation_record_id": confirmation_record_id,
        "current_state": current_state,
        "draft_context": draft_context,
        "lineage": lineage,
        "state_history": history,
    }


def _build_transition_entry(
    *,
    confirmation_record_id: str,
    transition_index: int,
    previous_state: str | None,
    next_state: str,
    transition_status: TransitionStatus,
    reason: str,
) -> ConfirmationTransitionEntry:
    identity = {
        "confirmation_record_id": confirmation_record_id,
        "transition_index": transition_index,
        "previous_state": previous_state,
        "next_state": next_state,
        "transition_status": transition_status,
        "reason": reason,
    }
    return {
        "transition_id": _sha256_hex(canonical_json_dumps(identity)),
        "transition_index": transition_index,
        "previous_state": previous_state,
        "next_state": next_state,
        "transition_status": transition_status,
        "reason": reason,
    }


def _require_object(source: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxConfirmationStateError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return cast(Mapping[str, object], value)


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxConfirmationStateError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxConfirmationStateError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_transition_entries(
    source: Mapping[str, object],
    field_name: str,
) -> list[ConfirmationTransitionEntry]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxConfirmationStateError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    transitions: list[ConfirmationTransitionEntry] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, Mapping):
            raise IncomeTaxConfirmationStateError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only transition objects.",
                details={"field_name": field_name},
            )
        typed_item = cast(Mapping[str, object], item)
        transitions.append(
            {
                "transition_id": _require_string(typed_item, "transition_id"),
                "transition_index": _require_int(typed_item, "transition_index"),
                "previous_state": cast(str | None, typed_item.get("previous_state")),
                "next_state": _require_string(typed_item, "next_state"),
                "transition_status": cast(
                    TransitionStatus, _require_string(typed_item, "transition_status")
                ),
                "reason": _require_string(typed_item, "reason"),
            }
        )
    return transitions


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
