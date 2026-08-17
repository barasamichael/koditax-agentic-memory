"""Canonical delegation-context shape and validation helpers."""

from __future__ import annotations

from uuid import UUID
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass


class DelegationPolicyError(ValueError):
    """Represent deterministic delegation policy validation failure."""

    def __init__(self, *, reason: str, message: str, details: dict[str, object]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details


@dataclass(frozen=True)
class DelegationContext:
    """Represent canonical delegation context carried in auth context envelopes."""

    is_delegated: bool
    principal_user_id: UUID | None
    delegate_user_id: UUID | None
    delegation_id: UUID | None
    granted_at: datetime | None
    revoked_at: datetime | None

    @staticmethod
    def not_delegated() -> DelegationContext:
        """Return deterministic non-delegated context baseline."""

        return DelegationContext(
            is_delegated=False,
            principal_user_id=None,
            delegate_user_id=None,
            delegation_id=None,
            granted_at=None,
            revoked_at=None,
        )


def validate_delegation_context(
    *,
    delegation_context: DelegationContext,
    user_id: UUID,
    tenant_id: str,
    required_tenant_id: str,
    allow_delegation: bool,
    now: datetime | None = None,
) -> None:
    """Validate delegation context deterministically for policy enforcement."""

    if not delegation_context.is_delegated:
        return

    if not allow_delegation:
        raise DelegationPolicyError(
            reason="authorization_delegation_forbidden",
            message="Delegated access is forbidden.",
            details={"claim": "delegation_context"},
        )

    if tenant_id != required_tenant_id:
        raise DelegationPolicyError(
            reason="delegation_tenant_mismatch",
            message="Delegation tenant is invalid.",
            details={"tenant_id": tenant_id},
        )

    if (
        delegation_context.principal_user_id is None
        or delegation_context.delegate_user_id is None
        or delegation_context.delegation_id is None
        or delegation_context.granted_at is None
    ):
        raise DelegationPolicyError(
            reason="delegation_context_missing",
            message="Delegation context is incomplete.",
            details={"claim": "delegation_context"},
        )

    if delegation_context.delegate_user_id != user_id:
        raise DelegationPolicyError(
            reason="delegation_context_invalid",
            message="Delegation context does not match principal identity.",
            details={"claim": "delegate_user_id"},
        )

    if delegation_context.principal_user_id == delegation_context.delegate_user_id:
        raise DelegationPolicyError(
            reason="delegation_context_invalid",
            message="Delegation context is invalid.",
            details={"claim": "principal_user_id"},
        )

    if delegation_context.revoked_at is not None:
        raise DelegationPolicyError(
            reason="delegation_revoked",
            message="Delegation is revoked.",
            details={"delegation_id": str(delegation_context.delegation_id)},
        )

    current_time = now or datetime.now(UTC)
    if delegation_context.granted_at > current_time:
        raise DelegationPolicyError(
            reason="delegation_not_active",
            message="Delegation is not active.",
            details={"delegation_id": str(delegation_context.delegation_id)},
        )
