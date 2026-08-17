"""Canonical conversation fact ledger for cross-turn taxpayer state comparison."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Literal
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from services.orchestration.app.response_integrity_signals import FactMismatch
from services.orchestration.app.value_normalization import NormalizedValue
from services.orchestration.app.value_normalization import canonical_text
from services.orchestration.app.value_normalization import convert_frequency_value
from services.orchestration.app.value_normalization import parse_amount_text
from services.orchestration.app.value_normalization import parse_date_text
from services.orchestration.app.value_normalization import parse_rate_text
from services.orchestration.app.value_normalization import parse_tax_year_text

FACT_SCHEMA_VERSION = "2026-07-26"
FACT_LEDGER_VERSION = "2026-07-26"

FactSourceStatus = Literal[
    "explicit",
    "corrected",
    "confirmed",
    "reused",
    "document_extracted",
    "computed",
    "inferred",
    "removed",
    "unknown",
]


class CanonicalConversationFact(BaseModel):
    """Represent one canonical conversational fact."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    fact_schema_version: str = FACT_SCHEMA_VERSION
    entity_type: str = "taxpayer"
    entity_label: str | None = None
    field_name: str
    predicate: str
    raw_value_text: str | None = None
    original_value: object | None = None
    normalized_value: NormalizedValue = Field(default_factory=lambda: NormalizedValue(kind="unknown"))
    unit: str | None = None
    currency_code: str | None = None
    frequency: str | None = None
    tax_year: int | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    jurisdiction: str | None = None
    origin_execution_id: str | None = None
    origin_conversation_state_record: str | None = None
    origin_turn_sequence: int = 0
    source_status: FactSourceStatus = "explicit"
    is_reusable: bool = True
    is_sensitive: bool = False
    correction_of_fact_id: str | None = None
    replacement_fact_id: str | None = None
    active: bool = True


def compare_stated_facts(
    current: Mapping[str, object],
    prior: Mapping[str, object],
    prior_execution_id: str,
) -> list[FactMismatch]:
    """Return backward-compatible mismatch records for current and prior fact ledgers."""

    current_ledger = build_canonical_fact_ledger(
        stated_facts=current,
        origin_execution_id=None,
        origin_record_id=None,
        source_status="explicit",
        turn_sequence=1,
    )
    prior_ledger = build_canonical_fact_ledger(
        stated_facts=prior,
        origin_execution_id=prior_execution_id,
        origin_record_id=None,
        source_status="reused",
        turn_sequence=0,
    )
    return compare_fact_ledgers(
        current=current_ledger,
        prior=prior_ledger,
        prior_execution_id=prior_execution_id,
    )


