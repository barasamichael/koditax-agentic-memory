"""Build deterministic trace context for orchestration boundaries."""

from __future__ import annotations

from typing import TypedDict
import hashlib

from shared.determinism.input_hash import canonical_json_dumps


class TraceContext(TypedDict):
    """Represent deterministic trace linkage identifiers."""

    correlation_id: str
    trace_id: str


def build_trace_id(correlation_id: str) -> str:
    """Build deterministic trace identifier from correlation context."""

    normalized = correlation_id.strip()
    digest_input = {
        "scope": "income_tax_orchestration_trace_context",
        "correlation_id": normalized,
    }
    return hashlib.sha256(canonical_json_dumps(digest_input).encode("utf-8")).hexdigest()


def build_trace_context(correlation_id: str) -> TraceContext:
    """Build deterministic trace context envelope from correlation identifier."""

    normalized = correlation_id.strip()
    return {
        "correlation_id": normalized,
        "trace_id": build_trace_id(normalized),
    }


def build_optional_trace_id(correlation_id: str | None) -> str | None:
    """Build deterministic trace identifier when correlation exists."""

    if correlation_id is None:
        return None
    normalized = correlation_id.strip()
    if not normalized:
        return None
    return build_trace_id(normalized)
