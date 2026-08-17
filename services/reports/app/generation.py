"""Deterministic report-generation flow from finalized lineage references."""

from __future__ import annotations

from time import perf_counter
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
import hashlib
from collections.abc import Mapping

from services.reports.app.audit import ReportsAuditEmitter
from services.reports.app.authz import ReportAccessContext
from services.reports.app.errors import INVALID_REPORT_REQUEST
from services.reports.app.errors import REPORT_PACKAGING_FAILED
from services.reports.app.errors import REPORT_RENDERING_FAILED
from services.reports.app.errors import INVALID_LINEAGE_REFERENCE
from services.reports.app.errors import REPORT_GENERATION_NOT_SUPPORTED
from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportArtifactMetadataModel
from services.reports.app.models import ReportGenerationRequestModel
from services.reports.app.models import ReportGenerationResponseModel
from services.reports.app.metrics import ReportsMetricsEmitter
from services.reports.app.metrics import REPORTS_GENERATION_TOTAL
from services.reports.app.metrics import REPORTS_GENERATION_LATENCY_MS
from services.reports.app.metrics import REPORTS_GENERATION_FAILURES_TOTAL
from services.reports.app.metrics import get_default_reports_metrics_emitter
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.repository import ReportsRepository
from services.reports.app.csv_renderer import render_report_csv
from services.reports.app.csv_renderer import ReportCsvRenderingError
from services.reports.app.pdf_renderer import render_report_pdf
from services.reports.app.pdf_renderer import ReportPdfRenderingError
from services.reports.app.audit_package import ReportAuditPackageError
from services.reports.app.audit_package import render_audit_package_zip
from services.reports.app.excel_renderer import render_report_excel
from services.reports.app.excel_renderer import ReportExcelRenderingError
from services.forms.app.income_tax.report_generation import SUPPORTED_REPORT_BINDINGS
from services.forms.app.income_tax.report_version_binding import SUPPORTED_REPORT_VERSION_BINDINGS

HEALTH_CONTRIBUTION_REPORT_BINDINGS: dict[tuple[str, str], str] = {
    (
        "health_contribution_nhif_legacy_v1_2010_07_16",
        "HCH-VER-20100716-A",
    ): "Health Contribution Summary - NHIF Legacy (2010 Window)",
    (
        "health_contribution_nhif_legacy_v1_2015_04_01",
        "HCH-VER-20150401-A",
    ): "Health Contribution Summary - NHIF Legacy (2015 Window)",
    (
        "health_contribution_nhif_legacy_v1_2021_05_28",
        "HCH-VER-20210528-A",
    ): "Health Contribution Summary - NHIF Legacy (2021 Window)",
    (
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        "HCH-VER-20221231-REG",
    ): "Health Contribution Summary - NHIF Legacy (2022 Window)",
    (
        "health_contribution_sha_shif_v1_2024_10_01",
        "HCH-VER-20241001-A",
    ): "Health Contribution Summary - SHA/SHIF (2024 Window)",
    (
        "health_contribution_sha_shif_v1_2025_02_28_pit",
        "HCH-VER-20250228-PIT",
    ): "Health Contribution Summary - SHA/SHIF (2025 Window)",
}

HEALTH_CONTRIBUTION_REPORT_VERSION_BINDINGS: dict[tuple[str, str], dict[str, str]] = {
    (
        "health_contribution_nhif_legacy_v1_2010_07_16",
        "HCH-VER-20100716-A",
    ): {
        "report_version_id": "HCT-RPT-20100716-NHIF-V1",
    },
    (
        "health_contribution_nhif_legacy_v1_2015_04_01",
        "HCH-VER-20150401-A",
    ): {
        "report_version_id": "HCT-RPT-20150401-NHIF-V1",
    },
    (
        "health_contribution_nhif_legacy_v1_2021_05_28",
        "HCH-VER-20210528-A",
    ): {
        "report_version_id": "HCT-RPT-20210528-NHIF-V1",
    },
    (
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        "HCH-VER-20221231-REG",
    ): {
        "report_version_id": "HCT-RPT-20221231-NHIF-V1",
    },
    (
        "health_contribution_sha_shif_v1_2024_10_01",
        "HCH-VER-20241001-A",
    ): {
        "report_version_id": "HCT-RPT-20241001-SHA-V1",
    },
    (
        "health_contribution_sha_shif_v1_2025_02_28_pit",
        "HCH-VER-20250228-PIT",
    ): {
        "report_version_id": "HCT-RPT-20250228-SHA-V1",
    },
}


