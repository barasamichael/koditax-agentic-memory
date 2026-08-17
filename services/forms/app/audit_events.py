"""Canonical forms audit taxonomy helpers with deterministic immutable evidence payloads."""

from __future__ import annotations

from typing import cast
from typing import Final
import hashlib
import logging
from datetime import datetime
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.retention_policy import get_forms_retention_reference_time

FORMS_AUDIT_LOG_EVENT_NAME: Final[str] = "forms_audit_event"

FORMS_AUDIT_EVENT_VALIDATION_EXECUTED: Final[str] = "forms_validation_executed"
FORMS_AUDIT_EVENT_ARTIFACT_GENERATED: Final[str] = "forms_artifact_generated"
FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED: Final[str] = "forms_history_record_persisted"
FORMS_AUDIT_EVENT_DOWNLOAD_LINK_ISSUED: Final[str] = "forms_download_link_issued"
FORMS_AUDIT_EVENT_ACCESS_DENIED: Final[str] = "forms_access_denied"

REQUIRED_AUDIT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        FORMS_AUDIT_EVENT_VALIDATION_EXECUTED,
        FORMS_AUDIT_EVENT_ARTIFACT_GENERATED,
        FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED,
        FORMS_AUDIT_EVENT_DOWNLOAD_LINK_ISSUED,
        FORMS_AUDIT_EVENT_ACCESS_DENIED,
    }
)

LOGGER = logging.getLogger("kodi.forms.audit")


def get_forms_audit_event_timestamp(*, now: datetime | None = None) -> str:
    """Return ISO timestamp suitable for audit evidence with deterministic override support."""

    reference_time = get_forms_retention_reference_time(now=now)
    return reference_time.isoformat()


def build_forms_audit_event_id(payload: Mapping[str, object]) -> str:
    """Build deterministic sha256 evidence id for one canonical audit payload."""

    normalized = _as_object(payload)
    return hashlib.sha256(canonical_json_dumps(normalized).encode("utf-8")).hexdigest()


def build_forms_audit_evidence_envelope(
    *,
    audit_event_id: str,
    event_type: str,
    event_timestamp: str,
    trace_id: str,
    correlation_id: str,
    lineage_reference: Mapping[str, object],
    actor_context: Mapping[str, object],
) -> dict[str, object]:
    """Build one contract-aligned FormsAuditEvidenceEnvelope."""

    normalized_audit_event_id = audit_event_id.strip()
    normalized_event_type = event_type.strip()
    normalized_event_timestamp = event_timestamp.strip()
    normalized_trace_id = trace_id.strip()
    normalized_correlation_id = correlation_id.strip()

    if not normalized_audit_event_id:
        raise ValueError("audit_event_id must be non-empty")
    if not normalized_event_type:
        raise ValueError("event_type must be non-empty")
    if not normalized_event_timestamp:
        raise ValueError("event_timestamp must be non-empty")
    if not normalized_trace_id:
        raise ValueError("trace_id must be non-empty")
    if not normalized_correlation_id:
        raise ValueError("correlation_id must be non-empty")

    return {
        "audit_event_id": normalized_audit_event_id,
        "event_type": normalized_event_type,
        "event_timestamp": normalized_event_timestamp,
        "trace_id": normalized_trace_id,
        "correlation_id": normalized_correlation_id,
        "lineage_reference": dict(_as_object(lineage_reference)),
        "actor_context": dict(_as_object(actor_context)),
    }


def emit_forms_audit_log_event(payload: Mapping[str, object]) -> dict[str, object]:
    """Emit one structured audit log entry (no secrets/PII expected) and return payload."""

    normalized = _as_object(payload)
    LOGGER.info("%s %s", FORMS_AUDIT_LOG_EVENT_NAME, canonical_json_dumps(normalized))
    return normalized


def _as_object(value: Mapping[str, object]) -> dict[str, object]:
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}
