"""Core reports-service route scaffold for Phase 9.2.1."""

from __future__ import annotations

from typing import Any
from typing import cast
from dataclasses import asdict
from collections.abc import Mapping

from fastapi import Body
from fastapi import Request
from fastapi import APIRouter

from services.reports.app.audit import ReportsAuditEmitter
from services.reports.app.authz import is_valid_report_id
from services.reports.app.authz import ReportAccessContext
from services.reports.app.authz import resolve_report_access_context
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from services.reports.app.config import REPORTS_SERVICE_NAME
from services.reports.app.config import get_reports_service_version
from services.reports.app.errors import REPORT_NOT_FOUND
from services.reports.app.errors import INVALID_TAX_DOMAIN
from services.reports.app.errors import INVALID_REPORT_REQUEST
from services.reports.app.errors import REPORT_ACCESS_FORBIDDEN
from services.reports.app.errors import create_report_http_error
from services.reports.app.errors import INVALID_LINEAGE_REFERENCE
from services.reports.app.errors import REPORT_STORAGE_UNAVAILABLE
from services.reports.app.errors import UNSUPPORTED_TAX_DOMAIN_PATH
from services.reports.app.errors import REPORT_GENERATION_NOT_SUPPORTED
from services.reports.app.errors import UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION
from services.reports.app.generation import ReportGenerationServiceError
from services.reports.app.generation import generate_report_from_lineage_request
from services.reports.app.repository import ReportsRepository
from services.reports.app.repository import StoredReportRecord
from services.reports.app.repository import ReportRepositoryError
from services.reports.app.repository import get_default_reports_repository
from services.reports.app.download_links import issue_report_download_capability
from services.storage.app.capability_tokens import StorageCapabilityService

ROUTER = APIRouter()
REQUEST_BODY_OPTIONAL = Body(None)
RECOGNIZED_REPORT_TAX_DOMAINS: dict[str, str] = {
    "income-tax": "income-tax",
    "income_tax": "income-tax",
    "health-contribution": "health-contribution",
    "health_contribution": "health-contribution",
    "vat": "vat",
    "withholding-tax": "withholding-tax",
    "withholding_tax": "withholding-tax",
    "corporate-tax": "corporate-tax",
    "corporate_tax": "corporate-tax",
    "payroll": "payroll",
    "paye": "payroll",
}