def build_canonical_fact_ledger(
    *,
    stated_facts: Mapping[str, object],
    origin_execution_id: str | None,
    origin_record_id: str | None,
    source_status: FactSourceStatus,
    turn_sequence: int,
) -> list[CanonicalConversationFact]:
    """Project one legacy stated-facts mapping into the canonical fact ledger."""

    if not stated_facts:
        return []
    ledger_hint = stated_facts.get("canonical_fact_ledger")
    if isinstance(ledger_hint, list) and ledger_hint:
        projected: list[CanonicalConversationFact] = []
        for item in cast(list[object], ledger_hint):
            if isinstance(item, Mapping):
                item_payload = cast(dict[str, object], item)
                projected.append(
                    CanonicalConversationFact.model_validate(
                        {
                            **item_payload,
                            "origin_execution_id": origin_execution_id,
                            "origin_conversation_state_record": origin_record_id,
                            "origin_turn_sequence": turn_sequence,
                            "source_status": source_status,
                        }
                    )
                )
        if projected:
            return projected

    facts: list[CanonicalConversationFact] = []
    field_map = {
        "income_amount_kes": "income_amount_kes",
        "income_frequency": "income_frequency",
        "turnover_amount_kes": "turnover_amount_kes",
        "residency_status": "residency_status",
        "filing_status": "filing_status",
    }
    for field_name, predicate in field_map.items():
        value = stated_facts.get(field_name)
        if value is None:
            continue
        normalized_value = _normalize_fact_value(field_name=field_name, value=value)
        facts.append(
            CanonicalConversationFact(
                fact_id=_fact_id(
                    field_name=field_name,
                    normalized_value=normalized_value,
                    origin_execution_id=origin_execution_id,
                    origin_record_id=origin_record_id,
                    turn_sequence=turn_sequence,
                ),
                field_name=field_name,
                predicate=predicate,
                raw_value_text=str(value),
                original_value=value,
                normalized_value=normalized_value,
                frequency=normalized_value.frequency,
                currency_code=normalized_value.currency_code,
                tax_year=_optional_int(stated_facts.get("tax_year")),
                effective_from=_optional_str(stated_facts.get("effective_from")),
                effective_to=_optional_str(stated_facts.get("effective_to")),
                jurisdiction=_optional_str(stated_facts.get("jurisdiction")),
                origin_execution_id=origin_execution_id,
                origin_conversation_state_record=origin_record_id,
                origin_turn_sequence=turn_sequence,
                source_status=source_status,
                is_reusable=not bool(stated_facts.get("non_reusable")),
                is_sensitive=bool(stated_facts.get("sensitive")),
                correction_of_fact_id=_optional_str(stated_facts.get("correction_of_fact_id")),
                replacement_fact_id=_optional_str(stated_facts.get("replacement_fact_id")),
                active=not bool(stated_facts.get("retracted_fields"))
                or field_name not in set(_as_string_list(stated_facts.get("retracted_fields"))),
            )
        )

    extra_fields = [
        key
        for key in stated_facts.keys()
        if key
        not in {
            "income_amount_kes",
            "income_frequency",
            "turnover_amount_kes",
            "residency_status",
            "filing_status",
            "confidence_per_field",
            "tax_year",
            "effective_from",
            "effective_to",
            "jurisdiction",
            "non_reusable",
            "sensitive",
            "correction_of_fact_id",
            "replacement_fact_id",
            "retracted_fields",
            "canonical_fact_ledger",
        }
    ]
    for key in extra_fields:
        value = stated_facts.get(key)
        if value is None:
            continue
        normalized_value = _normalize_fact_value(field_name=str(key), value=value)
        facts.append(
            CanonicalConversationFact(
                fact_id=_fact_id(
                    field_name=str(key),
                    normalized_value=normalized_value,
                    origin_execution_id=origin_execution_id,
                    origin_record_id=origin_record_id,
                    turn_sequence=turn_sequence,
                ),
                field_name=str(key),
                predicate=str(key),
                raw_value_text=str(value),
                original_value=value,
                normalized_value=normalized_value,
                frequency=normalized_value.frequency,
                currency_code=normalized_value.currency_code,
                tax_year=_optional_int(stated_facts.get("tax_year")),
                effective_from=_optional_str(stated_facts.get("effective_from")),
                effective_to=_optional_str(stated_facts.get("effective_to")),
                jurisdiction=_optional_str(stated_facts.get("jurisdiction")),
                origin_execution_id=origin_execution_id,
                origin_conversation_state_record=origin_record_id,
                origin_turn_sequence=turn_sequence,
                source_status=source_status,
                is_reusable=not bool(stated_facts.get("non_reusable")),
                is_sensitive=bool(stated_facts.get("sensitive")),
                correction_of_fact_id=_optional_str(stated_facts.get("correction_of_fact_id")),
                replacement_fact_id=_optional_str(stated_facts.get("replacement_fact_id")),
                active=not bool(stated_facts.get("retracted_fields"))
                or str(key) not in set(_as_string_list(stated_facts.get("retracted_fields"))),
            )
        )
    return facts


def compare_fact_ledgers(
    *,
    current: Sequence[CanonicalConversationFact],
    prior: Sequence[CanonicalConversationFact],
    prior_execution_id: str,
) -> list[FactMismatch]:
    """Compare two canonical fact ledgers and return backward-compatible mismatches."""

    prior_index = {(fact.entity_type, fact.field_name): fact for fact in prior if fact.active}
    mismatches: list[FactMismatch] = []
    for fact in current:
        if not fact.active:
            continue
        prior_fact = prior_index.get((fact.entity_type, fact.field_name))
        if prior_fact is None:
            continue
        comparison = _compare_fact_values(current_fact=fact, prior_fact=prior_fact)
        if comparison is None:
            continue
        mismatches.append(
            {
                "field": fact.field_name,
                "prior_value": prior_fact.original_value
                if prior_fact.original_value is not None
                else (
                    prior_fact.raw_value_text
                    if prior_fact.raw_value_text is not None
                    else _render_fact_value(prior_fact.normalized_value)
                ),
                "prior_execution_id": prior_execution_id,
                "current_value": fact.original_value
                if fact.original_value is not None
                else (
                    fact.raw_value_text
                    if fact.raw_value_text is not None
                    else _render_fact_value(fact.normalized_value)
                ),
            }
        )
    return mismatches


def _compare_fact_values(
    *,
    current_fact: CanonicalConversationFact,
    prior_fact: CanonicalConversationFact,
) -> str | None:
    if current_fact.entity_type != prior_fact.entity_type:
        return None
    if current_fact.tax_year is not None and prior_fact.tax_year is not None and current_fact.tax_year != prior_fact.tax_year:
        return None
    if current_fact.jurisdiction and prior_fact.jurisdiction and canonical_text(current_fact.jurisdiction) != canonical_text(prior_fact.jurisdiction):
        return None
    if current_fact.correction_of_fact_id or prior_fact.replacement_fact_id:
        return None
    if _normalized_fact_values_equal(current_fact.normalized_value, prior_fact.normalized_value):
        return None
    if _normalized_fact_values_compatible(current_fact.normalized_value, prior_fact.normalized_value):
        return "mismatch"
    return None


