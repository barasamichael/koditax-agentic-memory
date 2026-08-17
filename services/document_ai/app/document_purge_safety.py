"""Evaluate deterministic read-only purge safety for document lifecycle dry-run checks."""

from __future__ import annotations

from uuid import UUID
from typing import Literal
import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from pydantic import BaseModel

from shared.determinism.input_hash import canonical_json_dumps
from services.document_ai.app.upload_sessions import UploadSessionTraceability
from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.document_lifecycle import is_document_compliance_lock_active

_EVALUATED_AT_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class DocumentPurgeSafetyDryRunEnvelope(BaseModel):
    """Represent deterministic purge precheck dry-run output."""

    status: Literal["ok"] = "ok"
    document_id: UUID
    dry_run: Literal[True] = True
    purge_ready: bool
    blockers: list[str]
    evaluated_at: str
    traceability: UploadSessionTraceability


def evaluate_document_purge_safety(
    *,
    document_record: PersistedDocumentRecord,
    correlation_id: str,
    trace_id: str,
    now_utc: datetime | None = None,
) -> DocumentPurgeSafetyDryRunEnvelope:
    """Evaluate purge readiness with deterministic blocker codes and no state mutation."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    blockers: list[str] = []
    if is_document_compliance_lock_active(
        compliance_lock_until=document_record.compliance_lock_until,
        now_utc=reference_now,
    ):
        _append_unique(blockers, "compliance_lock_active")

    if document_record.state == "purged":
        _append_unique(blockers, "already_purged")
    elif document_record.state != "eligible_for_purge":
        _append_unique(blockers, "invalid_execute_purge_state_transition")

    parsed_uploaded_at = _parse_iso_utc(document_record.uploaded_at)
    if parsed_uploaded_at is None:
        _append_unique(blockers, "invalid_uploaded_at")

    if document_record.purge_eligible_at is None:
        _append_unique(blockers, "missing_purge_eligible_at")
    else:
        parsed_purge_eligible_at = _parse_iso_utc(document_record.purge_eligible_at)
        if parsed_purge_eligible_at is None:
            _append_unique(blockers, "invalid_purge_eligible_at")
        else:
            if parsed_uploaded_at is not None and parsed_purge_eligible_at < parsed_uploaded_at:
                _append_unique(blockers, "purge_eligible_at_before_uploaded_at")
            if parsed_purge_eligible_at > reference_now:
                _append_unique(blockers, "purge_not_yet_eligible")

    evaluated_at = _deterministic_evaluated_at(
        document_record=document_record,
        blockers=blockers,
        correlation_id=correlation_id,
        trace_id=trace_id,
    )
    return DocumentPurgeSafetyDryRunEnvelope(
        status="ok",
        document_id=document_record.document_id,
        dry_run=True,
        purge_ready=not blockers,
        blockers=blockers,
        evaluated_at=evaluated_at,
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    )


def _deterministic_evaluated_at(
    *,
    document_record: PersistedDocumentRecord,
    blockers: list[str],
    correlation_id: str,
    trace_id: str,
) -> str:
    identity = {
        "scope": "document_purge_safety_dry_run",
        "document_id": str(document_record.document_id),
        "state": document_record.state,
        "uploaded_at": document_record.uploaded_at,
        "purge_eligible_at": document_record.purge_eligible_at,
        "purged_at": document_record.purged_at,
        "compliance_lock_until": document_record.compliance_lock_until,
        "tenant_id": document_record.tenant_id,
        "owner_user_id": str(document_record.owner_user_id),
        "blockers": blockers,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    digest = hashlib.sha256(canonical_json_dumps(identity).encode("utf-8")).hexdigest()
    offset_seconds = int(digest[:8], 16) % (365 * 24 * 60 * 60)
    return (_EVALUATED_AT_BASE + timedelta(seconds=offset_seconds)).isoformat()


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _append_unique(values: list[str], item: str) -> None:
    if item not in values:
        values.append(item)
