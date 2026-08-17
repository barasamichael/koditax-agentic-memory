"""Canonical report runtime error helpers."""

from __future__ import annotations

from typing import Final
from typing import TypedDict
from typing import NotRequired

from fastapi import Request
from fastapi import HTTPException

from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from services.reports.app.config import REPORTS_SERVICE_NAME
from services.reports.app.logging_policy import emit_report_structured_log

REPORTS_SCOPE_NOT_SUPPORTED: Final[str] = "unsupported_report_scope"
INVALID_TAX_DOMAIN: Final[str] = "invalid_tax_domain"
UNSUPPORTED_TAX_DOMAIN_PATH: Final[str] = "unsupported_tax_domain_path"
UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION: Final[
    str
] = "unimplemented_tax_domain_report_generation"
REPORT_NOT_FOUND: Final[str] = "report_not_found"
REPORT_GENERATION_NOT_SUPPORTED: Final[str] = "report_generation_not_supported"
INVALID_REPORT_REQUEST: Final[str] = "invalid_report_request"
INVALID_LINEAGE_REFERENCE: Final[str] = "invalid_lineage_reference"
REPORT_ACCESS_FORBIDDEN: Final[str] = "report_access_forbidden"
REPORT_ARTIFACT_EXPIRED: Final[str] = "report_artifact_expired"
REPORT_STORAGE_UNAVAILABLE: Final[str] = "report_storage_unavailable"
DOWNLOAD_LINK_EXPIRED: Final[str] = "download_link_expired"
REPORT_RENDERING_FAILED: Final[str] = "report_rendering_failed"
REPORT_PACKAGING_FAILED: Final[str] = "report_packaging_failed"
REPORTS_CONTRACT_VIOLATION: Final[str] = "reports_contract_violation"

REPORTS_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REPORTS_SCOPE_NOT_SUPPORTED,
        INVALID_TAX_DOMAIN,
        UNSUPPORTED_TAX_DOMAIN_PATH,
        UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION,
        REPORT_NOT_FOUND,
        REPORT_GENERATION_NOT_SUPPORTED,
        INVALID_REPORT_REQUEST,
        INVALID_LINEAGE_REFERENCE,
        REPORT_ACCESS_FORBIDDEN,
        REPORT_ARTIFACT_EXPIRED,
        REPORT_STORAGE_UNAVAILABLE,
        DOWNLOAD_LINK_EXPIRED,
        REPORT_RENDERING_FAILED,
        REPORT_PACKAGING_FAILED,
        REPORTS_CONTRACT_VIOLATION,
    }
)


class ReportErrorEnvelope(TypedDict):
    """Represent deterministic report runtime error envelope payload."""

    error_code: str
    message: str
    reason: str
    reason_code: str
    trace_id: str
    correlation_id: str
    context: NotRequired[dict[str, object]]


def build_report_error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> ReportErrorEnvelope:
    """Build deterministic report-runtime canonical error envelope."""

    normalized_reason_code = _normalize_reason_code(reason_code)
    normalized_reason = _normalize_reason_code(reason)
    normalized_error_code = error_code.strip() or normalized_reason_code
    normalized_message = message.strip() or "Reports request failed."

    envelope: ReportErrorEnvelope = {
        "error_code": normalized_error_code,
        "message": normalized_message,
        "reason": normalized_reason,
        "reason_code": normalized_reason_code,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }
    if context is not None:
        envelope["context"] = context
    return envelope


def create_report_http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> HTTPException:
    """Create deterministic HTTPException containing canonical error detail."""

    envelope = build_report_error_envelope(
        request=request,
        error_code=error_code,
        message=message,
        reason=reason,
        reason_code=reason_code,
        context=context,
    )
    tenant_id_header = request.headers.get("X-Tenant-ID", "").strip() or None
    emit_report_structured_log(
        level="error",
        service=REPORTS_SERVICE_NAME,
        event_type="reports_error_response",
        correlation_id=envelope["correlation_id"],
        tenant_id=tenant_id_header,
        report_id=_extract_report_id(context=context),
        reason_code=envelope["reason_code"],
        details={
            "status_code": status_code,
            "error_code": envelope["error_code"],
            "reason": envelope["reason"],
            "context": context or {},
        },
    )
    return HTTPException(status_code=status_code, detail=envelope)


def _normalize_reason_code(candidate: str) -> str:
    normalized_candidate = candidate.strip()
    if normalized_candidate in REPORTS_REASON_CODES:
        return normalized_candidate
    return REPORTS_CONTRACT_VIOLATION


def _extract_report_id(*, context: dict[str, object] | None) -> str | None:
    if not isinstance(context, dict):
        return None
    report_id = context.get("report_id")
    if not isinstance(report_id, str):
        return None
    normalized = report_id.strip()
    return normalized or None
