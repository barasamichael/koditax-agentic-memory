"""Centralized deterministic access-policy gate for document actions."""

from __future__ import annotations

from uuid import UUID
from typing import Literal
from typing import TypedDict

DocumentAccessAction = Literal[
    "list_documents",
    "get_document",
    "retrieve_exact_evidence",
    "retrieve_semantic_candidates",
    "download_document",
    "trash",
    "restore",
    "mark_eligible_for_purge",
    "execute_purge",
    "purge_dry_run",
    "compliance_override_request",
    "compliance_override_approve",
    "compliance_override_reject",
]

DocumentAccessPolicyReason = Literal[
    "allowed",
    "role_not_permitted_for_action",
    "cross_tenant_access_forbidden",
    "owner_user_mismatch",
]


class DocumentAccessPolicyDecision(TypedDict):
    """Represent one deterministic access-policy decision."""

    decision: Literal["allow", "deny"]
    reason: DocumentAccessPolicyReason


_ACTION_ALLOWED_ROLES: dict[DocumentAccessAction, frozenset[str]] = {
    "list_documents": frozenset({"IndividualTaxpayer"}),
    "get_document": frozenset({"IndividualTaxpayer"}),
    "retrieve_exact_evidence": frozenset({"IndividualTaxpayer"}),
    "retrieve_semantic_candidates": frozenset({"IndividualTaxpayer"}),
    "download_document": frozenset({"IndividualTaxpayer"}),
    "trash": frozenset({"IndividualTaxpayer"}),
    "restore": frozenset({"IndividualTaxpayer"}),
    "mark_eligible_for_purge": frozenset({"IndividualTaxpayer"}),
    "execute_purge": frozenset({"IndividualTaxpayer"}),
    "purge_dry_run": frozenset({"IndividualTaxpayer"}),
    "compliance_override_request": frozenset({"IndividualTaxpayer"}),
    "compliance_override_approve": frozenset({"ComplianceOfficer"}),
    "compliance_override_reject": frozenset({"ComplianceOfficer"}),
}

_OWNER_REQUIRED_ACTIONS: frozenset[DocumentAccessAction] = frozenset(
    {
        "get_document",
        "retrieve_exact_evidence",
        "retrieve_semantic_candidates",
        "download_document",
        "trash",
        "restore",
        "mark_eligible_for_purge",
        "execute_purge",
        "purge_dry_run",
        "compliance_override_request",
    }
)


def evaluate_document_access_policy(
    *,
    actor_user_id: UUID,
    actor_tenant_id: str,
    actor_role: str,
    document_owner_user_id: UUID | None,
    document_tenant_id: str | None,
    action: DocumentAccessAction,
) -> DocumentAccessPolicyDecision:
    """Return deterministic authorization decision for one document action."""

    allowed_roles = _ACTION_ALLOWED_ROLES[action]
    if actor_role not in allowed_roles:
        return {"decision": "deny", "reason": "role_not_permitted_for_action"}

    if document_tenant_id is not None and document_tenant_id != actor_tenant_id:
        return {"decision": "deny", "reason": "cross_tenant_access_forbidden"}

    if action in _OWNER_REQUIRED_ACTIONS:
        if document_owner_user_id is None:
            return {"decision": "deny", "reason": "owner_user_mismatch"}
        if actor_user_id != document_owner_user_id:
            return {"decision": "deny", "reason": "owner_user_mismatch"}

    return {"decision": "allow", "reason": "allowed"}
