"""Deterministic document lifecycle transition enforcement."""

from __future__ import annotations

from typing import Literal
from datetime import UTC
from datetime import datetime

DocumentLifecycleState = Literal[
    "active",
    "trashed",
    "purge_pending",
    "uploaded",
    "processing",
    "validated",
    "eligible_for_purge",
    "purged",
]

_ALLOWED_STATE_TRANSITIONS: dict[DocumentLifecycleState, tuple[DocumentLifecycleState, ...]] = {
    "active": ("trashed", "purge_pending"),
    "trashed": ("active", "purge_pending"),
    "purge_pending": (),
    "uploaded": ("processing", "eligible_for_purge", "trashed", "purge_pending"),
    "processing": ("validated", "eligible_for_purge", "trashed", "purge_pending"),
    "validated": ("eligible_for_purge", "trashed", "purge_pending"),
    "eligible_for_purge": ("processing", "purged"),
    "purged": (),
}


class DocumentStateTransitionError(ValueError):
    """Represent deterministic state-transition rejection."""

    def __init__(self, reason: str, current_state: str, requested_state: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_state = current_state
        self.requested_state = requested_state


class DocumentLifecycleActionError(ValueError):
    """Represent deterministic lifecycle-action rejection."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        current_state: DocumentLifecycleState,
        action: str,
        requested_state: DocumentLifecycleState | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.current_state = current_state
        self.action = action
        self.requested_state = requested_state


def enforce_document_state_transition(
    current_state: DocumentLifecycleState,
    requested_state: DocumentLifecycleState,
) -> DocumentLifecycleState:
    """Return requested state if transition is legal, else raise deterministic error."""

    if current_state == requested_state:
        return current_state
    allowed_targets = _ALLOWED_STATE_TRANSITIONS[current_state]
    if requested_state not in allowed_targets:
        raise DocumentStateTransitionError(
            reason="illegal_state_transition",
            current_state=current_state,
            requested_state=requested_state,
        )
    return requested_state


def enforce_document_trash_action(
    *,
    current_state: DocumentLifecycleState,
    compliance_lock_until: str | None,
    compliance_override_granted: bool = False,
    now_utc: datetime | None = None,
) -> DocumentLifecycleState:
    """Return deterministic state for a trash action under current lifecycle model."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    if (
        _is_compliance_lock_active(
            compliance_lock_until=compliance_lock_until,
            now_utc=reference_now,
        )
        and not compliance_override_granted
    ):
        raise DocumentLifecycleActionError(
            reason="compliance_lock_active",
            message="Document lifecycle action is blocked by active compliance lock.",
            current_state=current_state,
            action="trash",
            requested_state="trashed",
        )
    if current_state == "trashed":
        return current_state
    if current_state == "eligible_for_purge":
        raise DocumentLifecycleActionError(
            reason="already_trashed",
            message="Document is already in trashed lifecycle state.",
            current_state=current_state,
            action="trash",
            requested_state="trashed",
        )
    if current_state == "purged":
        raise DocumentLifecycleActionError(
            reason="already_purged",
            message="Document lifecycle action is not allowed for purged state.",
            current_state=current_state,
            action="trash",
            requested_state="trashed",
        )
    try:
        return enforce_document_state_transition(
            current_state=current_state,
            requested_state="trashed",
        )
    except DocumentStateTransitionError as error:
        raise DocumentLifecycleActionError(
            reason="invalid_trash_state_transition",
            message="Document state does not support trash action.",
            current_state=current_state,
            action="trash",
            requested_state="trashed",
        ) from error


