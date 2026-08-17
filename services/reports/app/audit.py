"""Immutable append-only audit event emitter for reports lifecycle events."""

from __future__ import annotations

import os
from typing import Literal
import hashlib
from datetime import UTC
from datetime import datetime
from threading import Lock
from dataclasses import asdict
from dataclasses import dataclass

from shared.determinism.input_hash import canonical_json_dumps

ReportAuditEventType = Literal[
    "report_generated",
    "report_download_link_issued",
    "report_downloaded",
    "report_generation_failed",
]


@dataclass(frozen=True)
class ReportAuditEvent:
    event_id: str
    event_type: ReportAuditEventType
    occurred_at: str
    correlation_id: str
    report_id: str | None
    report_version_id: str | None
    tenant_id: str
    actor_id: str
    lineage: dict[str, object]
    error_code: str | None = None
    reason_code: str | None = None
    reason: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        if payload["error_code"] is None:
            payload.pop("error_code")
        if payload["reason_code"] is None:
            payload.pop("reason_code")
        if payload["reason"] is None:
            payload.pop("reason")
        return payload


class ReportsAuditEmitter:
    """Emit deterministic immutable audit events with append-only semantics."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[ReportAuditEvent] = []

    def append_event(
        self,
        *,
        event_type: ReportAuditEventType,
        correlation_id: str,
        report_id: str | None,
        report_version_id: str | None,
        tenant_id: str,
        actor_id: str,
        lineage: dict[str, object],
        error_code: str | None = None,
        reason_code: str | None = None,
        reason: str | None = None,
    ) -> ReportAuditEvent:
        normalized_lineage = _normalize_lineage(lineage=lineage)
        occurred_at = _current_time_iso()
        event_payload: dict[str, object] = {
            "event_type": event_type,
            "occurred_at": occurred_at,
            "correlation_id": correlation_id,
            "report_id": report_id,
            "report_version_id": report_version_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "lineage": normalized_lineage,
            "error_code": error_code,
            "reason_code": reason_code,
            "reason": reason,
        }
        event_id = _deterministic_event_id(payload=event_payload)
        event = ReportAuditEvent(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            report_id=report_id,
            report_version_id=report_version_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            lineage=normalized_lineage,
            error_code=error_code,
            reason_code=reason_code,
            reason=reason,
        )
        with self._lock:
            self._events.append(event)
        return event

    def snapshot(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(event.to_payload().copy() for event in self._events)


def _normalize_lineage(*, lineage: dict[str, object]) -> dict[str, object]:
    required_keys = (
        "computation_id",
        "form_id",
        "historical_version_id",
        "supported_lane_id",
    )
    normalized: dict[str, object] = {}
    for key in required_keys:
        normalized[key] = lineage.get(key)
    if "tax_type" in lineage:
        normalized["tax_type"] = lineage.get("tax_type")
    if "tax_year" in lineage:
        normalized["tax_year"] = lineage.get("tax_year")
    return normalized


def _current_time_iso() -> str:
    raw = os.getenv("REPORTS_AUDIT_EVENT_TIME", "").strip()
    if raw:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC).isoformat()
        return parsed.astimezone(UTC).isoformat()
    return datetime.now(UTC).isoformat()


def _deterministic_event_id(*, payload: dict[str, object]) -> str:
    serialized = canonical_json_dumps(payload)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    return f"evt_{digest}"