@ROUTER.get("/healthz")
def reports_health_status(request: Request) -> dict[str, str]:
    """Expose deterministic reports-service health endpoint."""

    return {
        "status": "ok",
        "service": REPORTS_SERVICE_NAME,
        "version": get_reports_service_version(),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.get("/readyz")
def reports_readiness_status(request: Request) -> dict[str, str]:
    """Expose deterministic reports-service readiness endpoint."""

    return {
        "status": "ready",
        "service": REPORTS_SERVICE_NAME,
        "version": get_reports_service_version(),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.post("/v1/reports/income-tax/artifacts", status_code=201)
def create_income_tax_report_artifact(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Generate deterministic report metadata from finalized lineage references."""

    return _create_report_artifact_for_tax_domain(
        request=request,
        payload=payload,
        tax_domain="income_tax",
    )


@ROUTER.post("/v1/reports/health-contribution/artifacts", status_code=201)
def create_health_contribution_report_artifact(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Generate deterministic health-contribution report metadata from finalized lineage."""

    return _create_report_artifact_for_tax_domain(
        request=request,
        payload=payload,
        tax_domain="health_contribution",
    )


def _create_report_artifact_for_tax_domain(
    *,
    request: Request,
    payload: Any,
    tax_domain: str,
) -> dict[str, object]:
    """Generate deterministic report metadata for one governed tax domain."""

    repository = _get_reports_repository(request=request)
    audit_emitter = _get_reports_audit_emitter(request=request)
    access_context = resolve_report_access_context(request=request)
    try:
        generated = generate_report_from_lineage_request(
            payload=payload,
            tax_domain=tax_domain,
            repository=repository,
            access_context=access_context,
            audit_emitter=audit_emitter,
            correlation_id=get_correlation_id(request),
        )
    except ReportRepositoryError as error:
        status_code = _status_code_for_repository_error(error=error)
        raise create_report_http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context=error.context,
        ) from error
    except ReportGenerationServiceError as error:
        raise create_report_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context=error.context(),
        ) from error

    response_payload = asdict(generated)
    response_payload["traceability"] = {
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }
    return response_payload


@ROUTER.get("/v1/reports/income-tax/artifacts/{report_id}/metadata")
def get_income_tax_report_artifact_metadata(
    request: Request,
    report_id: str,
) -> dict[str, object]:
    """Retrieve deterministic report metadata by report_id with owner/tenant checks."""

    return _get_report_artifact_metadata(
        request=request,
        report_id=report_id,
        tax_domain="income_tax",
    )


@ROUTER.get("/v1/reports/health-contribution/artifacts/{report_id}/metadata")
def get_health_contribution_report_artifact_metadata(
    request: Request,
    report_id: str,
) -> dict[str, object]:
    """Retrieve deterministic health report metadata by report_id with owner checks."""

    return _get_report_artifact_metadata(
        request=request,
        report_id=report_id,
        tax_domain="health_contribution",
    )


def _get_report_artifact_metadata(
    *,
    request: Request,
    report_id: str,
    tax_domain: str,
) -> dict[str, object]:
    """Retrieve deterministic report metadata by report id with owner checks."""

    normalized_report_id = report_id.strip().lower()
    if not is_valid_report_id(normalized_report_id):
        raise create_report_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_REPORT_REQUEST,
            message="Report identifier format is invalid.",
            reason=INVALID_REPORT_REQUEST,
            reason_code=INVALID_REPORT_REQUEST,
            context={"report_id": report_id},
        )

    repository = _get_reports_repository(request=request)
    try:
        stored_record = repository.get_persisted_report_by_id(report_id=normalized_report_id)
    except ReportRepositoryError as error:
        status_code = _status_code_for_repository_error(error=error)
        raise create_report_http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context=error.context,
        ) from error
    if stored_record is None:
        raise create_report_http_error(
            request=request,
            status_code=404,
            error_code=REPORT_NOT_FOUND,
            message="Requested report artifact metadata was not found.",
            reason=REPORT_NOT_FOUND,
            reason_code=REPORT_NOT_FOUND,
            context={"report_id": normalized_report_id},
        )
    lineage_reference: object = stored_record.report_payload.get("lineage_reference")
    if isinstance(lineage_reference, Mapping):
        stored_tax_type = cast(Mapping[object, object], lineage_reference).get("tax_type")
        if stored_tax_type != tax_domain:
            raise create_report_http_error(
                request=request,
                status_code=404,
                error_code=REPORT_NOT_FOUND,
                message="Requested report artifact metadata was not found.",
                reason=REPORT_NOT_FOUND,
                reason_code=REPORT_NOT_FOUND,
                context={"report_id": normalized_report_id},
            )

    access_context = resolve_report_access_context(request=request)
    _enforce_owner_tenant_access(
        request=request,
        report_id=normalized_report_id,
        access_context=access_context,
        stored_record=stored_record,
    )
    storage_service = _get_storage_capability_service(request=request)
    try:
        download_capability = issue_report_download_capability(
            storage_service=storage_service,
            stored_record=stored_record,
            report_id=normalized_report_id,
            correlation_id=get_correlation_id(request),
            audit_emitter=_get_reports_audit_emitter(request=request),
        )
    except ReportGenerationServiceError as error:
        raise create_report_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context=error.context(),
        ) from error
    response_payload = dict(stored_record.report_payload)
    response_payload["status"] = "ok"
    response_payload["created_at"] = stored_record.created_at
    response_payload["download_capability"] = asdict(download_capability)
    response_payload["traceability"] = {
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }
    return response_payload


