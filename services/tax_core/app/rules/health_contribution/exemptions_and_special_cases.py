"""Governed health-contribution exemptions and special-case handling."""

from __future__ import annotations

from typing import NoReturn
from decimal import Decimal
from dataclasses import dataclass
from collections.abc import Sequence

from shared.determinism.input_hash import InputHashError

SPECIAL_MEMBER_CONTRIBUTION = Decimal("500.00")
UNRESOLVED_SPECIAL_CASE_POLICY_ID = "HCP-POL-U03"


@dataclass(frozen=True)
class NhifSpecialCaseResolution:
    """Represent the single governed NHIF special-case path supported so far."""

    contribution_basis: Decimal
    contribution_amount: Decimal
    decision_refs: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]


def reject_unresolved_special_case_assertions(
    assertion_items: Sequence[object],
    *,
    path: str,
) -> None:
    """Fail closed when unresolved exemption or special-case assertions are present."""

    if not assertion_items:
        return

    _raise_rule_input_error(
        reason="unsupported_special_case_assertions",
        message=(
            "Exemption and special-case assertions remain unresolved and fail-closed "
            "for the current governed health-contribution windows."
        ),
        path=path,
    )


def resolve_nhif_special_member(
    *,
    member_class: str,
    contributor_kind: str,
    income_basis_type: str,
    amount: Decimal,
    schedule_rule_id: str,
    window_policy_id: str,
    contributor_kind_path: str,
    income_basis_type_path: str,
    amount_path: str,
) -> NhifSpecialCaseResolution | None:
    """Resolve the only source-proven health special-case path for NHIF legacy."""

    if member_class != "special_member":
        return None

    if contributor_kind != "self_employed":
        _raise_rule_input_error(
            reason="unsupported_contributor_kind",
            message="special_member requests require contributor_kind=self_employed.",
            path=contributor_kind_path,
        )
    if income_basis_type != "special_contributor_basis":
        _raise_rule_input_error(
            reason="unsupported_nhif_income_basis_type",
            message="special_member requests require income_basis_type=special_contributor_basis.",
            path=income_basis_type_path,
        )
    if amount != SPECIAL_MEMBER_CONTRIBUTION:
        _raise_rule_input_error(
            reason="unsupported_special_member_basis_amount",
            message="special_member requests must assert the fixed KSh 500.00 contribution basis.",
            path=amount_path,
        )

    return NhifSpecialCaseResolution(
        contribution_basis=SPECIAL_MEMBER_CONTRIBUTION,
        contribution_amount=SPECIAL_MEMBER_CONTRIBUTION,
        decision_refs=("HC-NHIF-NPOL-0002", schedule_rule_id),
        applied_policy_ids=("HCP-POL-003", "HCP-POL-110", window_policy_id),
    )


def _raise_rule_input_error(
    *,
    reason: str,
    message: str,
    path: str,
) -> NoReturn:
    raise InputHashError(reason=reason, message=message, path=path)
