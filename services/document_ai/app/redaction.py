"""Centralized sensitive-field redaction policy for document_ai surfaces."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlparse
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlunparse

REDACTED_VALUE = "***redacted***"
SENSITIVE_KEY_FAMILIES: tuple[str, ...] = (
    "token",
    "secret",
    "authorization",
    "signature",
    "api_key",
    "apikey",
    "password",
)
TRACEABILITY_KEY_ALLOWLIST: frozenset[str] = frozenset({"trace_id", "correlation_id"})


def redact_sensitive_fields(value: object) -> object:
    """Recursively redact sensitive values from nested payloads deterministically."""

    if isinstance(value, dict):
        value_map = cast(dict[str, object], value)
        redacted: dict[str, object] = {}
        for key, nested_value in value_map.items():
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_VALUE
                continue
            if isinstance(nested_value, str):
                redacted[key] = _redact_sensitive_url_query(nested_value)
                continue
            redacted[key] = redact_sensitive_fields(nested_value)
        return redacted

    if isinstance(value, list):
        return [redact_sensitive_fields(entry) for entry in cast(list[object], value)]

    if isinstance(value, tuple):
        return tuple(redact_sensitive_fields(entry) for entry in cast(tuple[object, ...], value))

    if isinstance(value, str):
        return _redact_sensitive_url_query(value)

    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in TRACEABILITY_KEY_ALLOWLIST:
        return False
    if normalized.endswith("_env_var") or normalized.endswith("_environment_variable"):
        return False
    return any(part in normalized for part in SENSITIVE_KEY_FAMILIES)


def _redact_sensitive_url_query(value: str) -> str:
    if "://" not in value or "?" not in value:
        return value
    parsed = urlparse(value)
    if not parsed.query:
        return value
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    redacted_pairs: list[tuple[str, str]] = []
    for key, query_value in query_pairs:
        if _is_sensitive_key(key):
            redacted_pairs.append((key, REDACTED_VALUE))
            continue
        redacted_pairs.append((key, query_value))
    return urlunparse(parsed._replace(query=urlencode(redacted_pairs)))
