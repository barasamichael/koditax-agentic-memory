"""Deterministic conflict detection for projected document evidence vs user inputs."""

from __future__ import annotations

from decimal import Decimal
from decimal import InvalidOperation
from collections.abc import Mapping
from collections.abc import Callable
from typing import cast


class EvidenceConflictDetectionError(ValueError):
    """Represent deterministic evidence conflict-detection failure."""

    def __init__(self, *, error_code: str, message: str, reason: str) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.message = message
        self.reason = reason


def detect_evidence_input_conflicts(
    *,
    evidence_projection: Mapping[str, object],
    user_execution_request: Mapping[str, object],
) -> dict[str, object]:
    """Compare evidence projection against user input and return canonical conflict report."""

    projection = _coerce_projection(evidence_projection)
    execution_request = _coerce_execution_request(user_execution_request)

    mapped_evidence_fields = _as_dict(
        projection.get("mapped_evidence_fields"),
        reason="invalid_projection_payload:mapped_evidence_fields",
    )
    traceability = _as_dict(
        projection.get("traceability"),
        reason="invalid_projection_payload:traceability",
    )
    input_payload = _as_dict(
        execution_request.get("input_payload"),
        reason="invalid_user_input_payload:input_payload",
    )

    taxpayer_context = _as_dict(
        input_payload.get("taxpayer_context"),
        reason="invalid_user_input_payload:taxpayer_context",
    )
    income_sections = _as_dict(
        input_payload.get("income_sections"),
        reason="invalid_user_input_payload:income_sections",
    )

    conflicts: list[dict[str, object]] = []
    compared_fields: list[str] = []
    supported_lane_id = projection.get("supported_lane_id")
    if not isinstance(supported_lane_id, str) or supported_lane_id == "":
        raise _conflict_error(reason="invalid_projection_payload:supported_lane_id")

    _compare_scalar(
        conflicts=conflicts,
        field_path="input_payload.taxpayer_context.taxpayer_reference_id",
        evidence_value=mapped_evidence_fields.get("taxpayer_pin"),
        user_value=taxpayer_context.get("taxpayer_reference_id"),
        compared_fields=compared_fields,
        normalizer=_normalize_string_token,
    )
    _compare_scalar(
        conflicts=conflicts,
        field_path="input_payload.taxpayer_context.resident_status_assertion",
        evidence_value=mapped_evidence_fields.get("resident_status_assertion"),
        user_value=taxpayer_context.get("resident_status_assertion"),
        compared_fields=compared_fields,
        normalizer=_normalize_string_token,
    )
    _compare_scalar(
        conflicts=conflicts,
        field_path="tax_year",
        evidence_value=mapped_evidence_fields.get("document_tax_year"),
        user_value=execution_request.get("tax_year"),
        compared_fields=compared_fields,
        normalizer=_normalize_int,
    )

    employment_evidence = mapped_evidence_fields.get("employment")
    if isinstance(employment_evidence, Mapping):
        employment_dict = dict(cast(Mapping[str, object], employment_evidence))
        _compare_scalar(
            conflicts=conflicts,
            field_path="input_payload.income_sections.employment.employment_items[].amount_kes",
            evidence_value=employment_dict.get("gross_employment_income_kes"),
            user_value=_sum_employment_money_value(
                income_sections=income_sections,
                money_key="amount_kes",
            ),
            compared_fields=compared_fields,
            normalizer=_normalize_decimal_money,
        )
        _compare_scalar(
            conflicts=conflicts,
            field_path="input_payload.income_sections.employment.employment_items[].paye_withheld_kes",
            evidence_value=employment_dict.get("paye_withheld_kes"),
            user_value=_sum_employment_money_value(
                income_sections=income_sections,
                money_key="paye_withheld_kes",
            ),
            compared_fields=compared_fields,
            normalizer=_normalize_decimal_money,
        )
        _compare_employer_reference(
            conflicts=conflicts,
            evidence_value=employment_dict.get("employer_tax_pin"),
            income_sections=income_sections,
            compared_fields=compared_fields,
        )

    qualifying_interest_evidence = mapped_evidence_fields.get("qualifying_interest")
    if isinstance(qualifying_interest_evidence, Mapping):
        qualifying_interest_dict = dict(cast(Mapping[str, object], qualifying_interest_evidence))
        _compare_scalar(
            conflicts=conflicts,
            field_path="input_payload.income_sections.investment.investment_items[].gross_amount_kes",
            evidence_value=qualifying_interest_dict.get("gross_interest_income_kes"),
            user_value=_sum_investment_interest_money(
                income_sections=income_sections,
                money_key="gross_amount_kes",
            ),
            compared_fields=compared_fields,
            normalizer=_normalize_decimal_money,
        )
        _compare_scalar(
            conflicts=conflicts,
            field_path="input_payload.income_sections.investment.investment_items[].withholding_applied_kes",
            evidence_value=qualifying_interest_dict.get("withholding_applied_kes"),
            user_value=_sum_investment_interest_money(
                income_sections=income_sections,
                money_key="withholding_applied_kes",
            ),
            compared_fields=compared_fields,
            normalizer=_normalize_decimal_money,
        )

    sorted_conflicts = sorted(
        conflicts,
        key=lambda item: (
            str(item["field_path"]),
            str(item["reason_code"]),
            str(item.get("evidence_value")),
            str(item.get("user_value")),
        ),
    )
    return {
        "conflict_detected": len(sorted_conflicts) > 0,
        "conflicts": sorted_conflicts,
        "comparison_scope": {
            "supported_lane_id": supported_lane_id,
            "fields_compared": sorted(set(compared_fields)),
        },
        "traceability": {
            "trace_id": traceability.get("trace_id"),
            "correlation_id": traceability.get("correlation_id"),
            "document_id": projection.get("document_id"),
            "representation_id": projection.get("representation_id"),
        },
    }


