"""Deterministic prior-year pre-population helpers for supported forms scope."""

from __future__ import annotations

from typing import cast
from typing import Final
from collections.abc import Mapping

PRE_POPULATION_POLICY_TAG: Final[str] = "prior_year_artifact_whitelist_v1"
PRE_POPULATION_FIELD_WHITELIST: Final[tuple[str, ...]] = (
    "taxpayer.taxpayer_kind",
    "taxpayer.resident_status",
    "taxpayer.classification_outcome",
    "form_fields.employment_income_kes",
    "form_fields.investment_income_kes",
    "form_fields.chargeable_income_kes",
    "form_fields.total_reliefs_kes",
    "form_fields.net_income_tax_due_kes",
)


def build_pre_population_source_fields(
    *,
    generated_content_payload: Mapping[str, object],
) -> dict[str, object]:
    """Extract deterministic whitelisted source fields from generated artifact payload."""

    source_fields: dict[str, object] = {}
    for field_path in PRE_POPULATION_FIELD_WHITELIST:
        field_value = _extract_dotted_path_value(generated_content_payload, field_path)
        if _is_allowed_pre_population_value(field_value):
            source_fields[field_path] = field_value
    return source_fields


def build_pre_population_field_suggestions(
    *,
    source_fields: Mapping[str, object],
    source_artifact_id: str,
    source_tax_year: int,
) -> list[dict[str, object]]:
    """Build deterministic field-level pre-population suggestions with provenance."""

    suggestions: list[dict[str, object]] = []
    for field_path in PRE_POPULATION_FIELD_WHITELIST:
        field_value = source_fields.get(field_path)
        if not _is_allowed_pre_population_value(field_value):
            continue
        suggestions.append(
            {
                "field": field_path,
                "value": field_value,
                "source_artifact_id": source_artifact_id,
                "source_tax_year": source_tax_year,
                "policy_tag": PRE_POPULATION_POLICY_TAG,
            }
        )
    return suggestions


def _extract_dotted_path_value(source: Mapping[str, object], dotted_path: str) -> object:
    path_parts = [part for part in dotted_path.split(".") if part]
    current_value: object = source
    for path_part in path_parts:
        if not isinstance(current_value, Mapping):
            return None
        current_mapping = cast(Mapping[object, object], current_value)
        if path_part not in current_mapping:
            return None
        current_value = current_mapping[path_part]
    return current_value


def _is_allowed_pre_population_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return False
