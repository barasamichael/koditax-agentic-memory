"""Deterministic precedence and confirmation policy for evidence conflicts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

_DECISION_BLOCK = "block"
_DECISION_AWAIT_CONFIRMATION = "await_confirmation"
_DECISION_PROCEED = "proceed_no_conflict_only"

_HIGH_SEVERITY_REASON_CODES: frozenset[str] = frozenset(
    {
        "type_mismatch",
        "ambiguous_user_value",
        "missing_user_value",
        "missing_evidence_value",
    }
)
_CONFIRMATION_REASON_CODES: frozenset[str] = frozenset({"value_mismatch"})


class EvidenceConflictPolicyError(ValueError):
    """Represent deterministic evidence conflict-policy rejection."""

    def __init__(self, *, error_code: str, message: str, reason: str) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.message = message
        self.reason = reason


def evaluate_evidence_conflict_policy(
    *, conflict_report: Mapping[str, object]
) -> dict[str, object]:
    """Evaluate deterministic precedence/confirmation policy from conflict report."""

    parsed_conflict_report = _coerce_conflict_report(conflict_report)
    traceability = _as_dict(
        parsed_conflict_report.get("traceability"),
        reason="invalid_conflict_report:traceability",
    )
    comparison_scope = _as_dict(
        parsed_conflict_report.get("comparison_scope"),
        reason="invalid_conflict_report:comparison_scope",
    )
    fields_compared = _as_string_list(
        comparison_scope.get("fields_compared"),
        reason="invalid_conflict_report:comparison_scope.fields_compared",
    )
    conflicts = _as_conflict_entries(parsed_conflict_report.get("conflicts"))

    if not conflicts:
        return {
            "decision": _DECISION_PROCEED,
            "decision_reason_codes": ["no_conflict_detected"],
            "requires_confirmation": False,
            "blocked_fields": [],
            "proceedable_fields": sorted(set(fields_compared)),
            "comparison_scope": {
                "supported_lane_id": comparison_scope.get("supported_lane_id"),
                "fields_compared": sorted(set(fields_compared)),
                "conflict_count": 0,
            },
            "traceability": _canonical_traceability(traceability),
        }

    reason_codes = sorted({entry["reason_code"] for entry in conflicts})
    blocked_fields = sorted(
        {
            entry["field_path"]
            for entry in conflicts
            if _is_policy_blocking_reason(entry["reason_code"])
        }
    )
    has_unknown_reason = any(
        entry["reason_code"] not in _HIGH_SEVERITY_REASON_CODES
        and entry["reason_code"] not in _CONFIRMATION_REASON_CODES
        for entry in conflicts
    )
    if has_unknown_reason:
        blocked_fields = sorted({entry["field_path"] for entry in conflicts})

    if blocked_fields:
        reason_prefix = ["conflict_detected", "policy_blocking_conflict"]
        if has_unknown_reason:
            reason_prefix.append("unknown_conflict_reason_code")
        return {
            "decision": _DECISION_BLOCK,
            "decision_reason_codes": reason_prefix + reason_codes,
            "requires_confirmation": False,
            "blocked_fields": blocked_fields,
            "proceedable_fields": sorted(set(fields_compared) - set(blocked_fields)),
            "comparison_scope": {
                "supported_lane_id": comparison_scope.get("supported_lane_id"),
                "fields_compared": sorted(set(fields_compared)),
                "conflict_count": len(conflicts),
            },
            "traceability": _canonical_traceability(traceability),
        }

    conflict_fields = sorted({entry["field_path"] for entry in conflicts})
    return {
        "decision": _DECISION_AWAIT_CONFIRMATION,
        "decision_reason_codes": [
            "conflict_detected",
            "requires_confirmation",
            *reason_codes,
        ],
        "requires_confirmation": True,
        "blocked_fields": [],
        "proceedable_fields": sorted(set(fields_compared) - set(conflict_fields)),
        "comparison_scope": {
            "supported_lane_id": comparison_scope.get("supported_lane_id"),
            "fields_compared": sorted(set(fields_compared)),
            "conflict_count": len(conflicts),
        },
        "traceability": _canonical_traceability(traceability),
    }


def _coerce_conflict_report(payload: Mapping[str, object]) -> dict[str, object]:
    data = dict(payload)
    required = {
        "conflict_detected",
        "conflicts",
        "comparison_scope",
        "traceability",
    }
    if not required.issubset(data.keys()):
        raise _policy_error(reason="invalid_conflict_report:missing_required_fields")
    raw_conflict_detected = data.get("conflict_detected")
    if not isinstance(raw_conflict_detected, bool):
        raise _policy_error(reason="invalid_conflict_report:conflict_detected")
    return data


def _as_conflict_entries(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _policy_error(reason="invalid_conflict_report:conflicts")
    parsed: list[dict[str, str]] = []
    for item in cast(list[object], value):
        if not isinstance(item, Mapping):
            raise _policy_error(reason="invalid_conflict_report:conflicts.item")
        item_map = cast(Mapping[str, object], item)
        field_path = item_map.get("field_path")
        reason_code = item_map.get("reason_code")
        if not isinstance(field_path, str) or field_path == "":
            raise _policy_error(reason="invalid_conflict_report:conflicts.field_path")
        if not isinstance(reason_code, str) or reason_code == "":
            raise _policy_error(reason="invalid_conflict_report:conflicts.reason_code")
        parsed.append(
            {
                "field_path": field_path,
                "reason_code": reason_code,
            }
        )
    return parsed


def _is_policy_blocking_reason(reason_code: str) -> bool:
    return reason_code in _HIGH_SEVERITY_REASON_CODES


def _canonical_traceability(traceability: Mapping[str, object]) -> dict[str, object]:
    return {
        "trace_id": traceability.get("trace_id"),
        "correlation_id": traceability.get("correlation_id"),
        "document_id": traceability.get("document_id"),
        "representation_id": traceability.get("representation_id"),
    }


def _as_string_list(value: object, *, reason: str) -> list[str]:
    if not isinstance(value, list):
        raise _policy_error(reason=reason)
    parsed: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or item == "":
            raise _policy_error(reason=reason)
        parsed.append(item)
    return parsed


def _as_dict(value: object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _policy_error(reason=reason)
    return dict(cast(Mapping[str, object], value))


def _policy_error(reason: str) -> EvidenceConflictPolicyError:
    return EvidenceConflictPolicyError(
        error_code="evidence_conflict_policy_rejected",
        message="Evidence conflict policy input is invalid.",
        reason=reason,
    )