def _coerce_projection(payload: Mapping[str, object]) -> dict[str, object]:
    data = dict(payload)
    required = {
        "document_id",
        "representation_id",
        "supported_lane_id",
        "mapped_evidence_fields",
        "traceability",
    }
    if not required.issubset(data.keys()):
        raise _conflict_error(reason="invalid_projection_payload:missing_required_fields")
    return data


def _coerce_execution_request(payload: Mapping[str, object]) -> dict[str, object]:
    data = dict(payload)
    if data.get("tax_type") != "income_tax" or data.get("regime_type") != "income_tax":
        raise _conflict_error(reason="invalid_user_input_payload:unsupported_tax_type")
    if "input_payload" not in data:
        raise _conflict_error(reason="invalid_user_input_payload:missing_input_payload")
    return data


def _compare_scalar(
    *,
    conflicts: list[dict[str, object]],
    field_path: str,
    evidence_value: object,
    user_value: object,
    compared_fields: list[str],
    normalizer: Callable[[object], object],
) -> None:
    compared_fields.append(field_path)

    if evidence_value is None and user_value is None:
        return
    if evidence_value is None and user_value is not None:
        _append_conflict(
            conflicts=conflicts,
            field_path=field_path,
            evidence_value=evidence_value,
            user_value=user_value,
            reason_code="missing_evidence_value",
        )
        return
    if evidence_value is not None and user_value is None:
        _append_conflict(
            conflicts=conflicts,
            field_path=field_path,
            evidence_value=evidence_value,
            user_value=user_value,
            reason_code="missing_user_value",
        )
        return

    try:
        normalized_evidence = normalizer(evidence_value)
        normalized_user = normalizer(user_value)
    except ValueError:
        _append_conflict(
            conflicts=conflicts,
            field_path=field_path,
            evidence_value=evidence_value,
            user_value=user_value,
            reason_code="type_mismatch",
        )
        return

    if normalized_evidence != normalized_user:
        _append_conflict(
            conflicts=conflicts,
            field_path=field_path,
            evidence_value=evidence_value,
            user_value=user_value,
            reason_code="value_mismatch",
        )


