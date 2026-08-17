"""Structured logging utilities with deterministic redaction for auth flows."""

from __future__ import annotations

import re
from uuid import UUID
from typing import cast
from typing import TypedDict
import logging
from urllib.parse import urlparse
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlunparse
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

LOGGER = logging.getLogger("auth.structured")
REDACTED_VALUE = "***redacted***"


class StructuredAuthLogEvent(TypedDict):
    """Represent canonical structured auth log envelope."""

    event_type: str
    event_status: str
    reason_code: str | None
    trace_id: str
    correlation_id: str
    user_id: str | None
    tenant_id: str
    details: dict[str, object]


class InMemoryAuthStructuredLogStore:
    """Collect structured auth log events in deterministic in-memory storage."""

    def __init__(self) -> None:
        self._events: list[StructuredAuthLogEvent] = []

    def append(self, event: StructuredAuthLogEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[StructuredAuthLogEvent, ...]:
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()


_DEFAULT_AUTH_STRUCTURED_LOG_STORE = InMemoryAuthStructuredLogStore()


def get_default_auth_structured_log_store() -> InMemoryAuthStructuredLogStore:
    """Return default deterministic auth structured-log store."""

    return _DEFAULT_AUTH_STRUCTURED_LOG_STORE


def reset_default_auth_structured_log_store() -> None:
    """Reset default deterministic auth structured-log store for tests."""

    _DEFAULT_AUTH_STRUCTURED_LOG_STORE.clear()


def emit_auth_structured_log(
    *,
    event_type: str,
    event_status: str,
    reason_code: str | None,
    trace_id: str,
    correlation_id: str,
    user_id: UUID | str | None,
    tenant_id: str,
    details: Mapping[str, object] | None = None,
    structured_log_store: InMemoryAuthStructuredLogStore | None = None,
) -> None:
    """Emit one structured auth log event with deterministic redaction."""

    resolved_store = (
        get_default_auth_structured_log_store()
        if structured_log_store is None
        else structured_log_store
    )
    event: StructuredAuthLogEvent = {
        "event_type": event_type.strip().lower(),
        "event_status": event_status.strip().lower(),
        "reason_code": None if reason_code is None else reason_code.strip(),
        "trace_id": trace_id.strip(),
        "correlation_id": correlation_id.strip(),
        "user_id": None if user_id is None else str(user_id),
        "tenant_id": tenant_id.strip(),
        "details": _normalize_details(details=details),
    }
    try:
        resolved_store.append(event)
    except Exception:
        _emit_structured_log_warning(event=event, failure_stage="store_append_failed")
        return
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        _emit_structured_log_warning(event=event, failure_stage="logger_emit_failed")
        return


_TRACEABILITY_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "event_type",
        "event_status",
        "reason_code",
        "trace_id",
        "correlation_id",
        "user_id",
        "tenant_id",
    }
)
_SENSITIVE_KEY_FAMILIES: tuple[str, ...] = (
    "password",
    "otp",
    "token",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "proof",
    "email",
    "phone",
    "recipient",
    "destination",
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(password|otp|secret|credential|authorization)\s*[:=]"),
    re.compile(r"(?i)eyj[a-z0-9_-]{10,}\.[a-z0-9._-]+\.[a-z0-9._-]+"),
)


def _normalize_details(
    *, details: Mapping[str, object] | None
) -> dict[str, object]:
    if details is None:
        return {}
    sanitized = _redact_sensitive_fields(value=dict(details))
    if not isinstance(sanitized, dict):
        return {}
    normalized: dict[str, object] = {}
    sanitized_map = cast(dict[str, object], sanitized)
    for key in sorted(sanitized_map):
        normalized[key] = sanitized_map[key]
    return normalized


def _redact_sensitive_fields(*, value: object) -> object:
    if isinstance(value, dict):
        value_map = cast(dict[str, object], value)
        redacted: dict[str, object] = {}
        for key, nested_value in value_map.items():
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_VALUE
                continue
            redacted[key] = _redact_sensitive_fields(value=nested_value)
        return redacted
    if isinstance(value, list):
        return [
            _redact_sensitive_fields(value=item)
            for item in cast(list[object], value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_sensitive_fields(value=item)
            for item in cast(tuple[object, ...], value)
        )
    if isinstance(value, str):
        return _redact_sensitive_string(value=value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _TRACEABILITY_KEY_ALLOWLIST:
        return False
    return any(token in normalized for token in _SENSITIVE_KEY_FAMILIES)


def _redact_sensitive_string(*, value: str) -> str:
    redacted_value = _redact_sensitive_url_query(value=value)
    if any(
        pattern.search(redacted_value) is not None
        for pattern in _SENSITIVE_VALUE_PATTERNS
    ):
        return REDACTED_VALUE
    return redacted_value


def _redact_sensitive_url_query(*, value: str) -> str:
    if "://" not in value or "?" not in value:
        return value
    parsed = urlparse(value)
    if not parsed.query:
        return value
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    redacted_pairs: list[tuple[str, str]] = []
    for key, query_value in pairs:
        if _is_sensitive_key(key):
            redacted_pairs.append((key, REDACTED_VALUE))
            continue
        redacted_pairs.append((key, query_value))
    return urlunparse(parsed._replace(query=urlencode(redacted_pairs)))


def _emit_structured_log_warning(
    *,
    event: StructuredAuthLogEvent,
    failure_stage: str,
) -> None:
    warning_event = {
        "event_type": event["event_type"],
        "event_status": event["event_status"],
        "reason_code": event["reason_code"],
        "trace_id": event["trace_id"],
        "correlation_id": event["correlation_id"],
        "tenant_id": event["tenant_id"],
        "failure_stage": failure_stage,
    }
    try:
        LOGGER.warning(canonical_json_dumps(warning_event))
    except Exception:
        return