def enforce_document_restore_action(
    *,
    current_state: DocumentLifecycleState,
    compliance_lock_until: str | None,
    compliance_override_granted: bool = False,
    now_utc: datetime | None = None,
) -> DocumentLifecycleState:
    """Return deterministic state for restore action under current lifecycle model."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    if (
        _is_compliance_lock_active(
            compliance_lock_until=compliance_lock_until,
            now_utc=reference_now,
        )
        and not compliance_override_granted
    ):
        raise DocumentLifecycleActionError(
            reason="compliance_lock_active",
            message="Document lifecycle action is blocked by active compliance lock.",
            current_state=current_state,
            action="restore",
            requested_state=current_state,
        )
    if current_state == "purged":
        raise DocumentLifecycleActionError(
            reason="already_purged",
            message="Document lifecycle action is not allowed for purged state.",
            current_state=current_state,
            action="restore",
            requested_state=current_state,
        )
    if current_state == "active":
        return current_state
    if current_state == "trashed":
        return enforce_document_state_transition(current_state, "active")
    if current_state != "eligible_for_purge":
        raise DocumentLifecycleActionError(
            reason="invalid_restore_state_transition",
            message="Document state does not support restore action.",
            current_state=current_state,
            action="restore",
            requested_state="processing",
        )
    try:
        return enforce_document_state_transition(
            current_state=current_state,
            requested_state="processing",
        )
    except DocumentStateTransitionError as error:
        raise DocumentLifecycleActionError(
            reason="invalid_restore_state_transition",
            message="Document state does not support restore action.",
            current_state=current_state,
            action="restore",
            requested_state="processing",
        ) from error


def enforce_mark_eligible_for_purge_action(
    *,
    current_state: DocumentLifecycleState,
    compliance_lock_until: str | None,
    purge_eligible_at: str | None,
    uploaded_at: str,
    compliance_override_granted: bool = False,
    now_utc: datetime | None = None,
) -> tuple[DocumentLifecycleState, str]:
    """Validate deterministic eligible_for_purge transition and purge eligibility timestamp."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    requested_state: DocumentLifecycleState = "eligible_for_purge"
    if (
        _is_compliance_lock_active(
            compliance_lock_until=compliance_lock_until,
            now_utc=reference_now,
        )
        and not compliance_override_granted
    ):
        raise DocumentLifecycleActionError(
            reason="compliance_lock_active",
            message="Document lifecycle action is blocked by active compliance lock.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    if purge_eligible_at is None:
        raise DocumentLifecycleActionError(
            reason="missing_purge_eligible_at",
            message="Purge eligibility marking requires purge_eligible_at timestamp.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    parsed_uploaded_at = _parse_iso_datetime_or_raise(
        value=uploaded_at,
        current_state=current_state,
        action="mark_eligible_for_purge",
        requested_state=requested_state,
        invalid_reason="invalid_uploaded_at",
        invalid_message="Document uploaded_at timestamp is invalid for lifecycle evaluation.",
    )
    parsed_purge_eligible_at = _parse_iso_datetime_or_raise(
        value=purge_eligible_at,
        current_state=current_state,
        action="mark_eligible_for_purge",
        requested_state=requested_state,
        invalid_reason="invalid_purge_eligible_at",
        invalid_message="Purge eligibility timestamp is invalid.",
    )
    if parsed_purge_eligible_at > reference_now:
        raise DocumentLifecycleActionError(
            reason="purge_eligible_at_in_future",
            message="Purge eligibility timestamp must be in the past or present.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    if parsed_purge_eligible_at < parsed_uploaded_at:
        raise DocumentLifecycleActionError(
            reason="purge_eligible_at_before_uploaded_at",
            message="Purge eligibility timestamp cannot be earlier than uploaded_at.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    if current_state == "eligible_for_purge":
        raise DocumentLifecycleActionError(
            reason="already_eligible_for_purge",
            message="Document is already marked as eligible_for_purge.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    if current_state == "purged":
        raise DocumentLifecycleActionError(
            reason="already_purged",
            message="Document lifecycle action is not allowed for purged state.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        )
    try:
        transitioned = enforce_document_state_transition(
            current_state=current_state,
            requested_state=requested_state,
        )
    except DocumentStateTransitionError as error:
        raise DocumentLifecycleActionError(
            reason="invalid_mark_eligible_state_transition",
            message="Document state does not support purge eligibility marking.",
            current_state=current_state,
            action="mark_eligible_for_purge",
            requested_state=requested_state,
        ) from error
    return transitioned, _to_iso_utc(parsed_purge_eligible_at)


def enforce_execute_purge_action(
    *,
    current_state: DocumentLifecycleState,
    compliance_lock_until: str | None,
    purge_eligible_at: str | None,
    purged_at: str | None,
    compliance_override_granted: bool = False,
    now_utc: datetime | None = None,
) -> tuple[DocumentLifecycleState, str]:
    """Validate deterministic purged transition and purge execution timestamp."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    requested_state: DocumentLifecycleState = "purged"
    if (
        _is_compliance_lock_active(
            compliance_lock_until=compliance_lock_until,
            now_utc=reference_now,
        )
        and not compliance_override_granted
    ):
        raise DocumentLifecycleActionError(
            reason="compliance_lock_active",
            message="Document lifecycle action is blocked by active compliance lock.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        )
    if current_state == "purged":
        raise DocumentLifecycleActionError(
            reason="already_purged",
            message="Document lifecycle action is not allowed for purged state.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        )
    if current_state != "eligible_for_purge":
        raise DocumentLifecycleActionError(
            reason="invalid_execute_purge_state_transition",
            message="Document state does not support purge execution.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        )
    if purge_eligible_at is None:
        raise DocumentLifecycleActionError(
            reason="missing_purge_eligible_at",
            message="Purge execution requires purge_eligible_at timestamp.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        )
    parsed_purge_eligible_at = _parse_iso_datetime_or_raise(
        value=purge_eligible_at,
        current_state=current_state,
        action="execute_purge",
        requested_state=requested_state,
        invalid_reason="invalid_purge_eligible_at",
        invalid_message="Stored purge_eligible_at timestamp is invalid for purge execution.",
    )
    parsed_purged_at = reference_now
    if purged_at is not None:
        parsed_purged_at = _parse_iso_datetime_or_raise(
            value=purged_at,
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
            invalid_reason="invalid_purged_at",
            invalid_message="Purge execution timestamp is invalid.",
        )
    if parsed_purged_at < parsed_purge_eligible_at:
        raise DocumentLifecycleActionError(
            reason="purge_before_eligibility",
            message="Purge execution timestamp cannot be earlier than purge_eligible_at.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        )
    try:
        transitioned = enforce_document_state_transition(
            current_state=current_state,
            requested_state=requested_state,
        )
    except DocumentStateTransitionError as error:
        raise DocumentLifecycleActionError(
            reason="invalid_execute_purge_state_transition",
            message="Document state does not support purge execution.",
            current_state=current_state,
            action="execute_purge",
            requested_state=requested_state,
        ) from error
    return transitioned, _to_iso_utc(parsed_purged_at)


def _is_compliance_lock_active(
    *,
    compliance_lock_until: str | None,
    now_utc: datetime,
) -> bool:
    if compliance_lock_until is None:
        return False
    try:
        parsed = datetime.fromisoformat(compliance_lock_until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed > now_utc


def is_document_compliance_lock_active(
    *,
    compliance_lock_until: str | None,
    now_utc: datetime | None = None,
) -> bool:
    """Return whether compliance lock is currently active for the document."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    return _is_compliance_lock_active(
        compliance_lock_until=compliance_lock_until,
        now_utc=reference_now,
    )


def _parse_iso_datetime_or_raise(
    *,
    value: str,
    current_state: DocumentLifecycleState,
    action: str,
    requested_state: DocumentLifecycleState,
    invalid_reason: str,
    invalid_message: str,
) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DocumentLifecycleActionError(
            reason=invalid_reason,
            message=invalid_message,
            current_state=current_state,
            action=action,
            requested_state=requested_state,
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


def _to_iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_document_lifecycle_event_name(
    *,
    action: str,
    status: str,
) -> str:
    """Build canonical lifecycle structured-log event name."""

    return f"document_lifecycle_{action}_{status}"
