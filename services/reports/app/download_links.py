"""Reports-to-storage download capability integration helpers."""

from __future__ import annotations

from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

from services.reports.app.audit import ReportsAuditEmitter
from services.reports.app.config import get_report_reference_time
from services.reports.app.config import get_report_download_ttl_seconds
from services.reports.app.errors import REPORT_ARTIFACT_EXPIRED
from services.reports.app.errors import REPORT_STORAGE_UNAVAILABLE
from services.reports.app.models import ReportDownloadCapabilityModel
from services.storage.app.errors import STORAGE_CAPABILITY_EXPIRED
from services.storage.app.models import StorageObjectMetadataModel
from services.storage.app.models import DownloadCapabilityRequestModel
from services.reports.app.metrics import ReportsMetricsEmitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL
from services.reports.app.metrics import get_default_reports_metrics_emitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL
from services.reports.app.generation import ReportGenerationServiceError
from services.reports.app.repository import StoredReportRecord
from services.storage.app.capability_tokens import StorageCapabilityService
from services.storage.app.capability_tokens import StorageCapabilityResolutionError


def issue_report_download_capability(
    *,
    storage_service: StorageCapabilityService,
    stored_record: StoredReportRecord,
    report_id: str,
    correlation_id: str = "",
    audit_emitter: ReportsAuditEmitter | None = None,
    metrics_emitter: ReportsMetricsEmitter | None = None,
) -> ReportDownloadCapabilityModel:
    """Issue deterministic storage download capability for one report artifact."""

    effective_metrics_emitter = metrics_emitter or get_default_reports_metrics_emitter()
    created_at = _parse_created_at(stored_record.created_at)
    expiry_cutoff = created_at + timedelta(seconds=get_report_download_ttl_seconds())
    if expiry_cutoff <= get_report_reference_time():
        _emit_download_expiry_reject_metric(
            metrics_emitter=effective_metrics_emitter,
            reason_code=REPORT_ARTIFACT_EXPIRED,
        )
        raise ReportGenerationServiceError(
            reason_code=REPORT_ARTIFACT_EXPIRED,
            message="Requested report artifact has expired.",
            status_code=410,
            context={"report_id": report_id, "created_at": stored_record.created_at},
        )

    report_payload = stored_record.report_payload
    report_version_id = str(report_payload["report_version_id"])
    object_key = _build_object_key(report_id=report_id, report_version_id=report_version_id)
    lineage = report_payload.get("lineage_reference")
    if not isinstance(lineage, dict):
        raise ReportGenerationServiceError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Report lineage payload is unavailable for download capability issuance.",
            status_code=503,
            context={"report_id": report_id},
        )
    try:
        storage_service.upsert_object_metadata(
            metadata=StorageObjectMetadataModel(
                object_key=object_key,
                tenant_id=stored_record.tenant_id,
                owner_user_id=stored_record.owner_user_id,
                content_type="application/octet-stream",
                size_bytes=0,
                checksum_sha256=_default_checksum(
                    report_id=report_id,
                    report_version_id=report_version_id,
                ),
                created_at=stored_record.created_at,
                document_id=None,
            )
        )
        issued = storage_service.issue_download_capability(
            request_model=DownloadCapabilityRequestModel(
                tenant_id=stored_record.tenant_id,
                owner_user_id=stored_record.owner_user_id,
                object_key=object_key,
                document_id=None,
            ),
            idempotency_key=_build_download_idempotency_key(
                report_id=report_id,
                report_version_id=report_version_id,
            ),
        )
    except ReportGenerationServiceError:
        raise
    except Exception as error:
        raise ReportGenerationServiceError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Report storage capability issuance failed.",
            status_code=503,
            context={"report_id": report_id, "object_key": object_key},
        ) from error
    if issued is None:
        raise ReportGenerationServiceError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Report storage capability issuance failed.",
            status_code=503,
            context={"report_id": report_id, "object_key": object_key},
        )
    report_payload_lineage = _lineage_payload(stored_record=stored_record)
    if audit_emitter is not None:
        audit_emitter.append_event(
            event_type="report_download_link_issued",
            correlation_id=correlation_id or "reports-correlation-unknown",
            report_id=report_id,
            report_version_id=report_version_id,
            tenant_id=stored_record.tenant_id,
            actor_id=stored_record.owner_user_id,
            lineage=report_payload_lineage,
        )
    _emit_download_link_issued_metric(metrics_emitter=effective_metrics_emitter)
    try:
        resolved_capability = storage_service.resolve_download_capability(
            capability_id=issued.capability.capability_id
        )
    except StorageCapabilityResolutionError as error:
        if error.reason_code == STORAGE_CAPABILITY_EXPIRED:
            _emit_download_expiry_reject_metric(
                metrics_emitter=effective_metrics_emitter,
                reason_code=error.reason_code,
            )
            raise ReportGenerationServiceError(
                reason_code=REPORT_ARTIFACT_EXPIRED,
                message="Requested report artifact has expired.",
                status_code=410,
                context={"report_id": report_id, "object_key": object_key},
            ) from error
        raise ReportGenerationServiceError(
            reason_code=REPORT_STORAGE_UNAVAILABLE,
            message="Report storage capability issuance failed.",
            status_code=503,
            context={"report_id": report_id, "object_key": object_key},
        ) from error
    if audit_emitter is not None:
        audit_emitter.append_event(
            event_type="report_downloaded",
            correlation_id=correlation_id or "reports-correlation-unknown",
            report_id=report_id,
            report_version_id=report_version_id,
            tenant_id=stored_record.tenant_id,
            actor_id=stored_record.owner_user_id,
            lineage=report_payload_lineage,
        )
    return ReportDownloadCapabilityModel(
        report_id=report_id,
        capability_id=resolved_capability.capability_id,
        download_url=resolved_capability.url,
        expires_at=resolved_capability.expires_at,
    )