def _compare_employer_reference(
    *,
    conflicts: list[dict[str, object]],
    evidence_value: object,
    income_sections: dict[str, object],
    compared_fields: list[str],
) -> None:
    field_path = "input_payload.income_sections.employment.employment_items[].employer_reference_id"
    compared_fields.append(field_path)
    user_employer_values = _employment_employer_reference_values(income_sections)
    if len(user_employer_values) > 1:
        _append_conflict(
            conflicts=conflicts,
            field_path=field_path,
            evidence_value=evidence_value,
            user_value=user_employer_values,
            reason_code="ambiguous_user_value",
        )
        return
    user_value = user_employer_values[0] if user_employer_values else None
    _compare_scalar(
        conflicts=conflicts,
        field_path=field_path,
        evidence_value=evidence_value,
        user_value=user_value,
        compared_fields=[],
        normalizer=_normalize_string_token,
    )


def _sum_employment_money_value(
    *, income_sections: dict[str, object], money_key: str
) -> str | None:
    employment = income_sections.get("employment")
    if not isinstance(employment, Mapping):
        return None
    employment_map = cast(Mapping[str, object], employment)
    items = employment_map.get("employment_items")
    if not isinstance(items, list):
        return None
    running_total = Decimal("0.00")
    has_value = False
    for item in cast(list[object], items):
        if not isinstance(item, Mapping):
            continue
        raw_value = cast(Mapping[str, object], item).get(money_key)
        if raw_value is None:
            continue
        running_total += _parse_money(raw_value)
        has_value = True
    if not has_value:
        return None
    return f"{running_total:.2f}"


def _sum_investment_interest_money(
    *, income_sections: dict[str, object], money_key: str
) -> str | None:
    investment = income_sections.get("investment")
    if not isinstance(investment, Mapping):
        return None
    investment_map = cast(Mapping[str, object], investment)
    items = investment_map.get("investment_items")
    if not isinstance(items, list):
        return None
    running_total = Decimal("0.00")
    has_value = False
    for item in cast(list[object], items):
        if not isinstance(item, Mapping):
            continue
        subtype = cast(Mapping[str, object], item).get("income_subtype")
        if subtype != "interest":
            continue
        raw_value = cast(Mapping[str, object], item).get(money_key)
        if raw_value is None:
            continue
        running_total += _parse_money(raw_value)
        has_value = True
    if not has_value:
        return None
    return f"{running_total:.2f}"


def _employment_employer_reference_values(income_sections: dict[str, object]) -> list[str]:
    employment = income_sections.get("employment")
    if not isinstance(employment, Mapping):
        return []
    employment_map = cast(Mapping[str, object], employment)
    items = employment_map.get("employment_items")
    if not isinstance(items, list):
        return []
    values: set[str] = set()
    for item in cast(list[object], items):
        if not isinstance(item, Mapping):
            continue
        raw_value = cast(Mapping[str, object], item).get("employer_reference_id")
        if isinstance(raw_value, str) and raw_value != "":
            values.add(raw_value)
    return sorted(values)


def _parse_money(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("money_value_must_be_string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid_money_value") from error
    return parsed.quantize(Decimal("0.01"))


def _normalize_decimal_money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid_money_value")
    if isinstance(value, str):
        return _parse_money(value)
    if isinstance(value, int | float):
        return Decimal(str(float(value))).quantize(Decimal("0.01"))
    raise ValueError("invalid_money_value")


def _normalize_string_token(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_string_value")
    normalized = value.strip().lower()
    if normalized == "":
        raise ValueError("empty_string_value")
    return normalized


def _normalize_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_int_value")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError("invalid_int_value")


def _append_conflict(
    *,
    conflicts: list[dict[str, object]],
    field_path: str,
    evidence_value: object,
    user_value: object,
    reason_code: str,
) -> None:
    conflicts.append(
        {
            "field_path": field_path,
            "evidence_value": evidence_value,
            "user_value": user_value,
            "reason_code": reason_code,
        }
    )


def _as_dict(value: object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _conflict_error(reason=reason)
    return dict(cast(Mapping[str, object], value))


def _conflict_error(reason: str) -> EvidenceConflictDetectionError:
    return EvidenceConflictDetectionError(
        error_code="evidence_conflict_detection_rejected",
        message="Evidence conflict detection request is invalid.",
        reason=reason,
    )