def _normalize_fact_value(*, field_name: str, value: object) -> NormalizedValue:
    if field_name in {"income_amount_kes", "turnover_amount_kes"}:
        parsed = parse_amount_text(str(value))
        if parsed is not None:
            return parsed
    if field_name == "income_frequency":
        return NormalizedValue(kind="categorical", raw_text=str(value), enum_value=str(value).strip().lower(), frequency=str(value).strip().lower())
    if field_name == "residency_status":
        return NormalizedValue(kind="categorical", raw_text=str(value), enum_value=str(value).strip().lower())
    if field_name == "filing_status":
        return NormalizedValue(kind="text", raw_text=str(value), text_value=str(value).strip())
    if isinstance(value, bool):
        return NormalizedValue(kind="boolean", raw_text=str(value), bool_value=value)
    if isinstance(value, (int, float)):
        return NormalizedValue(kind="amount", raw_text=str(value), number_value=float(value))
    if isinstance(value, str):
        rate = parse_rate_text(value)
        if rate is not None:
            return rate
        amount = parse_amount_text(value)
        if amount is not None:
            return amount
        date_value = parse_date_text(value)
        if date_value is not None:
            return date_value
        tax_year = parse_tax_year_text(value)
        if tax_year is not None:
            return NormalizedValue(kind="categorical", raw_text=value, enum_value=str(tax_year))
        return NormalizedValue(kind="text", raw_text=value, text_value=value.strip())
    return NormalizedValue(kind="unknown", raw_text=str(value))


def _normalized_fact_values_equal(a: NormalizedValue, b: NormalizedValue) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "amount":
        if a.currency_code and b.currency_code and a.currency_code != b.currency_code:
            return False
        if a.number_value is None or b.number_value is None:
            return False
        if a.frequency or b.frequency:
            target_frequency = a.frequency or b.frequency
            converted_a = convert_frequency_value(a.number_value, from_frequency=a.frequency, to_frequency=target_frequency)
            converted_b = convert_frequency_value(b.number_value, from_frequency=b.frequency, to_frequency=target_frequency)
            if converted_a is None or converted_b is None:
                return False
            return abs(converted_a - converted_b) <= max(1e-9, 1e-6 * max(abs(converted_a), abs(converted_b), 1.0))
        return abs(a.number_value - b.number_value) <= max(1e-9, 1e-6 * max(abs(a.number_value), abs(b.number_value), 1.0))
    if a.kind == "rate":
        if a.basis and b.basis and a.basis != b.basis and not _rate_basis_equivalent(a.basis, b.basis):
            return False
        return a.number_value is not None and b.number_value is not None and abs(a.number_value - b.number_value) <= max(1e-9, 1e-6 * max(abs(a.number_value), abs(b.number_value), 1.0))
    if a.kind == "date":
        return a.date_value is not None and a.date_value == b.date_value
    if a.kind == "date_range":
        return (
            a.date_start == b.date_start
            and a.date_end == b.date_end
            and a.inclusive_start == b.inclusive_start
            and a.inclusive_end == b.inclusive_end
        )
    if a.kind == "boolean":
        return a.bool_value is not None and a.bool_value == b.bool_value
    if a.kind in {"categorical", "text"}:
        return canonical_text(a.enum_value or a.text_value) == canonical_text(b.enum_value or b.text_value)
    return False


def _normalized_fact_values_compatible(a: NormalizedValue, b: NormalizedValue) -> bool:
    if a.kind == b.kind:
        return True
    return {a.kind, b.kind} <= {"amount", "rate", "date", "date_range", "boolean", "categorical", "text"}


def _render_fact_value(value: NormalizedValue) -> str:
    if value.kind == "amount" and value.number_value is not None:
        return str(value.number_value)
    if value.kind == "rate" and value.number_value is not None:
        return str(value.number_value)
    if value.kind == "date" and value.date_value:
        return value.date_value
    if value.kind == "date_range" and value.date_start and value.date_end:
        return f"{value.date_start}..{value.date_end}"
    if value.enum_value is not None:
        return value.enum_value
    if value.text_value is not None:
        return value.text_value
    if value.bool_value is not None:
        return str(value.bool_value)
    return value.raw_text or ""


def _fact_id(
    *,
    field_name: str,
    normalized_value: NormalizedValue,
    origin_execution_id: str | None,
    origin_record_id: str | None,
    turn_sequence: int,
) -> str:
    payload = {
        "field_name": field_name,
        "normalized_value": normalized_value.comparison_key(),
        "origin_execution_id": origin_execution_id,
        "origin_record_id": origin_record_id,
        "turn_sequence": turn_sequence,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"fact-{digest[:24]}"


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _rate_basis_equivalent(basis_a: str, basis_b: str) -> bool:
    pair = {basis_a.lower(), basis_b.lower()}
    return pair <= {"percentage", "decimal"}


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in cast(list[object], value):
        if isinstance(item, str):
            result.append(item)
    return result