def _build_object_key(*, report_id: str, report_version_id: str) -> str:
    object_id = str(uuid5(NAMESPACE_URL, f"report-artifact:{report_id}:{report_version_id}"))
    return f"reports_income_tax_artifact_{object_id}.bin"


def _build_download_idempotency_key(*, report_id: str, report_version_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"report-download:{report_id}:{report_version_id}"))


def _parse_created_at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _default_checksum(*, report_id: str, report_version_id: str) -> str:
    checksum_seed = f"{report_id}:{report_version_id}"
    return str(uuid5(NAMESPACE_URL, checksum_seed)).replace("-", "").ljust(64, "0")[:64]


def _lineage_payload(*, stored_record: StoredReportRecord) -> dict[str, object]:
    report_payload = stored_record.report_payload
    lineage_value = report_payload.get("lineage_reference")
    if not isinstance(lineage_value, dict):
        return {
            "computation_id": None,
            "form_id": None,
            "historical_version_id": None,
            "supported_lane_id": None,
            "tax_type": "income_tax",
            "tax_year": None,
        }
    lineage = cast(Mapping[str, object], lineage_value)
    return {
        "computation_id": lineage.get("computation_id"),
        "form_id": lineage.get("form_id"),
        "historical_version_id": lineage.get("historical_version_id"),
        "supported_lane_id": lineage.get("supported_lane_id"),
        "tax_type": lineage.get("tax_type", "income_tax"),
        "tax_year": lineage.get("tax_year"),
    }


def _emit_download_link_issued_metric(
    *,
    metrics_emitter: ReportsMetricsEmitter | None,
) -> None:
    if metrics_emitter is None:
        return
    metrics_emitter.increment_counter_non_blocking(
        REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL,
        dimensions={"event_type": "report_download_link_issued", "status": "success"},
    )


def _emit_download_expiry_reject_metric(
    *,
    metrics_emitter: ReportsMetricsEmitter | None,
    reason_code: str,
) -> None:
    if metrics_emitter is None:
        return
    metrics_emitter.increment_counter_non_blocking(
        REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL,
        dimensions={"event_type": "report_downloaded", "reason_code": reason_code},
    )
