"""Centralized deterministic account-state transition and action guardrails."""

from __future__ import annotations

from typing import Final
from typing import Literal
from dataclasses import dataclass

AccountState = Literal["pending_verification", "active", "locked", "disabled"]
AccountAction = Literal["verify_email", "verify_phone", "auth_access"]

_ALLOWED_TRANSITIONS: Final[dict[AccountState, frozenset[AccountState]]] = {
    "pending_verification": frozenset({"active", "disabled"}),
    "active": frozenset({"locked", "disabled"}),
    "locked": frozenset({"active", "disabled"}),
    "disabled": frozenset(),
}

_ACTION_ALLOWED_STATES: Final[dict[AccountAction, frozenset[AccountState]]] = {
    "verify_email": frozenset({"pending_verification"}),
    "verify_phone": frozenset({"pending_verification"}),
    "auth_access": frozenset({"active"}),
}


@dataclass(frozen=True)
class AccountStateError(ValueError):
    """
    Represent deterministic account-state transition/action validation failures.
    """

    error_code: str
    message: str
    reason: str
    current_state: AccountState
    requested_state: AccountState

    def __str__(self) -> str:  # pragma: no cover - trivial wrapper
        return self.message


def ensure_state_transition_allowed(
    *,
    current_state: AccountState,
    requested_state: AccountState,
) -> None:
    """
    Validate transition against canonical matrix or raise deterministic error.
    """

    allowed_destinations = _ALLOWED_TRANSITIONS[current_state]
    if requested_state not in allowed_destinations:
        raise AccountStateError(
            error_code="account_state_transition_not_allowed",
            message="Requested account-state transition is not allowed.",
            reason="account_state_transition_not_allowed",
            current_state=current_state,
            requested_state=requested_state,
        )


def require_account_action_allowed(
    *,
    action: AccountAction,
    current_state: AccountState,
) -> None:
    """Validate one lifecycle action against canonical allowed-state policy."""

    allowed_states = _ACTION_ALLOWED_STATES[action]
    if current_state in allowed_states:
        return

    if action == "auth_access" and current_state == "pending_verification":
        raise AccountStateError(
            error_code="account_verification_required",
            message="Account verification is required before this action.",
            reason="account_verification_required",
            current_state=current_state,
            requested_state="active",
        )

    raise AccountStateError(
        error_code="account_state_action_forbidden",
        message="Account action is forbidden for current account state.",
        reason="account_state_action_forbidden",
        current_state=current_state,
        requested_state=current_state,
    )
