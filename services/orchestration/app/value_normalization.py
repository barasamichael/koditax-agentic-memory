"""Deterministic normalization helpers for canonical claim and fact comparison."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

_AMOUNT_SCALE_FACTORS: dict[str, float] = {
    "thousand": 1_000.0,
    "k": 1_000.0,
    "million": 1_000_000.0,
    "m": 1_000_000.0,
    "billion": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
}

_FREQUENCY_MULTIPLIERS: dict[str, float] = {
    "one_time": 1.0,
    "per_transaction": 1.0,
    "daily": 365.0,
    "weekly": 52.0,
    "monthly": 12.0,
    "quarterly": 4.0,
    "annual": 1.0,
    "yearly": 1.0,
}

_DATE_RANGE_CONNECTOR = re.compile(r"\s*(?:to|through|until|[-–—])\s*", re.IGNORECASE)
_NUMERIC_TOKEN = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_PERCENT_TOKEN = re.compile(r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*%\s*$")
_CURRENCY_TOKEN = re.compile(r"^(KES|USD|EUR|GBP|TZS|UGX|RWF|ETB|NGN|ZAR)\b", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedValue:
    """Represent one deterministic comparison value."""

    kind: Literal[
        "amount",
        "rate",
        "date",
        "date_range",
        "duration",
        "threshold",
        "boolean",
        "categorical",
        "text",
        "unknown",
    ]
    raw_text: str | None = None
    number_value: float | None = None
    currency_code: str | None = None
    unit: str | None = None
    frequency: str | None = None
    scale: str | None = None
    date_value: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    inclusive_start: bool | None = None
    inclusive_end: bool | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    basis: str | None = None
    enum_value: str | None = None
    bool_value: bool | None = None
    text_value: str | None = None

    def comparison_key(self) -> tuple[object, ...]:
        """Return a canonical comparison tuple for deterministic equality checks."""

        return (
            self.kind,
            self.number_value,
            self.currency_code,
            self.unit,
            self.frequency,
            self.scale,
            self.date_value,
            self.date_start,
            self.date_end,
            self.inclusive_start,
            self.inclusive_end,
            self.lower_bound,
            self.upper_bound,
            self.basis,
            self.enum_value,
            self.bool_value,
            self.text_value,
        )


def canonical_text(value: object) -> str:
    """Return a stable string for fingerprints and audit-safe summaries."""

    if value is None:
        return ""
    text = str(value).strip().lower()
    return re.sub(r"\s+", " ", text)


def parse_float_token(value: object) -> float | None:
    """Parse one numeric token without performing currency or rate inference."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_amount_text(raw_text: str | None) -> NormalizedValue | None:
    """Parse one currency amount with optional scale and frequency hints."""

    if raw_text is None:
        return None
    text = raw_text.strip()
    if not text:
        return None
    currency_match = _CURRENCY_TOKEN.match(text)
    currency_code = currency_match.group(1).upper() if currency_match else None
    number_match = _NUMERIC_TOKEN.search(text)
    if number_match is None:
        return None
    number = parse_float_token(number_match.group(0))
    if number is None:
        return None
    scale = None
    for label, factor in _AMOUNT_SCALE_FACTORS.items():
        if re.search(rf"\b{re.escape(label)}\b", text, re.IGNORECASE):
            scale = label
            number *= factor
            break
    frequency = parse_frequency_text(text)
    return NormalizedValue(
        kind="amount",
        raw_text=raw_text,
        number_value=number,
        currency_code=currency_code,
        frequency=frequency,
        scale=scale,
    )


def parse_rate_text(raw_text: str | None) -> NormalizedValue | None:
    """Parse one percentage or decimal rate."""

    if raw_text is None:
        return None
    text = raw_text.strip()
    if not text:
        return None
    percent_match = _PERCENT_TOKEN.match(text)
    if percent_match is not None:
        number = parse_float_token(percent_match.group(1))
        if number is None:
            return None
        return NormalizedValue(
            kind="rate",
            raw_text=raw_text,
            number_value=number / 100.0,
            unit="percent",
            basis="percentage",
        )
    number = parse_float_token(text)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        return NormalizedValue(
            kind="rate",
            raw_text=raw_text,
            number_value=number,
            unit="decimal",
            basis="decimal",
        )
    return NormalizedValue(
        kind="rate",
        raw_text=raw_text,
        number_value=number,
        basis="numeric",
    )


def parse_frequency_text(value: object) -> str | None:
    """Return one normalized frequency label when it is recognized."""

    if value is None:
        return None
    normalized = canonical_text(value)
    for label in (
        "one_time",
        "per_transaction",
        "daily",
        "weekly",
        "monthly",
        "quarterly",
        "annual",
        "yearly",
    ):
        if label in normalized:
            return "annual" if label == "yearly" else label
    return None


def parse_date_text(raw_text: str | None) -> NormalizedValue | None:
    """Parse one ISO date or simple date range."""

    if raw_text is None:
        return None
    text = raw_text.strip()
    if not text:
        return None
    if _DATE_RANGE_CONNECTOR.search(text):
        parts = [part.strip() for part in _DATE_RANGE_CONNECTOR.split(text) if part.strip()]
        if len(parts) == 2:
            start = _parse_date(parts[0])
            end = _parse_date(parts[1])
            if start is not None and end is not None:
                return NormalizedValue(
                    kind="date_range",
                    raw_text=raw_text,
                    date_start=start,
                    date_end=end,
                    inclusive_start=True,
                    inclusive_end=True,
                )
    parsed = _parse_date(text)
    if parsed is None:
        return None
    return NormalizedValue(kind="date", raw_text=raw_text, date_value=parsed)


def parse_tax_year_text(raw_text: str | None) -> int | None:
    """Parse one tax year from explicit text."""

    if raw_text is None:
        return None
    match = re.search(r"\b(19\d{2}|20\d{2})\b", raw_text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def convert_frequency_value(
    value: float,
    *,
    from_frequency: str | None,
    to_frequency: str | None,
) -> float | None:
    """Convert one periodic value between known compatible frequencies."""

    if from_frequency == to_frequency or to_frequency is None:
        return value
    from_multiplier = _FREQUENCY_MULTIPLIERS.get(from_frequency or "")
    to_multiplier = _FREQUENCY_MULTIPLIERS.get(to_frequency or "")
    if from_multiplier is None or to_multiplier is None:
        return None
    if from_multiplier <= 0.0 or to_multiplier <= 0.0:
        return None
    annualized = value * from_multiplier
    return annualized / to_multiplier


def _parse_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None