class ReportGenerationServiceError(RuntimeError):
    """Represent deterministic report-generation service failures."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        status_code: int,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self._context = context or {}

    def context(self) -> dict[str, object]:
        """Return stable structured error context payload."""

        return {"reason": self.reason_code, **self._context}


def generate_report_from_lineage_request(
    *,
    payload: object,
    tax_domain: str,
    repository: ReportsRepository,
    access_context: ReportAccessContext,
    audit_emitter: ReportsAuditEmitter | None = None,
    metrics_emitter: ReportsMetricsEmitter | None = None,
    correlation_id: str = "",
) -> ReportGenerationResponseModel:
    """Validate and generate deterministic report metadata from lineage references."""

    effective_metrics_emitter = metrics_emitter or get_default_reports_metrics_emitter()
    started_at = perf_counter()
    lineage_dimensions = _lineage_dimensions_from_payload(payload=payload)
    try:
        request_model = _parse_request(payload=payload, tax_domain=tax_domain)
        lineage_dimensions = {
            "supported_lane_id": request_model.supported_lane_id,
            "historical_version_id": request_model.historical_version_id,
        }
        lineage_reference = repository.resolve_finalized_lineage(
            computation_id=request_model.computation_id,
            form_id=request_model.form_id,
            historical_version_id=request_model.historical_version_id,
            supported_lane_id=request_model.supported_lane_id,
            tax_year=request_model.tax_year,
        )
        if lineage_reference is None:
            raise ReportGenerationServiceError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Finalized lineage reference does not exist.",
                status_code=404,
                context={
                    "computation_id": request_model.computation_id,
                    "form_id": request_model.form_id,
                    "historical_version_id": request_model.historical_version_id,
                    "supported_lane_id": request_model.supported_lane_id,
                    "tax_year": request_model.tax_year,
                },
            )
        if lineage_reference.tax_type != tax_domain:
            raise ReportGenerationServiceError(
                reason_code=INVALID_LINEAGE_REFERENCE,
                message="Finalized lineage tax domain does not match the requested report domain.",
                status_code=409,
                context={
                    "requested_tax_domain": tax_domain,
                    "lineage_tax_type": lineage_reference.tax_type,
                },
            )

        report_binding = _lookup_report_version_binding(
            tax_domain=tax_domain,
            supported_lane_id=request_model.supported_lane_id,
            historical_version_id=request_model.historical_version_id,
            tax_year=request_model.tax_year,
        )
        if report_binding is None:
            raise ReportGenerationServiceError(
                reason_code=REPORT_GENERATION_NOT_SUPPORTED,
                message="Report generation is not supported for the provided lineage context.",
                status_code=409,
                context={
                    "supported_lane_id": request_model.supported_lane_id,
                    "historical_version_id": request_model.historical_version_id,
                    "tax_year": request_model.tax_year,
                },
            )

        report_title = _lookup_report_title(
            tax_domain=tax_domain,
            supported_lane_id=request_model.supported_lane_id,
            historical_version_id=request_model.historical_version_id,
            tax_year=request_model.tax_year,
        )
        if report_title is None:
            raise ReportGenerationServiceError(
                reason_code=REPORT_GENERATION_NOT_SUPPORTED,
                message="Report generation is not supported for the provided lineage context.",
                status_code=409,
                context={
                    "supported_lane_id": request_model.supported_lane_id,
                    "historical_version_id": request_model.historical_version_id,
                    "tax_year": request_model.tax_year,
                },
            )

        report_version_id = str(report_binding["report_version_id"])
        report_id = _build_report_id(
            computation_id=request_model.computation_id,
            form_id=request_model.form_id,
            report_type=request_model.report_type,
            report_version_id=report_version_id,
            historical_version_id=request_model.historical_version_id,
            supported_lane_id=request_model.supported_lane_id,
            tax_year=request_model.tax_year,
            tax_type=tax_domain,
        )
        lineage = ReportLineageModel(
            computation_id=request_model.computation_id,
            form_id=request_model.form_id,
            report_id=report_id,
            report_version_id=report_version_id,
            historical_version_id=request_model.historical_version_id,
            supported_lane_id=request_model.supported_lane_id,
            tax_type=tax_domain,
            tax_year=request_model.tax_year,
            policy_anchor_ids=lineage_reference.policy_anchor_ids,
            source_anchor_ids=lineage_reference.source_anchor_ids,
        )
        response = ReportGenerationResponseModel(
            status="generated",
            report_id=report_id,
            report_type=request_model.report_type,
            tax_year=request_model.tax_year,
            report_version_id=report_version_id,
            lineage_reference=lineage,
        )
        artifact_kind = _artifact_kind_for_report_type(report_type=request_model.report_type)
        rendered_metadata = _render_output_metadata(
            request_model=request_model,
            response=response,
            artifact_kind=artifact_kind,
        )

        response = ReportGenerationResponseModel(
            status=response.status,
            report_id=response.report_id,
            report_type=response.report_type,
            tax_year=response.tax_year,
            report_version_id=response.report_version_id,
            lineage_reference=response.lineage_reference,
            artifact_metadata=rendered_metadata,
        )
        repository.persist_generated_report(report=response, access_context=access_context)
        if audit_emitter is not None:
            audit_emitter.append_event(
                event_type="report_generated",
                correlation_id=correlation_id or "reports-correlation-unknown",
                report_id=response.report_id,
                report_version_id=response.report_version_id,
                tenant_id=access_context.tenant_id,
                actor_id=access_context.owner_user_id,
                lineage=_lineage_payload_from_model(lineage=response.lineage_reference),
            )
        _emit_generation_success_metrics(
            metrics_emitter=effective_metrics_emitter,
            lineage_dimensions=lineage_dimensions,
        )
        return response
    except ReportGenerationServiceError as error:
        _emit_generation_failure_metrics(
            metrics_emitter=effective_metrics_emitter,
            reason_code=error.reason_code,
            lineage_dimensions=lineage_dimensions,
        )
        if audit_emitter is not None:
            audit_emitter.append_event(
                event_type="report_generation_failed",
                correlation_id=correlation_id or "reports-correlation-unknown",
                report_id=None,
                report_version_id=None,
                tenant_id=access_context.tenant_id,
                actor_id=access_context.owner_user_id,
                lineage=_lineage_payload_from_request_payload(
                    payload=payload,
                    tax_domain=tax_domain,
                ),
                error_code=error.reason_code,
                reason_code=error.reason_code,
                reason=error.reason_code,
            )
        raise
    finally:
        _emit_generation_latency_metrics(
            metrics_emitter=effective_metrics_emitter,
            started_at=started_at,
            lineage_dimensions=lineage_dimensions,
        )


def _parse_request(*, payload: object, tax_domain: str) -> ReportGenerationRequestModel:
    request_object = _as_object(payload, reason=INVALID_REPORT_REQUEST)
    required_string_fields = (
        "computation_id",
        "form_id",
        "report_type",
        "historical_version_id",
        "supported_lane_id",
    )
    required_int_fields = ("tax_year",)

    missing_fields: list[str] = []
    for field_name in required_string_fields:
        if (
            not isinstance(request_object.get(field_name), str)
            or not str(request_object[field_name]).strip()
        ):
            missing_fields.append(field_name)
    for field_name in required_int_fields:
        if not isinstance(request_object.get(field_name), int):
            missing_fields.append(field_name)
    if missing_fields:
        raise ReportGenerationServiceError(
            reason_code=INVALID_LINEAGE_REFERENCE,
            message="Report generation request is missing required lineage fields.",
            status_code=400,
            context={"missing_fields": sorted(set(missing_fields))},
        )

    report_type = cast(str, request_object["report_type"]).strip()
    if report_type not in _supported_report_types_for_domain(tax_domain=tax_domain):
        raise ReportGenerationServiceError(
            reason_code=REPORT_GENERATION_NOT_SUPPORTED,
            message="Requested report type is not supported for this baseline.",
            status_code=409,
            context={"report_type": report_type},
        )

    requested_format = request_object.get("format", "pdf")
    if not isinstance(requested_format, str) or requested_format.strip() == "":
        raise ReportGenerationServiceError(
            reason_code=INVALID_REPORT_REQUEST,
            message="Report generation request format is invalid.",
            status_code=400,
            context={"format": request_object.get("format")},
        )
    normalized_format = requested_format.strip().lower()
    if normalized_format not in {"pdf", "xlsx", "csv", "zip"}:
        raise ReportGenerationServiceError(
            reason_code=REPORT_GENERATION_NOT_SUPPORTED,
            message="Requested report output format is not supported.",
            status_code=409,
            context={"format": normalized_format},
        )

    request_model = ReportGenerationRequestModel(
        computation_id=cast(str, request_object["computation_id"]).strip(),
        form_id=cast(str, request_object["form_id"]).strip(),
        report_type=report_type,
        tax_year=cast(int, request_object["tax_year"]),
        historical_version_id=cast(str, request_object["historical_version_id"]).strip(),
        supported_lane_id=cast(str, request_object["supported_lane_id"]).strip(),
        output_format=normalized_format,
    )
    if request_model.tax_year < 2000 or request_model.tax_year > 2100:
        raise ReportGenerationServiceError(
            reason_code=INVALID_LINEAGE_REFERENCE,
            message="Report generation request tax year is outside supported bounds.",
            status_code=400,
            context={"tax_year": request_model.tax_year},
        )
    if request_model.historical_version_id == "":
        raise ReportGenerationServiceError(
            reason_code=INVALID_LINEAGE_REFERENCE,
            message="Report generation request has invalid lineage identifiers.",
            status_code=400,
        )
    return request_model


def _as_object(
    value: object,
    *,
    reason: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReportGenerationServiceError(
            reason_code=reason,
            message="Report generation request payload must be a JSON object.",
            status_code=400,
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _build_report_id(
    *,
    computation_id: str,
    form_id: str,
    report_type: str,
    report_version_id: str,
    historical_version_id: str,
    supported_lane_id: str,
    tax_year: int,
    tax_type: str,
) -> str:
    identity_payload = {
        "computation_id": computation_id,
        "form_id": form_id,
        "report_type": report_type,
        "report_version_id": report_version_id,
        "historical_version_id": historical_version_id,
        "supported_lane_id": supported_lane_id,
        "tax_year": tax_year,
        "tax_type": tax_type,
    }
    encoded_payload = canonical_json_dumps(identity_payload).encode("utf-8")
    digest = hashlib.sha256(encoded_payload).hexdigest()
    return str(uuid5(NAMESPACE_URL, digest))


def _artifact_kind_for_report_type(*, report_type: str) -> str:
    if report_type in {"income_tax_summary", "health_contribution_summary"}:
        return "tax_summary"
    if report_type == "income_tax_worksheet":
        return "worksheet"
    if report_type == "income_tax_audit_package_manifest":
        return "audit_package"
    return "unknown"


def _render_output_metadata(
    *,
    request_model: ReportGenerationRequestModel,
    response: ReportGenerationResponseModel,
    artifact_kind: str,
) -> ReportArtifactMetadataModel:
    try:
        if request_model.output_format == "xlsx":
            return render_report_excel(
                report_id=response.report_id,
                report_version_id=response.report_version_id,
                artifact_kind=artifact_kind,
                report_type=response.report_type,
                tax_year=response.tax_year,
                lineage=response.lineage_reference,
            )
        if request_model.output_format == "csv":
            return render_report_csv(
                report_id=response.report_id,
                report_version_id=response.report_version_id,
                artifact_kind=artifact_kind,
                report_type=response.report_type,
                tax_year=response.tax_year,
                lineage=response.lineage_reference,
            )
        if request_model.output_format == "zip":
            return render_audit_package_zip(
                report_id=response.report_id,
                report_version_id=response.report_version_id,
                artifact_kind=artifact_kind,
                report_type=response.report_type,
                tax_year=response.tax_year,
                lineage=response.lineage_reference,
            )
        return render_report_pdf(
            report_id=response.report_id,
            report_version_id=response.report_version_id,
            artifact_kind=artifact_kind,
            report_type=response.report_type,
            tax_year=response.tax_year,
            lineage=response.lineage_reference,
        )
    except (
        ReportPdfRenderingError,
        ReportExcelRenderingError,
        ReportCsvRenderingError,
        ReportAuditPackageError,
    ) as error:
        status_code = 409 if error.reason_code == REPORT_GENERATION_NOT_SUPPORTED else 503
        raise ReportGenerationServiceError(
            reason_code=(
                REPORT_RENDERING_FAILED
                if error.reason_code == REPORT_RENDERING_FAILED
                else REPORT_PACKAGING_FAILED
                if error.reason_code == REPORT_PACKAGING_FAILED
                else error.reason_code
            ),
            message=error.message,
            status_code=status_code,
            context={
                "report_id": response.report_id,
                "report_version_id": response.report_version_id,
                "artifact_kind": artifact_kind,
                "format": request_model.output_format,
            },
        ) from error


def _lineage_payload_from_model(*, lineage: ReportLineageModel) -> dict[str, object]:
    return {
        "computation_id": lineage.computation_id,
        "form_id": lineage.form_id,
        "historical_version_id": lineage.historical_version_id,
        "supported_lane_id": lineage.supported_lane_id,
        "tax_type": lineage.tax_type,
        "tax_year": lineage.tax_year,
    }


def _lineage_payload_from_request_payload(
    *,
    payload: object,
    tax_domain: str,
) -> dict[str, object]:
    if isinstance(payload, Mapping):
        source: Mapping[object, object] = cast(Mapping[object, object], payload)
    else:
        source = {}
    return {
        "computation_id": source.get("computation_id"),
        "form_id": source.get("form_id"),
        "historical_version_id": source.get("historical_version_id"),
        "supported_lane_id": source.get("supported_lane_id"),
        "tax_type": tax_domain,
        "tax_year": source.get("tax_year"),
    }


def _supported_report_types_for_domain(*, tax_domain: str) -> set[str]:
    if tax_domain == "income_tax":
        return {
            "income_tax_summary",
            "income_tax_worksheet",
            "income_tax_audit_package_manifest",
        }
    if tax_domain == "health_contribution":
        return {"health_contribution_summary"}
    return set()


def _lookup_report_version_binding(
    *,
    tax_domain: str,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> dict[str, str] | None:
    if tax_domain == "income_tax":
        return SUPPORTED_REPORT_VERSION_BINDINGS.get(
            (supported_lane_id, historical_version_id, tax_year)
        )
    if tax_domain == "health_contribution":
        return HEALTH_CONTRIBUTION_REPORT_VERSION_BINDINGS.get(
            (supported_lane_id, historical_version_id)
        )
    return None


def _lookup_report_title(
    *,
    tax_domain: str,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> str | None:
    if tax_domain == "income_tax":
        return SUPPORTED_REPORT_BINDINGS.get((supported_lane_id, historical_version_id, tax_year))
    if tax_domain == "health_contribution":
        return HEALTH_CONTRIBUTION_REPORT_BINDINGS.get((supported_lane_id, historical_version_id))
    return None


def _lineage_dimensions_from_payload(*, payload: object) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        return {}
    source = cast(Mapping[object, object], payload)
    supported_lane_id = source.get("supported_lane_id")
    historical_version_id = source.get("historical_version_id")
    dimensions: dict[str, str] = {}
    if isinstance(supported_lane_id, str) and supported_lane_id.strip():
        dimensions["supported_lane_id"] = supported_lane_id.strip()
    if isinstance(historical_version_id, str) and historical_version_id.strip():
        dimensions["historical_version_id"] = historical_version_id.strip()
    return dimensions


def _emit_generation_success_metrics(
    *,
    metrics_emitter: ReportsMetricsEmitter | None,
    lineage_dimensions: dict[str, str],
) -> None:
    if metrics_emitter is None:
        return
    dimensions = {"status": "success", **lineage_dimensions}
    metrics_emitter.increment_counter_non_blocking(
        REPORTS_GENERATION_TOTAL,
        dimensions=dimensions,
    )


def _emit_generation_failure_metrics(
    *,
    metrics_emitter: ReportsMetricsEmitter | None,
    reason_code: str,
    lineage_dimensions: dict[str, str],
) -> None:
    if metrics_emitter is None:
        return
    total_dimensions = {"status": "failure", **lineage_dimensions}
    metrics_emitter.increment_counter_non_blocking(
        REPORTS_GENERATION_TOTAL,
        dimensions=total_dimensions,
    )
    failure_dimensions = {"reason_code": reason_code, **lineage_dimensions}
    metrics_emitter.increment_counter_non_blocking(
        REPORTS_GENERATION_FAILURES_TOTAL,
        dimensions=failure_dimensions,
    )


def _emit_generation_latency_metrics(
    *,
    metrics_emitter: ReportsMetricsEmitter | None,
    started_at: float,
    lineage_dimensions: dict[str, str],
) -> None:
    if metrics_emitter is None:
        return
    duration_ms = round((perf_counter() - started_at) * 1000, 3)
    metrics_emitter.observe_histogram_non_blocking(
        REPORTS_GENERATION_LATENCY_MS,
        value=duration_ms,
        dimensions=lineage_dimensions,
    )