@ROUTER.api_route(
    "/v1/reports/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def reports_runtime_scaffold(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for invalid tax domains and recognized but unimplemented paths."""

    normalized_scope = _normalize_report_tax_domain(scope)
    requested_path = f"/v1/reports/{scope}/{remaining_path}".rstrip("/")
    if normalized_scope is None:
        raise create_report_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_TAX_DOMAIN,
            message="Requested tax domain is not recognized by the reports boundary.",
            reason=INVALID_TAX_DOMAIN,
            reason_code=INVALID_TAX_DOMAIN,
            context={
                "requested_path": requested_path,
                "tax_domain": scope.strip().lower() or "unknown",
            },
        )
    if normalized_scope != "income-tax":
        normalized_remaining_path = remaining_path.strip().lower().strip("/")
        if normalized_remaining_path == "artifacts":
            raise create_report_http_error(
                request=request,
                status_code=501,
                error_code=UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION,
                message=(
                    "Report generation for the requested recognized tax "
                    "domain is not yet implemented."
                ),
                reason=UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION,
                reason_code=UNIMPLEMENTED_TAX_DOMAIN_REPORT_GENERATION,
                context={
                    "requested_path": requested_path,
                    "tax_domain": normalized_scope.replace("-", "_"),
                },
            )
        raise create_report_http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_TAX_DOMAIN_PATH,
            message="Requested reports path is not available for the recognized tax domain.",
            reason=UNSUPPORTED_TAX_DOMAIN_PATH,
            reason_code=UNSUPPORTED_TAX_DOMAIN_PATH,
            context={
                "requested_path": requested_path,
                "tax_domain": normalized_scope.replace("-", "_"),
            },
        )

    raise create_report_http_error(
        request=request,
        status_code=501,
        error_code=REPORT_GENERATION_NOT_SUPPORTED,
        message="Requested report operation is not implemented in the Phase 9.2.1 baseline.",
        reason=REPORT_GENERATION_NOT_SUPPORTED,
        reason_code=REPORT_GENERATION_NOT_SUPPORTED,
        context={"requested_path": requested_path, "scope": normalized_scope},
    )


def _get_reports_repository(*, request: Request) -> ReportsRepository:
    configured_repository = getattr(request.app.state, "reports_repository", None)
    if isinstance(configured_repository, ReportsRepository):
        return configured_repository
    default_repository = get_default_reports_repository()
    request.app.state.reports_repository = default_repository
    return default_repository


def _enforce_owner_tenant_access(
    *,
    request: Request,
    report_id: str,
    access_context: ReportAccessContext,
    stored_record: StoredReportRecord,
) -> None:
    if stored_record.tenant_id != access_context.tenant_id:
        raise create_report_http_error(
            request=request,
            status_code=403,
            error_code=REPORT_ACCESS_FORBIDDEN,
            message="Report access is forbidden for this tenant context.",
            reason=REPORT_ACCESS_FORBIDDEN,
            reason_code=REPORT_ACCESS_FORBIDDEN,
            context={"report_id": report_id, "boundary": "tenant"},
        )
    if stored_record.owner_user_id != access_context.owner_user_id:
        raise create_report_http_error(
            request=request,
            status_code=403,
            error_code=REPORT_ACCESS_FORBIDDEN,
            message="Report access is forbidden for this owner context.",
            reason=REPORT_ACCESS_FORBIDDEN,
            reason_code=REPORT_ACCESS_FORBIDDEN,
            context={"report_id": report_id, "boundary": "owner"},
        )


def _status_code_for_repository_error(*, error: ReportRepositoryError) -> int:
    if error.reason_code == INVALID_LINEAGE_REFERENCE:
        return 400
    if error.reason_code == REPORT_NOT_FOUND:
        return 404
    if error.reason_code == REPORT_STORAGE_UNAVAILABLE:
        return 503
    return 500


def _get_storage_capability_service(*, request: Request) -> StorageCapabilityService:
    configured = getattr(request.app.state, "storage_capability_service", None)
    if isinstance(configured, StorageCapabilityService):
        return configured
    service = StorageCapabilityService()
    request.app.state.storage_capability_service = service
    return service


def _get_reports_audit_emitter(*, request: Request) -> ReportsAuditEmitter:
    configured = getattr(request.app.state, "reports_audit_emitter", None)
    if isinstance(configured, ReportsAuditEmitter):
        return configured
    emitter = ReportsAuditEmitter()
    request.app.state.reports_audit_emitter = emitter
    return emitter


def _normalize_report_tax_domain(scope: str) -> str | None:
    normalized_scope = scope.strip().lower()
    if not normalized_scope:
        return None
    return RECOGNIZED_REPORT_TAX_DOMAINS.get(normalized_scope)
