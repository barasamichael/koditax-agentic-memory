"""Structured logging with deterministic redaction for reports lifecycle flows."""

from __future__ import annotations

import re
from typing import cast
from typing import TypedDict
import logging
from datetime import UTC
from datetime import datetime
from urllib.parse import urlparse
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlunparse
from collections.abc import Mapping
from collections.abc import Sequence

from shared.determinism.input_hash import canonical_json_dumps

LOGGER = logging.getLogger("reports.structured")
REDACTED_VALUE = "[REDACTED]"


class StructuredReportLogEvent(TypedDict):
    """Represent canonical structured reports log envelope."""

    timestamp: str
    level: str
    service: str
    event_type: str
    correlation_id: str
    tenant_id: str | None
    report_id: str | None
    reason_code: str | None
    details: dict[str, object]


class InMemoryReportStructuredLogStore:
    """Collect deterministic structured reports logs for tests."""

    def __init__(self) -> None:
        self._events: list[StructuredReportLogEvent] = []

    def append(self, event: StructuredReportLogEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[StructuredReportLogEvent, ...]:
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()


_DEFAULT_REPORT_STRUCTURED_LOG_STORE = InMemoryReportStructuredLogStore()


def get_default_report_structured_log_store() -> InMemoryReportStructuredLogStore:
    """Return default deterministic reports structured log store."""

    return _DEFAULT_REPORT_STRUCTURED_LOG_STORE


def reset_default_report_structured_log_store() -> None:
    """Reset default deterministic reports structured log store for tests."""

    _DEFAULT_REPORT_STRUCTURED_LOG_STORE.clear()


def emit_report_structured_log(
    *,
    level: str,
    service: str,
    event_type: str,
    correlation_id: str,
    tenant_id: str | None,
    report_id: str | None,
    reason_code: str | None,
    details: Mapping[str, object] | None = None,
    structured_log_store: InMemoryReportStructuredLogStore | None = None,
) -> None:
    """Emit one structured reports/storage boundary log with deterministic redaction."""

    resolved_store = (
        get_default_report_structured_log_store()
        if structured_log_store is None
        else structured_log_store
    )
    event: StructuredReportLogEvent = {
        "timestamp": _timestamp_now_iso(),
        "level": level.strip().lower() or "info",
        "service": service.strip().lower(),
        "event_type": event_type.strip().lower(),
        "correlation_id": correlation_id.strip(),
        "tenant_id": None if tenant_id is None else tenant_id.strip() or None,
        "report_id": None if report_id is None else report_id.strip() or None,
        "reason_code": None if reason_code is None else reason_code.strip() or None,
        "details": _normalize_details(details=details),
    }
    try:
        resolved_store.append(event)
    except Exception:
        return
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        return


_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "apikey",
    "cookie",
    "download_url",
    "capability_id",
    "payload",
    "body",
    "email",
    "phone",
    "ssn",
    "x_user_id",
    "user_id",
)
_SAFE_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "event_type",
        "correlation_id",
        "tenant_id",
        "report_id",
        "reason_code",
        "path",
        "method",
        "status_code",
    }
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(password|token|authorization|secret|credential|api[_-]?key)\s*[:=]"),
    re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"),
)


def _normalize_details(*, details: Mapping[str, object] | None) -> dict[str, object]:
    if details is None:
        return {}
    redacted = _redact_sensitive_fields(value=dict(details))
    if not isinstance(redacted, dict):
        return {}
    redacted_mapping = cast(Mapping[str, object], redacted)
    normalized: dict[str, object] = {}
    for key in sorted(redacted_mapping.keys()):
        normalized[key] = redacted_mapping[key]
    return normalized


def _redact_sensitive_fields(*, value: object) -> object:
    if isinstance(value, Mapping):
        source = cast(Mapping[object, object], value)
        redacted: dict[str, object] = {}
        for key, nested_value in source.items():
            if not isinstance(key, str):
                continue
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_VALUE
                continue
            redacted[key] = _redact_sensitive_fields(value=nested_value)
        return redacted
    if isinstance(value, list):
        source_list = cast(list[object], value)
        return [_redact_sensitive_fields(value=item) for item in source_list]
    if isinstance(value, tuple):
        source_tuple = cast(Sequence[object], value)
        return tuple(_redact_sensitive_fields(value=item) for item in source_tuple)
    if isinstance(value, str):
        return _redact_sensitive_string(value=value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SAFE_KEY_ALLOWLIST:
        return False
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_sensitive_string(*, value: str) -> str:
    redacted_value = _redact_sensitive_url_query(value=value)
    if any(pattern.search(redacted_value) is not None for pattern in _SENSITIVE_VALUE_PATTERNS):
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


def _timestamp_now_iso() -> str:
    return datetime.now(UTC).isoformat()
