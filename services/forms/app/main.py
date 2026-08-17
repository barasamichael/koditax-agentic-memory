"""Forms service runtime boundary baseline for Phase 10.1."""

import json
from pathlib import Path as PathlibPath
from time import perf_counter
from typing import Any
from typing import cast
from typing import NoReturn
from collections.abc import Mapping

from fastapi import Body
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from starlette.types import ExceptionHandler
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from services.forms.app.errors import FORMS_REASON_CODES
from services.forms.app.errors import FORMS_REQUEST_INVALID
from services.forms.app.errors import create_forms_http_error
from services.forms.app.errors import FORMS_CONTRACT_VIOLATION
from services.forms.app.errors import FORMS_SCOPE_NOT_SUPPORTED
from services.forms.app.errors import build_forms_error_envelope
from services.forms.app.errors import FORMS_OPERATION_NOT_IMPLEMENTED
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.forms.app.audit_events import build_forms_audit_event_id
from services.forms.app.audit_events import emit_forms_audit_log_event
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_ACCESS_DENIED
from services.forms.app.audit_events import get_forms_audit_event_timestamp
from services.forms.app.audit_events import build_forms_audit_evidence_envelope
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_VALIDATION_EXECUTED
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED
from services.forms.app.history_store import FormsHistoryStoreError
from services.forms.app.history_store import set_form_artifact_download_expiry
from services.forms.app.history_store import get_form_artifact_storage_metadata
from services.forms.app.history_store import get_form_artifact_retention_metadata
from services.forms.app.history_store import persist_form_artifact_history_record
from services.forms.app.history_store import get_form_artifact_history_record_by_identity
from services.forms.app.history_store import list_form_artifact_history_records_by_filter
from services.forms.app.observability import MetricEvent
from services.forms.app.observability import FormsSloAlert
from services.forms.app.observability import FormsMetricsEmitter
from services.forms.app.observability import FormsSloMetricSnapshot
from services.forms.app.observability import FormsSloThresholdPolicy
from services.forms.app.observability import FORMS_GENERATION_LATENCY_MS
from services.forms.app.observability import evaluate_forms_slo_thresholds
from services.forms.app.observability import FORMS_GENERATION_FAILURE_TOTAL
from services.forms.app.observability import FORMS_GENERATION_SUCCESS_TOTAL
from services.forms.app.observability import get_default_forms_metrics_emitter
from services.forms.app.observability import FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL
from services.forms.app.observability import FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS
from services.forms.app.observability import FORMS_DOWNLOAD_ISSUANCE_FAILURE_TOTAL
from services.forms.app.observability import FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL
from services.forms.app.observability import get_default_forms_slo_threshold_policy
from services.forms.app.download_links import FormsDownloadLinkIssuanceError
from services.forms.app.download_links import issue_forms_artifact_download_token
from services.forms.app.pre_population import build_pre_population_source_fields
from services.forms.app.pre_population import build_pre_population_field_suggestions
from services.forms.app.batch_generation import build_forms_batch_id
from services.forms.app.batch_generation import build_forms_batch_summary
from services.forms.app.batch_generation import build_canonical_batch_item_error
from services.forms.app.batch_generation import extract_batch_item_error_from_http_exception_detail
from services.forms.app.retention_policy import FormsRetentionPolicyError
from services.forms.app.retention_policy import evaluate_forms_download_access
from services.forms.app.retention_policy import get_forms_retention_reference_time
from services.forms.app.retention_policy import build_forms_artifact_retention_metadata
from services.forms.app.retention_policy import evaluate_forms_artifact_retention_access
from services.forms.app.storage_integration import FormsStorageIntegrationError
from services.forms.app.storage_integration import persist_form_artifact_in_governed_storage
from services.forms.app.submission_checklist import build_submission_checklist
from services.validation.app.validation_rules import evaluate_forms_workflow_validation
from services.forms.app.income_tax.form_mapping import IncomeTaxFormMappingError
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_validation import validate_income_tax_pre_generation_context
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_version_binding import IncomeTaxFormVersionBindingError
from services.forms.app.health_contribution.form_mapping import HealthContributionFormMappingError
from services.forms.app.health_contribution.form_mapping import (
    map_finalized_health_contribution_output_to_form_ready,
)
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.form_artifact_generation import (
    IncomeTaxFormArtifactGenerationError,
)

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

ROUTER = APIRouter()
REQUEST_BODY_OPTIONAL = Body(None)
FORMS_MAPPING_INPUT_NOT_FINALIZED = "forms_mapping_input_not_finalized"
FORMS_VERSION_NOT_SUPPORTED = "forms_version_not_supported"
FORMS_VERSION_BINDING_AMBIGUOUS = "forms_version_binding_ambiguous"
FORMS_GENERATION_PRECONDITION_MISSING = "forms_generation_precondition_missing"
FORMS_ARTIFACT_GENERATION_FAILED = "forms_artifact_generation_failed"
FORMS_AUDIT_EVIDENCE_MISSING = "forms_audit_evidence_missing"
FORMS_VALIDATION_CONTRACT_VIOLATION = "forms_validation_contract_violation"
FORMS_GENERATION_BLOCKED_BY_VALIDATION = "forms_generation_blocked_by_validation"
FORMS_HISTORY_PERSISTENCE_FAILED = "forms_history_persistence_failed"
FORMS_HISTORY_NOT_FOUND = "forms_history_not_found"
FORMS_UNAUTHORIZED_ACCESS = "forms_unauthorized_access"
FORMS_STORAGE_WRITE_FAILED = "forms_storage_write_failed"
FORMS_STORAGE_REFERENCE_MISSING = "forms_storage_reference_missing"
FORMS_DOWNLOAD_NOT_AUTHORIZED = "forms_download_not_authorized"
FORMS_DOWNLOAD_ARTIFACT_NOT_FOUND = "forms_download_artifact_not_found"
FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED = "forms_download_link_issuance_failed"
FORMS_DOWNLOAD_LINK_EXPIRED = "forms_download_link_expired"
FORMS_ARTIFACT_RETENTION_EXPIRED = "forms_artifact_retention_expired"
FORMS_ARTIFACT_ACCESS_RESTRICTED = "forms_artifact_access_restricted"
FORMS_PRE_POPULATION_SOURCE_NOT_FOUND = "forms_pre_population_source_not_found"
FORMS_PRE_POPULATION_SCOPE_NOT_SUPPORTED = "forms_pre_population_scope_not_supported"
FORMS_PRE_POPULATION_NOT_AUTHORIZED = "forms_pre_population_not_authorized"
FORMS_SUBMISSION_CHECKLIST_NOT_AUTHORIZED = "forms_submission_checklist_not_authorized"
FORMS_SUBMISSION_CHECKLIST_SCOPE_NOT_SUPPORTED = "forms_submission_checklist_scope_not_supported"
FORMS_SUBMISSION_CHECKLIST_SOURCE_MISSING = "forms_submission_checklist_source_missing"
FORMS_TEMPLATE_CAPABILITY_DISABLED = "forms_template_capability_disabled"
INVALID_TAX_DOMAIN = "invalid_tax_domain"
UNSUPPORTED_TAX_DOMAIN_PATH = "unsupported_tax_domain_path"
UNIMPLEMENTED_TAX_DOMAIN_MAPPING = "unimplemented_tax_domain_mapping"
FORMS_GENERATION_ENDPOINT_PATH = "/v1/forms/income-tax/artifacts"
FORMS_DOWNLOAD_ISSUANCE_SUFFIX = "/download-links"
FORMS_DOWNLOAD_DENIAL_REASONS = frozenset(
    {
        FORMS_DOWNLOAD_NOT_AUTHORIZED,
        FORMS_DOWNLOAD_LINK_EXPIRED,
        FORMS_ARTIFACT_RETENTION_EXPIRED,
        FORMS_ARTIFACT_ACCESS_RESTRICTED,
    }
)
RECOGNIZED_FORMS_TAX_DOMAINS: dict[str, str] = {
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
def forms_health_status(request: Request) -> dict[str, object]:
    """Expose deterministic forms-service health endpoint."""

    return {
        "status": "ok",
        "service": "forms",
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/income-tax/mappings")
def map_income_tax_form_output_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> Any:
    """Map finalized deterministic income-tax output to form-ready structure."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("finalized_output",),
    )
    _enforce_disabled_template_capability_guard(request=request, payload=typed_payload)
    finalized_output = typed_payload.get("finalized_output")
    if not isinstance(finalized_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "finalized_output", "reason": "must_be_object"},
        )

    try:
        mapping_output = map_finalized_income_tax_output_to_form_ready(
            cast(dict[str, object], finalized_output)
        )
    except IncomeTaxFormMappingError as error:
        status_code, reason = _map_income_tax_mapping_error_reason(error.reason)
        message = (
            "Forms mapping requires finalized computation context."
            if reason == FORMS_MAPPING_INPUT_NOT_FINALIZED
            else error.message
        )
        raise create_forms_http_error(
            request=request,
            status_code=status_code,
            error_code=reason,
            message=message,
            reason=reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    governed_validation = evaluate_forms_workflow_validation(
        tax_domain="income_tax",
        finalized_output=cast(dict[str, object], finalized_output),
    ).to_dict()
    if governed_validation["validation_status"] != "accepted":
        return JSONResponse(
            status_code=409,
            content=_build_governed_validation_block_response(
                request=request,
                governed_validation=governed_validation,
            ),
        )

    mapping_status = str(mapping_output.get("mapping_status", "ok"))
    form_type = str(mapping_output.get("form_type", "income_tax_return"))
    form_version = str(mapping_output.get("form_version", "income_tax_vertical_slice_v1"))
    mapping_output_map = mapping_output
    computation_identity = mapping_output_map.get("computation_identity")
    version_identity = mapping_output_map.get("version_identity")
    lineage_reference: dict[str, object] = {}
    event_timestamp = ""
    if isinstance(computation_identity, dict):
        computation_identity_map = cast(dict[str, object], computation_identity)
        raw_event_timestamp = computation_identity_map.get("finalized_at")
        if isinstance(raw_event_timestamp, str):
            event_timestamp = raw_event_timestamp
        lineage_reference["computation_id"] = computation_identity_map.get("computation_id")
        lineage_reference["input_hash"] = computation_identity_map.get("input_hash")
        lineage_reference["tax_year"] = computation_identity_map.get("tax_year")
    if isinstance(version_identity, dict):
        version_identity_map = cast(dict[str, object], version_identity)
        lineage_reference["historical_version_id"] = version_identity_map.get(
            "historical_version_id"
        )
    lineage_reference["supported_lane_id"] = mapping_output_map.get("supported_lane_id")
    lineage_reference["form_type"] = form_type
    lineage_reference["form_version"] = form_version
    audit_evidence = _build_generation_path_audit_evidence(
        request=request,
        event_type="forms_mapping_completed",
        event_timestamp=event_timestamp,
        lineage_reference=lineage_reference,
        pipeline_output=mapping_output,
    )
    return {
        "status": "ok",
        "mapping_status": mapping_status,
        "form_type": form_type,
        "form_version": form_version,
        "mapping_output": mapping_output,
        "audit_evidence": audit_evidence,
        "governed_validation": governed_validation,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/health-contribution/mappings")
def map_health_contribution_form_output_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> Any:
    """Map finalized deterministic health-contribution output to form-ready structure."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("finalized_output",),
    )
    finalized_output = typed_payload.get("finalized_output")
    if not isinstance(finalized_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "finalized_output", "reason": "must_be_object"},
        )

    try:
        mapping_output = map_finalized_health_contribution_output_to_form_ready(
            cast(dict[str, object], finalized_output)
        )
    except HealthContributionFormMappingError as error:
        status_code, reason = _map_health_contribution_mapping_error_reason(error.reason)
        message = (
            "Forms mapping requires finalized computation context."
            if reason == FORMS_MAPPING_INPUT_NOT_FINALIZED
            else error.message
        )
        raise create_forms_http_error(
            request=request,
            status_code=status_code,
            error_code=reason,
            message=message,
            reason=reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    governed_validation = evaluate_forms_workflow_validation(
        tax_domain="health_contribution",
        finalized_output=cast(dict[str, object], finalized_output),
    ).to_dict()
    if governed_validation["validation_status"] != "accepted":
        return JSONResponse(
            status_code=409,
            content=_build_governed_validation_block_response(
                request=request,
                governed_validation=governed_validation,
            ),
        )

    mapping_status = str(mapping_output.get("mapping_status", "ok"))
    form_type = str(mapping_output.get("form_type", "health_contribution_summary"))
    form_version = str(mapping_output.get("form_version", "health_contribution_vertical_slice_v1"))
    mapping_output_map = mapping_output
    computation_identity = mapping_output_map.get("computation_identity")
    version_identity = mapping_output_map.get("version_identity")
    lineage_reference: dict[str, object] = {}
    event_timestamp = ""
    if isinstance(computation_identity, dict):
        computation_identity_map = cast(dict[str, object], computation_identity)
        raw_event_timestamp = computation_identity_map.get("finalized_at")
        if isinstance(raw_event_timestamp, str):
            event_timestamp = raw_event_timestamp
        lineage_reference["computation_id"] = computation_identity_map.get("computation_id")
        lineage_reference["input_hash"] = computation_identity_map.get("input_hash")
        lineage_reference["tax_year"] = computation_identity_map.get("tax_year")
    if isinstance(version_identity, dict):
        version_identity_map = cast(dict[str, object], version_identity)
        lineage_reference["historical_version_id"] = version_identity_map.get(
            "historical_version_id"
        )
    lineage_reference["supported_lane_id"] = mapping_output_map.get("supported_lane_id")
    lineage_reference["form_type"] = form_type
    lineage_reference["form_version"] = form_version
    audit_evidence = _build_generation_path_audit_evidence(
        request=request,
        event_type="forms_mapping_completed",
        event_timestamp=event_timestamp,
        lineage_reference=lineage_reference,
        pipeline_output=mapping_output,
    )
    return {
        "status": "ok",
        "mapping_status": mapping_status,
        "form_type": form_type,
        "form_version": form_version,
        "mapping_output": mapping_output,
        "audit_evidence": audit_evidence,
        "governed_validation": governed_validation,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/income-tax/version-bindings")
def bind_income_tax_form_version_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Bind mapped deterministic form-ready output to a governed form version."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("mapped_output",),
    )
    _enforce_disabled_template_capability_guard(request=request, payload=typed_payload)
    mapped_output = typed_payload.get("mapped_output")
    if not isinstance(mapped_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "mapped_output", "reason": "must_be_object"},
        )

    try:
        binding_output = bind_income_tax_form_version(cast(dict[str, object], mapped_output))
    except IncomeTaxFormVersionBindingError as error:
        status_code, reason = _map_income_tax_version_binding_error_reason(error.reason)
        raise create_forms_http_error(
            request=request,
            status_code=status_code,
            error_code=reason,
            message=error.message,
            reason=reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    binding_output_map = binding_output
    binding_lineage = binding_output_map.get("binding_lineage")
    effective_start = ""
    effective_end: str | None = None
    binding_lineage_map: dict[str, object] | None = None
    if isinstance(binding_lineage, dict):
        binding_lineage_map = cast(dict[str, object], binding_lineage)
        raw_effective_start = binding_lineage_map.get("effective_start")
        raw_effective_end = binding_lineage_map.get("effective_end")
        if isinstance(raw_effective_start, str):
            effective_start = raw_effective_start
        if isinstance(raw_effective_end, str):
            effective_end = None if raw_effective_end == "9999-12-31" else raw_effective_end

    mapped_output_map = cast(dict[str, object], mapped_output)
    form_type = str(binding_output_map.get("form_type", "income_tax_return"))
    form_version = str(mapped_output_map.get("form_version", "income_tax_vertical_slice_v1"))
    form_version_id = str(binding_output_map.get("form_version_id", ""))
    form_template_id = str(binding_output_map.get("template_id", ""))
    historical_version_id = str(binding_output_map.get("historical_version_id", ""))
    mapped_computation_identity = mapped_output_map.get("computation_identity")
    binding_lineage_reference: dict[str, object] = {}
    event_timestamp = ""
    if isinstance(mapped_computation_identity, dict):
        mapped_computation_identity_map = cast(dict[str, object], mapped_computation_identity)
        raw_event_timestamp = mapped_computation_identity_map.get("finalized_at")
        if isinstance(raw_event_timestamp, str):
            event_timestamp = raw_event_timestamp
    if binding_lineage_map is not None:
        binding_lineage_reference["computation_id"] = binding_lineage_map.get("computation_id")
        binding_lineage_reference["input_hash"] = binding_lineage_map.get("input_hash")
    binding_lineage_reference["supported_lane_id"] = binding_output_map.get("supported_lane_id")
    binding_lineage_reference["historical_version_id"] = binding_output_map.get(
        "historical_version_id"
    )
    binding_lineage_reference["form_version_id"] = binding_output_map.get("form_version_id")
    binding_lineage_reference["form_type"] = form_type
    binding_lineage_reference["tax_year"] = binding_output_map.get("tax_year")
    audit_evidence = _build_generation_path_audit_evidence(
        request=request,
        event_type="forms_version_binding_completed",
        event_timestamp=event_timestamp,
        lineage_reference=binding_lineage_reference,
        pipeline_output=binding_output,
    )
    return {
        "status": "ok",
        "binding_status": str(binding_output.get("binding_status", "bound")),
        "form_type": form_type,
        "form_version": form_version,
        "form_version_id": form_version_id,
        "form_template_id": form_template_id,
        "historical_version_id": historical_version_id,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "binding_output": binding_output,
        "audit_evidence": audit_evidence,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/income-tax/validations")
def validate_income_tax_form_generation_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> Any:
    """Validate mapped and version-bound income-tax context before generation."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("form_ready_output", "form_version_binding"),
        missing_field_reason=FORMS_REQUEST_INVALID,
    )
    _enforce_disabled_template_capability_guard(request=request, payload=typed_payload)
    form_ready_output = typed_payload.get("form_ready_output")
    if not isinstance(form_ready_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_ready_output", "reason": "must_be_object"},
        )
    form_version_binding = typed_payload.get("form_version_binding")
    if not isinstance(form_version_binding, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_version_binding", "reason": "must_be_object"},
        )

    form_ready_output_map = cast(dict[str, object], form_ready_output)
    form_version_binding_map = cast(dict[str, object], form_version_binding)
    validation_result = validate_income_tax_pre_generation_context(
        form_ready_output=form_ready_output_map,
        form_version_binding=form_version_binding_map,
    )

    validation_lineage: dict[str, object] = {
        "form_type": "income_tax_return",
        "tax_year": form_version_binding_map.get("tax_year"),
        "historical_version_id": form_version_binding_map.get("historical_version_id"),
        "validation_status": validation_result.get("validation_status"),
        "is_valid": validation_result.get("is_valid"),
    }
    emit_forms_audit_log_event(
        {
            "audit_event_id": build_forms_audit_event_id(
                {
                    "event_type": FORMS_AUDIT_EVENT_VALIDATION_EXECUTED,
                    "trace_id": get_trace_id(request),
                    "correlation_id": get_correlation_id(request),
                    "lineage_reference": validation_lineage,
                }
            ),
            "event_type": FORMS_AUDIT_EVENT_VALIDATION_EXECUTED,
            "event_timestamp": get_forms_audit_event_timestamp(),
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
            "lineage_reference": validation_lineage,
            "actor_context": {
                "actor_type": "user",
                "user_id": _resolve_history_user_id(request=request),
            },
        }
    )
    return {
        "status": "ok",
        "validation_status": validation_result["validation_status"],
        "is_valid": validation_result["is_valid"],
        "findings": validation_result["findings"],
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/income-tax/artifacts", status_code=201)
def generate_income_tax_form_artifact_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> Any:
    """Generate immutable form artifact from finalized mapped and bound income-tax context."""

    _mark_forms_generation_started(request=request)
    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("finalized_output", "form_ready_output", "form_version_binding"),
        missing_field_reason=FORMS_GENERATION_PRECONDITION_MISSING,
    )
    _enforce_disabled_template_capability_guard(request=request, payload=typed_payload)
    finalized_output = typed_payload.get("finalized_output")
    form_ready_output = typed_payload.get("form_ready_output")
    form_version_binding = typed_payload.get("form_version_binding")
    if not isinstance(finalized_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "finalized_output", "reason": "must_be_object"},
        )
    if not isinstance(form_ready_output, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_ready_output", "reason": "must_be_object"},
        )
    if not isinstance(form_version_binding, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_version_binding", "reason": "must_be_object"},
        )

    form_ready_output_map = cast(dict[str, object], form_ready_output)
    form_version_binding_map = cast(dict[str, object], form_version_binding)
    governed_validation = evaluate_forms_workflow_validation(
        tax_domain="income_tax",
        finalized_output=cast(dict[str, object], finalized_output),
    ).to_dict()
    if governed_validation["validation_status"] != "accepted":
        _emit_forms_generation_failure(
            request=request,
            reason_code=FORMS_GENERATION_BLOCKED_BY_VALIDATION,
        )
        _emit_forms_generation_latency(request=request)
        return JSONResponse(
            status_code=409,
            content=_build_governed_validation_block_response(
                request=request,
                governed_validation=governed_validation,
            ),
        )
    validation_result = validate_income_tax_pre_generation_context(
        form_ready_output=form_ready_output_map,
        form_version_binding=form_version_binding_map,
    )
    if not validation_result["is_valid"]:
        _emit_forms_generation_failure(
            request=request,
            reason_code=FORMS_GENERATION_BLOCKED_BY_VALIDATION,
        )
        _emit_forms_generation_latency(request=request)
        return JSONResponse(
            status_code=409,
            content=_build_generation_block_response(
                request=request,
                validation_result=validation_result,
            ),
        )

    try:
        artifact_output = generate_income_tax_form_artifact(
            finalized_output=cast(dict[str, object], finalized_output),
            form_ready_output=form_ready_output_map,
            form_version_binding=form_version_binding_map,
        )
    except IncomeTaxFormArtifactGenerationError as error:
        status_code, reason = _map_income_tax_artifact_generation_error_reason(error.reason)
        raise create_forms_http_error(
            request=request,
            status_code=status_code,
            error_code=reason,
            message=error.message,
            reason=reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    artifact_output_map = artifact_output
    lineage = artifact_output_map.get("lineage")
    lineage_reference: dict[str, object] = {}
    created_at = ""
    if isinstance(lineage, dict):
        lineage_map = cast(dict[str, object], lineage)
        raw_created_at = lineage_map.get("finalized_at")
        if isinstance(raw_created_at, str):
            created_at = raw_created_at
        lineage_reference = {
            "computation_id": artifact_output_map.get("computation_id"),
            "input_hash": lineage_map.get("input_hash"),
            "supported_lane_id": artifact_output_map.get("supported_lane_id"),
            "historical_version_id": artifact_output_map.get("historical_version_id"),
            "form_version_id": artifact_output_map.get("form_version_id"),
            "finalized_audit_event_id": lineage_map.get("finalized_audit_event_id"),
        }
    lineage_reference["artifact_id"] = artifact_output_map.get("artifact_id")
    lineage_reference["artifact_hash"] = artifact_output_map.get("content_sha256")
    lineage_reference["form_type"] = artifact_output_map.get("form_type")
    lineage_reference["tax_year"] = artifact_output_map.get("tax_year")
    audit_evidence = _build_generation_path_audit_evidence(
        request=request,
        event_type="forms_artifact_generated",
        event_timestamp=created_at,
        lineage_reference=lineage_reference,
        pipeline_output=artifact_output,
    )
    try:
        retention_metadata = build_forms_artifact_retention_metadata(created_at=created_at)
    except FormsRetentionPolicyError as error:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_CONTRACT_VIOLATION,
            message="Forms artifact retention metadata violates contract requirements.",
            reason=FORMS_CONTRACT_VIOLATION,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error
    artifact_response = {
        "status": "ok",
        "generation_status": str(artifact_output_map.get("generation_status", "generated")),
        "artifact_id": str(artifact_output_map.get("artifact_id", "")),
        "artifact_hash": str(artifact_output_map.get("content_sha256", "")),
        "artifact_type": str(artifact_output_map.get("artifact_type", "income_tax_form_artifact")),
        "form_type": str(artifact_output_map.get("form_type", "income_tax_return")),
        "form_version_id": str(artifact_output_map.get("form_version_id", "")),
        "tax_year": artifact_output_map.get("tax_year"),
        "historical_version_id": str(artifact_output_map.get("historical_version_id", "")),
        "lineage_reference": lineage_reference,
        "created_at": created_at,
        "generated_at": created_at,
        "immutability_status": "immutable",
        "immutable": True,
        "artifact_output": artifact_output,
        "audit_evidence": audit_evidence,
        "governed_validation": governed_validation,
        "retention_metadata": retention_metadata,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }
    generated_content_payload = artifact_output.get("generated_content_payload")
    if not isinstance(generated_content_payload, dict):
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_STORAGE_REFERENCE_MISSING,
            message="Forms storage reference metadata is missing.",
            reason=FORMS_STORAGE_REFERENCE_MISSING,
            details={"field": "generated_content_payload", "constraint": "object"},
        )
    generated_content_payload_map = cast(dict[str, object], generated_content_payload)
    try:
        storage_metadata = persist_form_artifact_in_governed_storage(
            artifact_id=str(artifact_response["artifact_id"]),
            artifact_hash=str(artifact_response["artifact_hash"]),
            form_type=str(artifact_response["form_type"]),
            artifact_payload=generated_content_payload_map,
        )
    except FormsStorageIntegrationError as error:
        mapped_reason = (
            error.reason
            if error.reason
            in {
                FORMS_STORAGE_WRITE_FAILED,
                FORMS_STORAGE_REFERENCE_MISSING,
                FORMS_SCOPE_NOT_SUPPORTED,
            }
            else FORMS_STORAGE_WRITE_FAILED
        )
        mapped_status_code = 409 if mapped_reason == FORMS_SCOPE_NOT_SUPPORTED else 500
        raise create_forms_http_error(
            request=request,
            status_code=mapped_status_code,
            error_code=mapped_reason,
            message=error.message,
            reason=mapped_reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error
    artifact_response["storage_metadata"] = storage_metadata
    history_record = _build_form_history_record_from_artifact_response(
        artifact_response,
        request=request,
    )
    try:
        persist_form_artifact_history_record(history_record)
    except FormsHistoryStoreError as error:
        reason = (
            FORMS_CONTRACT_VIOLATION
            if error.reason == FORMS_CONTRACT_VIOLATION
            else FORMS_HISTORY_PERSISTENCE_FAILED
        )
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=reason,
            message=(
                "Forms artifact generated but history persistence failed."
                if reason == FORMS_HISTORY_PERSISTENCE_FAILED
                else "Forms history record violates persistence contract."
            ),
            reason=reason,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    history_record_map: dict[str, object] = history_record
    history_lineage_raw = history_record_map.get("lineage_reference")
    if isinstance(history_lineage_raw, dict):
        history_lineage_map = cast(dict[str, object], history_lineage_raw)
    else:
        history_lineage_map = {}
    emit_forms_audit_log_event(
        {
            "audit_event_id": build_forms_audit_event_id(
                {
                    "event_type": FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED,
                    "trace_id": get_trace_id(request),
                    "correlation_id": get_correlation_id(request),
                    "lineage_reference": history_lineage_map,
                }
            ),
            "event_type": FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED,
            "event_timestamp": str(artifact_response["created_at"]),
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
            "lineage_reference": history_lineage_map,
            "actor_context": {
                "actor_type": "user",
                "user_id": str(history_record_map.get("user_id", "anonymous_user")),
            },
        }
    )

    _emit_forms_generation_success(request=request)
    _emit_forms_generation_latency(request=request)
    return artifact_response


@ROUTER.post("/v1/forms/income-tax/artifacts/batch")
def generate_income_tax_form_artifact_batch_baseline(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Generate multiple deterministic income-tax form artifacts in stable input order."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("items",),
    )
    batch_items = typed_payload.get("items")
    if not isinstance(batch_items, list):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "items", "constraint": "array"},
        )
    batch_items_list = cast(list[object], batch_items)
    if not batch_items_list:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "items", "constraint": "non_empty_array"},
        )

    normalized_batch_items: list[dict[str, object]] = []
    for raw_item in batch_items_list:
        if isinstance(raw_item, dict):
            raw_item_map = cast(dict[object, object], raw_item)
            normalized_batch_items.append({str(key): raw_item_map[key] for key in raw_item_map})
        else:
            normalized_batch_items.append({"scope": "income-tax", "payload": raw_item})

    batch_id = build_forms_batch_id(items=normalized_batch_items)
    batch_results: list[dict[str, object]] = []
    for index, item in enumerate(normalized_batch_items):
        item_scope = item.get("scope", "income-tax")
        if not isinstance(item_scope, str):
            item_scope = "income-tax"
        normalized_scope = _normalize_forms_tax_domain(item_scope)
        if normalized_scope is None:
            batch_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": build_canonical_batch_item_error(
                        error_code=INVALID_TAX_DOMAIN,
                        message="Requested tax domain is not recognized by the forms boundary.",
                        reason=INVALID_TAX_DOMAIN,
                    ),
                }
            )
            continue
        if normalized_scope != "income-tax":
            batch_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": build_canonical_batch_item_error(
                        error_code=UNIMPLEMENTED_TAX_DOMAIN_MAPPING,
                        message=(
                            "Forms mapping for the requested recognized tax "
                            "domain is not yet implemented."
                        ),
                        reason=UNIMPLEMENTED_TAX_DOMAIN_MAPPING,
                    ),
                }
            )
            continue

        item_payload = item.get("payload")
        if not isinstance(item_payload, dict):
            batch_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": build_canonical_batch_item_error(
                        error_code=FORMS_REQUEST_INVALID,
                        message="Forms request payload is invalid.",
                        reason=FORMS_REQUEST_INVALID,
                    ),
                }
            )
            continue

        try:
            item_payload_map = cast(dict[str, object], item_payload)
            _enforce_disabled_template_capability_guard(
                request=request,
                payload=item_payload_map,
            )
            item_result = generate_income_tax_form_artifact_baseline(
                request=request,
                payload=item_payload_map,
            )
        except HTTPException as error:
            batch_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": extract_batch_item_error_from_http_exception_detail(error.detail),
                }
            )
            continue

        if isinstance(item_result, JSONResponse):
            blocked_payload = _decode_json_response_payload(item_result)
            blocked_reason = blocked_payload.get("reason")
            normalized_reason = (
                str(blocked_reason).strip()
                if isinstance(blocked_reason, str) and str(blocked_reason).strip()
                else FORMS_GENERATION_BLOCKED_BY_VALIDATION
            )
            batch_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": build_canonical_batch_item_error(
                        error_code=normalized_reason,
                        message="Forms artifact generation blocked by validation.",
                        reason=normalized_reason,
                    ),
                }
            )
            continue

        batch_results.append(
            {
                "index": index,
                "status": "succeeded",
                "artifact": item_result,
            }
        )

    summary = build_forms_batch_summary(results=batch_results)
    return {
        "status": "ok",
        "batch_id": batch_id,
        "summary": summary,
        "results": batch_results,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.get("/v1/forms/income-tax/versions")
def list_income_tax_form_versions(
    request: Request,
    user_id: str,
    tax_year: int,
    form_type: str,
) -> dict[str, object]:
    """List deterministic form-history versions for the requested owned filter."""

    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "user_id", "reason": "must_be_non_empty_string"},
        )
    if tax_year < 2000 or tax_year > 2100:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "tax_year", "constraint": "between_2000_and_2100"},
        )

    normalized_form_type = form_type.strip()
    _enforce_disabled_template_capability_guard(
        request=request,
        payload={},
        explicit_template_code=normalized_form_type,
    )
    if normalized_form_type != "income_tax_return":
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_SCOPE_NOT_SUPPORTED,
            message="Requested forms scope is not supported by this baseline.",
            reason=FORMS_SCOPE_NOT_SUPPORTED,
            details={"field": "form_type", "value": normalized_form_type},
        )

    authenticated_user_id = _resolve_history_user_id(request=request)
    if authenticated_user_id != "anonymous_user" and authenticated_user_id != normalized_user_id:
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=FORMS_UNAUTHORIZED_ACCESS,
            message="Access to requested forms history is not authorized.",
            reason=FORMS_UNAUTHORIZED_ACCESS,
            details={
                "requested_user_id": normalized_user_id,
                "authenticated_user_id": authenticated_user_id,
            },
        )

    history_records = list_form_artifact_history_records_by_filter(
        user_id=normalized_user_id,
        tax_year=tax_year,
        form_type=normalized_form_type,
    )
    if not history_records:
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_HISTORY_NOT_FOUND,
            message="No forms history records found for requested filter.",
            reason=FORMS_HISTORY_NOT_FOUND,
            details={
                "user_id": normalized_user_id,
                "tax_year": tax_year,
                "form_type": normalized_form_type,
            },
        )

    versions = [
        {
            "artifact_id": record["artifact_id"],
            "form_type": record["form_type"],
            "form_version_id": record["form_version_id"],
            "tax_year": record["tax_year"],
            "historical_version_id": record["historical_version_id"],
            "status": record["status"],
            "created_at": record["created_at"],
            "lineage_reference": record["lineage_reference"],
        }
        for record in history_records
    ]
    return {
        "status": "ok",
        "user_id": normalized_user_id,
        "tax_year": tax_year,
        "form_type": normalized_form_type,
        "versions": versions,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post("/v1/forms/income-tax/pre-populations")
def resolve_income_tax_form_pre_population(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Resolve deterministic prior-year pre-population suggestions for supported forms."""

    typed_payload = _validate_request_object(
        request=request,
        payload=payload,
        required_fields=("form_type", "target_tax_year"),
    )
    _enforce_disabled_template_capability_guard(request=request, payload=typed_payload)
    form_type = typed_payload.get("form_type")
    if not isinstance(form_type, str) or not form_type.strip():
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_type", "constraint": "non_empty_string"},
        )
    normalized_form_type = form_type.strip()
    if normalized_form_type != "income_tax_return":
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_PRE_POPULATION_SCOPE_NOT_SUPPORTED,
            message="Requested forms scope is not supported for pre-population.",
            reason=FORMS_PRE_POPULATION_SCOPE_NOT_SUPPORTED,
            details={"field": "form_type", "value": normalized_form_type},
        )

    target_tax_year = typed_payload.get("target_tax_year")
    if not isinstance(target_tax_year, int) or target_tax_year < 2000 or target_tax_year > 2100:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "target_tax_year", "constraint": "between_2000_and_2100"},
        )

    source_tax_year_value = typed_payload.get("source_tax_year")
    source_tax_year: int | None
    selection_mode: str
    if source_tax_year_value is None:
        source_tax_year = target_tax_year - 1
        selection_mode = "auto_previous_year"
    elif isinstance(source_tax_year_value, int):
        source_tax_year = source_tax_year_value
        selection_mode = "explicit_source_tax_year"
    else:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "source_tax_year", "constraint": "integer_or_null"},
        )
    if source_tax_year < 2000 or source_tax_year >= target_tax_year:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={
                "field": "source_tax_year",
                "constraint": "between_2000_and_target_tax_year_minus_one",
            },
        )

    authenticated_user_id = _resolve_history_user_id(request=request)
    source_user_id_value = typed_payload.get("source_user_id")
    source_user_id = authenticated_user_id
    if source_user_id_value is not None:
        if not isinstance(source_user_id_value, str) or not source_user_id_value.strip():
            raise create_forms_http_error(
                request=request,
                status_code=400,
                error_code=FORMS_REQUEST_INVALID,
                message="Forms request payload is invalid.",
                reason=FORMS_REQUEST_INVALID,
                details={"field": "source_user_id", "constraint": "non_empty_string_or_null"},
            )
        source_user_id = source_user_id_value.strip()
    if source_user_id != authenticated_user_id:
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=FORMS_PRE_POPULATION_NOT_AUTHORIZED,
            message="Pre-population source access is not authorized.",
            reason=FORMS_PRE_POPULATION_NOT_AUTHORIZED,
            details={
                "requested_user_id": source_user_id,
                "authenticated_user_id": authenticated_user_id,
            },
        )

    prior_year_records = list_form_artifact_history_records_by_filter(
        user_id=source_user_id,
        tax_year=source_tax_year,
        form_type=normalized_form_type,
    )
    if not prior_year_records:
        return {
            "status": "ok",
            "pre_population_status": "source_not_found",
            "reason": FORMS_PRE_POPULATION_SOURCE_NOT_FOUND,
            "target_form_type": normalized_form_type,
            "target_tax_year": target_tax_year,
            "source_context": {
                "selection_mode": selection_mode,
                "source_tax_year": source_tax_year,
                "source_artifact_id": None,
            },
            "populated_fields": [],
            "traceability": {
                "trace_id": get_trace_id(request),
                "correlation_id": get_correlation_id(request),
            },
        }

    source_record = prior_year_records[0]
    source_fields = source_record.get("pre_population_source_fields")
    if not source_fields:
        return {
            "status": "ok",
            "pre_population_status": "source_not_found",
            "reason": FORMS_PRE_POPULATION_SOURCE_NOT_FOUND,
            "target_form_type": normalized_form_type,
            "target_tax_year": target_tax_year,
            "source_context": {
                "selection_mode": selection_mode,
                "source_tax_year": source_tax_year,
                "source_artifact_id": source_record["artifact_id"],
            },
            "populated_fields": [],
            "traceability": {
                "trace_id": get_trace_id(request),
                "correlation_id": get_correlation_id(request),
            },
        }

    populated_fields = build_pre_population_field_suggestions(
        source_fields=source_fields,
        source_artifact_id=source_record["artifact_id"],
        source_tax_year=source_tax_year,
    )
    pre_population_status = "applied" if populated_fields else "source_not_found"
    response_payload: dict[str, object] = {
        "status": "ok",
        "pre_population_status": pre_population_status,
        "target_form_type": normalized_form_type,
        "target_tax_year": target_tax_year,
        "source_context": {
            "selection_mode": selection_mode,
            "source_tax_year": source_tax_year,
            "source_artifact_id": source_record["artifact_id"],
        },
        "populated_fields": populated_fields,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }
    if not populated_fields:
        response_payload["reason"] = FORMS_PRE_POPULATION_SOURCE_NOT_FOUND
    return response_payload


@ROUTER.get("/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata")
def get_income_tax_form_artifact_metadata_baseline(
    request: Request,
    artifact_id: str,
    form_version_id: str,
) -> dict[str, object]:
    """Retrieve deterministic artifact metadata by exact artifact/version identity."""

    normalized_artifact_id = artifact_id.strip().lower()
    if len(normalized_artifact_id) != 64 or not all(
        character in "0123456789abcdef" for character in normalized_artifact_id
    ):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "artifact_id", "constraint": "sha256_hex_64"},
        )
    normalized_form_version_id = form_version_id.strip()
    if not normalized_form_version_id:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_version_id", "constraint": "non_empty_string"},
        )

    history_record = get_form_artifact_history_record_by_identity(
        artifact_id=normalized_artifact_id,
        form_version_id=normalized_form_version_id,
    )
    if history_record is None:
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_HISTORY_NOT_FOUND,
            message="No forms history record found for requested artifact/version.",
            reason=FORMS_HISTORY_NOT_FOUND,
            details={
                "artifact_id": normalized_artifact_id,
                "form_version_id": normalized_form_version_id,
            },
        )

    authenticated_user_id = _resolve_history_user_id(request=request)
    if authenticated_user_id != history_record["user_id"]:
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=FORMS_UNAUTHORIZED_ACCESS,
            message="Access to requested forms artifact metadata is not authorized.",
            reason=FORMS_UNAUTHORIZED_ACCESS,
            details={
                "requested_user_id": history_record["user_id"],
                "authenticated_user_id": authenticated_user_id,
            },
        )

    if history_record["form_type"] != "income_tax_return":
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_SCOPE_NOT_SUPPORTED,
            message="Requested forms scope is not supported by this baseline.",
            reason=FORMS_SCOPE_NOT_SUPPORTED,
            details={"form_type": history_record["form_type"]},
        )
    retention_metadata = get_form_artifact_retention_metadata(history_record["artifact_id"])
    if retention_metadata is None:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_CONTRACT_VIOLATION,
            message="Forms artifact retention metadata violates contract requirements.",
            reason=FORMS_CONTRACT_VIOLATION,
            details={"artifact_id": history_record["artifact_id"]},
        )
    _enforce_artifact_retention_access(
        request=request,
        retention_metadata=retention_metadata,
        details={"artifact_id": history_record["artifact_id"]},
    )

    storage_metadata = get_form_artifact_storage_metadata(history_record["artifact_id"])
    if storage_metadata is None:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_STORAGE_REFERENCE_MISSING,
            message="Forms storage reference metadata is missing.",
            reason=FORMS_STORAGE_REFERENCE_MISSING,
            details={"artifact_id": history_record["artifact_id"]},
        )

    lineage_reference_with_storage = dict(history_record["lineage_reference"])
    lineage_reference_with_storage.update(storage_metadata)

    return {
        "status": "ok",
        "artifact_metadata": {
            "artifact_id": history_record["artifact_id"],
            "form_type": history_record["form_type"],
            "form_version_id": history_record["form_version_id"],
            "tax_year": history_record["tax_year"],
            "historical_version_id": history_record["historical_version_id"],
            "status": history_record["status"],
            "created_at": history_record["created_at"],
            "lineage_reference": lineage_reference_with_storage,
            "download_metadata": {
                "available": retention_metadata["retention_status"] == "active",
                "expires_at": retention_metadata["download_expires_at"],
            },
        },
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.get(
    "/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist"
)
def generate_income_tax_form_submission_checklist(
    request: Request,
    artifact_id: str,
    form_version_id: str,
) -> dict[str, object]:
    """Generate deterministic submission readiness checklist for one artifact version."""

    normalized_artifact_id = artifact_id.strip().lower()
    if len(normalized_artifact_id) != 64 or not all(
        character in "0123456789abcdef" for character in normalized_artifact_id
    ):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "artifact_id", "constraint": "sha256_hex_64"},
        )
    normalized_form_version_id = form_version_id.strip()
    if not normalized_form_version_id:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_version_id", "constraint": "non_empty_string"},
        )

    history_record = get_form_artifact_history_record_by_identity(
        artifact_id=normalized_artifact_id,
        form_version_id=normalized_form_version_id,
    )
    if history_record is None:
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_SUBMISSION_CHECKLIST_SOURCE_MISSING,
            message="Submission checklist source was not found for requested artifact/version.",
            reason=FORMS_SUBMISSION_CHECKLIST_SOURCE_MISSING,
            details={
                "artifact_id": normalized_artifact_id,
                "form_version_id": normalized_form_version_id,
            },
        )

    authenticated_user_id = _resolve_history_user_id(request=request)
    if authenticated_user_id != history_record["user_id"]:
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=FORMS_SUBMISSION_CHECKLIST_NOT_AUTHORIZED,
            message="Submission checklist access is not authorized for requested artifact.",
            reason=FORMS_SUBMISSION_CHECKLIST_NOT_AUTHORIZED,
            details={
                "requested_user_id": history_record["user_id"],
                "authenticated_user_id": authenticated_user_id,
            },
        )

    if history_record["form_type"] != "income_tax_return":
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_SUBMISSION_CHECKLIST_SCOPE_NOT_SUPPORTED,
            message="Requested forms scope is not supported for submission checklist generation.",
            reason=FORMS_SUBMISSION_CHECKLIST_SCOPE_NOT_SUPPORTED,
            details={"form_type": history_record["form_type"]},
        )

    storage_metadata = get_form_artifact_storage_metadata(history_record["artifact_id"])
    retention_metadata = get_form_artifact_retention_metadata(history_record["artifact_id"])
    checklist_payload = build_submission_checklist(
        artifact_id=history_record["artifact_id"],
        form_type=history_record["form_type"],
        tax_year=history_record["tax_year"],
        form_version_id=history_record["form_version_id"],
        lineage_reference=history_record["lineage_reference"],
        storage_metadata=storage_metadata or {},
        retention_metadata=retention_metadata or {},
        pre_population_source_fields=history_record["pre_population_source_fields"],
    )
    return {
        "status": "ok",
        "artifact_id": history_record["artifact_id"],
        "form_version_id": history_record["form_version_id"],
        **checklist_payload,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.post(
    "/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links"
)
def issue_income_tax_form_artifact_download_link(
    request: Request,
    artifact_id: str,
    form_version_id: str,
) -> dict[str, object]:
    """Issue deterministic time-bounded download token for one authorized artifact version."""

    _mark_forms_download_issuance_started(request=request)
    normalized_artifact_id = artifact_id.strip().lower()
    if len(normalized_artifact_id) != 64 or not all(
        character in "0123456789abcdef" for character in normalized_artifact_id
    ):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "artifact_id", "constraint": "sha256_hex_64"},
        )
    normalized_form_version_id = form_version_id.strip()
    if not normalized_form_version_id:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"field": "form_version_id", "constraint": "non_empty_string"},
        )

    history_record = get_form_artifact_history_record_by_identity(
        artifact_id=normalized_artifact_id,
        form_version_id=normalized_form_version_id,
    )
    if history_record is None:
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_DOWNLOAD_ARTIFACT_NOT_FOUND,
            message="Requested forms artifact for download issuance was not found.",
            reason=FORMS_DOWNLOAD_ARTIFACT_NOT_FOUND,
            details={
                "artifact_id": normalized_artifact_id,
                "form_version_id": normalized_form_version_id,
            },
        )

    if history_record["form_type"] != "income_tax_return":
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=FORMS_SCOPE_NOT_SUPPORTED,
            message="Requested forms scope is not supported by this baseline.",
            reason=FORMS_SCOPE_NOT_SUPPORTED,
            details={"form_type": history_record["form_type"]},
        )
    retention_metadata = get_form_artifact_retention_metadata(normalized_artifact_id)
    if retention_metadata is None:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_CONTRACT_VIOLATION,
            message="Forms artifact retention metadata violates contract requirements.",
            reason=FORMS_CONTRACT_VIOLATION,
            details={"artifact_id": normalized_artifact_id},
        )

    authenticated_user_id = _resolve_history_user_id(request=request)
    if authenticated_user_id != history_record["user_id"]:
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=FORMS_DOWNLOAD_NOT_AUTHORIZED,
            message="Download issuance is not authorized for requested artifact.",
            reason=FORMS_DOWNLOAD_NOT_AUTHORIZED,
            details={
                "requested_user_id": history_record["user_id"],
                "authenticated_user_id": authenticated_user_id,
            },
        )
    _enforce_artifact_retention_access(
        request=request,
        retention_metadata=retention_metadata,
        details={"artifact_id": normalized_artifact_id},
    )
    _enforce_download_access(
        request=request,
        retention_metadata=retention_metadata,
        details={"artifact_id": normalized_artifact_id},
    )

    storage_metadata = get_form_artifact_storage_metadata(normalized_artifact_id)
    if storage_metadata is None:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            message="Forms download link issuance failed.",
            reason=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            details={
                "artifact_id": normalized_artifact_id,
                "reason": "storage_reference_missing",
            },
        )

    try:
        reference_now = get_forms_retention_reference_time()
        issuance_payload = issue_forms_artifact_download_token(
            artifact_id=normalized_artifact_id,
            form_version_id=normalized_form_version_id,
            owner_user_id=authenticated_user_id,
            now=reference_now,
        )
    except FormsDownloadLinkIssuanceError as error:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            message=error.message,
            reason=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error
    try:
        set_form_artifact_download_expiry(
            artifact_id=normalized_artifact_id,
            download_expires_at=str(issuance_payload["expires_at"]),
        )
    except FormsHistoryStoreError as error:
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            message="Forms download link issuance failed.",
            reason=FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            details={"upstream_reason": error.reason, **error.details()},
        ) from error

    issued_at = str(issuance_payload["issued_at"])
    audit_evidence = {
        "audit_event_id": str(issuance_payload["audit_event_id"]),
        "event_type": "forms_download_link_issued",
        "event_timestamp": issued_at,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
        "lineage_reference": {
            "artifact_id": normalized_artifact_id,
            "form_version_id": normalized_form_version_id,
            "historical_version_id": history_record["historical_version_id"],
            "artifact_hash": history_record["artifact_hash"],
            "form_type": history_record["form_type"],
            "tax_year": history_record["tax_year"],
        },
        "actor_context": {
            "actor_type": "user",
            "user_id": authenticated_user_id,
        },
    }
    _emit_forms_download_issuance_success(request=request)
    _emit_forms_download_issuance_latency(request=request)
    return {
        "status": "issued",
        "artifact_id": normalized_artifact_id,
        "download_token": issuance_payload["download_token"],
        "issued_at": issuance_payload["issued_at"],
        "expires_at": issuance_payload["expires_at"],
        "ttl_seconds": issuance_payload["ttl_seconds"],
        "audit_evidence": audit_evidence,
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


@ROUTER.api_route(
    "/v1/forms/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def reject_unsupported_or_unimplemented_scope(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for invalid tax domains and recognized but unimplemented paths."""

    normalized_scope = _normalize_forms_tax_domain(scope)
    requested_path = f"/v1/forms/{scope}/{remaining_path}".rstrip("/")
    if normalized_scope is None:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_TAX_DOMAIN,
            message="Requested tax domain is not recognized by the forms boundary.",
            reason=INVALID_TAX_DOMAIN,
            details={
                "requested_path": requested_path,
                "tax_domain": scope.strip().lower() or "unknown",
            },
        )
    if normalized_scope != "income-tax":
        normalized_remaining_path = remaining_path.strip().lower().strip("/")
        if normalized_remaining_path == "mappings":
            raise create_forms_http_error(
                request=request,
                status_code=501,
                error_code=UNIMPLEMENTED_TAX_DOMAIN_MAPPING,
                message=(
                    "Forms mapping for the requested recognized tax domain is not yet implemented."
                ),
                reason=UNIMPLEMENTED_TAX_DOMAIN_MAPPING,
                details={
                    "requested_path": requested_path,
                    "tax_domain": normalized_scope.replace("-", "_"),
                },
            )
        raise create_forms_http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_TAX_DOMAIN_PATH,
            message="Requested forms path is not available for the recognized tax domain.",
            reason=UNSUPPORTED_TAX_DOMAIN_PATH,
            details={
                "requested_path": requested_path,
                "tax_domain": normalized_scope.replace("-", "_"),
            },
        )

    _raise_operation_not_implemented(
        request=request,
        operation="income_tax_route_not_yet_wired",
        details={"requested_path": requested_path},
    )


def create_app() -> FastAPI:
    """Build forms FastAPI app with deterministic baseline route registration."""

    from services.forms.app.template_registry import build_forms_template_capability_index

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.state.forms_template_capability_index = build_forms_template_capability_index()
    app.state.forms_metrics_emitter = get_default_forms_metrics_emitter()
    app.state.forms_slo_threshold_policy = get_default_forms_slo_threshold_policy()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, _handle_request_validation_error),
    )
    app.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, _handle_http_exception_error),
    )
    app.include_router(ROUTER)
    return app


def get_forms_metrics_emitter(request: Request) -> FormsMetricsEmitter:
    """Resolve optional test override or default forms metrics emitter."""

    configured_emitter = getattr(request.app.state, "forms_metrics_emitter", None)
    if configured_emitter is not None:
        return cast(FormsMetricsEmitter, configured_emitter)
    return get_default_forms_metrics_emitter()


def list_forms_metric_events(*, app_instance: FastAPI) -> tuple[MetricEvent, ...]:
    """Return immutable snapshot of emitted forms metric events."""

    configured_emitter = getattr(app_instance.state, "forms_metrics_emitter", None)
    if configured_emitter is None:
        return tuple()
    return cast(FormsMetricsEmitter, configured_emitter).snapshot()


def reset_forms_metric_events(*, app_instance: FastAPI) -> None:
    """Reset emitted forms metric events for deterministic isolated tests."""

    configured_emitter = getattr(app_instance.state, "forms_metrics_emitter", None)
    if configured_emitter is None:
        return
    cast(FormsMetricsEmitter, configured_emitter).reset()


def evaluate_forms_slo_alerts(
    *,
    app_instance: FastAPI,
    metrics_snapshot: FormsSloMetricSnapshot,
) -> tuple[FormsSloAlert, ...]:
    """Evaluate deterministic forms SLO alerts using app-policy state."""

    configured_policy = getattr(app_instance.state, "forms_slo_threshold_policy", None)
    effective_policy = (
        cast(FormsSloThresholdPolicy, configured_policy)
        if configured_policy is not None
        else get_default_forms_slo_threshold_policy()
    )
    return evaluate_forms_slo_thresholds(
        metrics_snapshot=metrics_snapshot,
        policy=effective_policy,
    )


def _is_generation_endpoint_request(*, request: Request) -> bool:
    return request.method.upper() == "POST" and request.url.path == FORMS_GENERATION_ENDPOINT_PATH


def _is_download_issuance_endpoint_request(*, request: Request) -> bool:
    if request.method.upper() != "POST":
        return False
    path = request.url.path
    if not path.endswith(FORMS_DOWNLOAD_ISSUANCE_SUFFIX):
        return False
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) != 8:
        return False
    return (
        segments[0] == "v1"
        and segments[1] == "forms"
        and segments[2] == "income-tax"
        and segments[3] == "artifacts"
        and segments[5] == "versions"
        and segments[7] == "download-links"
    )


def _mark_forms_generation_started(*, request: Request) -> None:
    request.state.forms_generation_started_at = perf_counter()


def _mark_forms_download_issuance_started(*, request: Request) -> None:
    request.state.forms_download_issuance_started_at = perf_counter()


def _emit_forms_generation_success(*, request: Request) -> None:
    emitter = get_forms_metrics_emitter(request=request)
    emitter.increment_counter_non_blocking(
        FORMS_GENERATION_SUCCESS_TOTAL,
        dimensions={
            "endpoint": FORMS_GENERATION_ENDPOINT_PATH,
            "status": "success",
            "reason_code": "generated",
        },
    )


def _emit_forms_generation_failure(*, request: Request, reason_code: str) -> None:
    emitter = get_forms_metrics_emitter(request=request)
    emitter.increment_counter_non_blocking(
        FORMS_GENERATION_FAILURE_TOTAL,
        dimensions={
            "endpoint": FORMS_GENERATION_ENDPOINT_PATH,
            "status": "failure",
            "reason_code": reason_code,
        },
    )


def _emit_forms_generation_latency(*, request: Request) -> None:
    started_at = getattr(request.state, "forms_generation_started_at", None)
    if not isinstance(started_at, float):
        return
    duration_ms = round((perf_counter() - started_at) * 1000, 3)
    emitter = get_forms_metrics_emitter(request=request)
    emitter.observe_histogram_non_blocking(
        FORMS_GENERATION_LATENCY_MS,
        value=duration_ms,
        dimensions={
            "endpoint": FORMS_GENERATION_ENDPOINT_PATH,
            "status": "observed",
            "reason_code": "request_completed",
        },
    )


def _emit_forms_download_issuance_success(*, request: Request) -> None:
    emitter = get_forms_metrics_emitter(request=request)
    emitter.increment_counter_non_blocking(
        FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL,
        dimensions={
            "endpoint": request.url.path,
            "status": "success",
            "reason_code": "issued",
        },
    )


def _emit_forms_download_issuance_failure(*, request: Request, reason_code: str) -> None:
    emitter = get_forms_metrics_emitter(request=request)
    emitter.increment_counter_non_blocking(
        FORMS_DOWNLOAD_ISSUANCE_FAILURE_TOTAL,
        dimensions={
            "endpoint": request.url.path,
            "status": "failure",
            "reason_code": reason_code,
        },
    )


def _emit_forms_download_issuance_latency(*, request: Request) -> None:
    started_at = getattr(request.state, "forms_download_issuance_started_at", None)
    if not isinstance(started_at, float):
        return
    duration_ms = round((perf_counter() - started_at) * 1000, 3)
    emitter = get_forms_metrics_emitter(request=request)
    emitter.observe_histogram_non_blocking(
        FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS,
        value=duration_ms,
        dimensions={
            "endpoint": request.url.path,
            "status": "observed",
            "reason_code": "request_completed",
        },
    )


def _emit_forms_download_access_denied(*, request: Request, reason_code: str) -> None:
    emitter = get_forms_metrics_emitter(request=request)
    emitter.increment_counter_non_blocking(
        FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL,
        dimensions={
            "endpoint": request.url.path,
            "status": "denied",
            "reason_code": reason_code,
            "denial_class": _map_forms_download_denial_class(reason_code=reason_code),
        },
    )


def _map_forms_download_denial_class(*, reason_code: str) -> str:
    if reason_code == FORMS_DOWNLOAD_NOT_AUTHORIZED:
        return "auth"
    if reason_code == FORMS_DOWNLOAD_LINK_EXPIRED:
        return "expiry"
    return "retention"


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    envelope = build_forms_error_envelope(
        request=request,
        error_code=FORMS_REQUEST_INVALID,
        message="Forms request payload is invalid.",
        reason=FORMS_REQUEST_INVALID,
        details={"validation_errors": exc.errors()},
    )
    return JSONResponse(status_code=400, content={"detail": envelope})


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail_payload: dict[str, object] = {}
    if isinstance(exc.detail, dict):
        detail_payload = dict(cast(dict[str, object], exc.detail))
    reason = detail_payload.get("reason")
    error_code = detail_payload.get("error_code")
    message = detail_payload.get("message")
    details = detail_payload.get("details")
    allowed_reason_codes = FORMS_REASON_CODES | frozenset(
        {
            FORMS_MAPPING_INPUT_NOT_FINALIZED,
            FORMS_VERSION_NOT_SUPPORTED,
            FORMS_VERSION_BINDING_AMBIGUOUS,
            FORMS_GENERATION_PRECONDITION_MISSING,
            FORMS_ARTIFACT_GENERATION_FAILED,
            FORMS_AUDIT_EVIDENCE_MISSING,
            FORMS_VALIDATION_CONTRACT_VIOLATION,
            FORMS_GENERATION_BLOCKED_BY_VALIDATION,
            FORMS_HISTORY_PERSISTENCE_FAILED,
            FORMS_HISTORY_NOT_FOUND,
            FORMS_UNAUTHORIZED_ACCESS,
            FORMS_STORAGE_WRITE_FAILED,
            FORMS_STORAGE_REFERENCE_MISSING,
            FORMS_DOWNLOAD_NOT_AUTHORIZED,
            FORMS_DOWNLOAD_ARTIFACT_NOT_FOUND,
            FORMS_DOWNLOAD_LINK_ISSUANCE_FAILED,
            FORMS_DOWNLOAD_LINK_EXPIRED,
            FORMS_ARTIFACT_RETENTION_EXPIRED,
            FORMS_ARTIFACT_ACCESS_RESTRICTED,
            FORMS_PRE_POPULATION_SOURCE_NOT_FOUND,
            FORMS_PRE_POPULATION_SCOPE_NOT_SUPPORTED,
            FORMS_PRE_POPULATION_NOT_AUTHORIZED,
            FORMS_SUBMISSION_CHECKLIST_NOT_AUTHORIZED,
            FORMS_SUBMISSION_CHECKLIST_SCOPE_NOT_SUPPORTED,
            FORMS_SUBMISSION_CHECKLIST_SOURCE_MISSING,
            FORMS_TEMPLATE_CAPABILITY_DISABLED,
            INVALID_TAX_DOMAIN,
            UNSUPPORTED_TAX_DOMAIN_PATH,
            UNIMPLEMENTED_TAX_DOMAIN_MAPPING,
        }
    )
    normalized_reason = (
        str(reason).strip()
        if isinstance(reason, str) and str(reason).strip() in allowed_reason_codes
        else FORMS_CONTRACT_VIOLATION
    )
    normalized_error_code = (
        str(error_code).strip()
        if isinstance(error_code, str) and str(error_code).strip()
        else normalized_reason
    )
    normalized_message = (
        str(message).strip()
        if isinstance(message, str) and str(message).strip()
        else "Forms request failed."
    )
    normalized_details = cast(dict[str, object], details) if isinstance(details, dict) else None
    if _is_generation_endpoint_request(request=request):
        _emit_forms_generation_failure(request=request, reason_code=normalized_reason)
        _emit_forms_generation_latency(request=request)
    elif _is_download_issuance_endpoint_request(request=request):
        _emit_forms_download_issuance_failure(request=request, reason_code=normalized_reason)
        _emit_forms_download_issuance_latency(request=request)
        if normalized_reason in FORMS_DOWNLOAD_DENIAL_REASONS:
            _emit_forms_download_access_denied(
                request=request,
                reason_code=normalized_reason,
            )
    envelope = build_forms_error_envelope(
        request=request,
        error_code=normalized_error_code,
        message=normalized_message,
        reason=normalized_reason,
        details=normalized_details,
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


def _raise_operation_not_implemented(
    *,
    request: Request,
    operation: str,
    details: dict[str, object] | None = None,
) -> NoReturn:
    operation_details: dict[str, object] = {"operation": operation}
    if details is not None:
        operation_details.update(details)
    raise create_forms_http_error(
        request=request,
        status_code=501,
        error_code=FORMS_OPERATION_NOT_IMPLEMENTED,
        message="Forms operation is not implemented in the Phase 10.1 runtime baseline.",
        reason=FORMS_OPERATION_NOT_IMPLEMENTED,
        details=operation_details,
    )


def _decode_json_response_payload(response: JSONResponse) -> dict[str, object]:
    body = response.body
    if not body:
        return {}
    try:
        raw_body = bytes(body)
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}


def _enforce_disabled_template_capability_guard(
    *,
    request: Request,
    payload: dict[str, object],
    explicit_template_code: object | None = None,
) -> None:
    capability_index = _get_forms_template_capability_index(request=request)
    requested_template_code = _extract_requested_template_code(
        payload=payload,
        capability_index=capability_index,
        explicit_template_code=explicit_template_code,
    )
    if requested_template_code is None:
        return

    capability_entry = capability_index.get(requested_template_code)
    if not isinstance(capability_entry, dict):
        return
    capability_status = capability_entry.get("status")
    if capability_status != "disabled":
        return

    raise create_forms_http_error(
        request=request,
        status_code=409,
        error_code=FORMS_TEMPLATE_CAPABILITY_DISABLED,
        message="Requested forms template capability is disabled by governance policy.",
        reason=FORMS_TEMPLATE_CAPABILITY_DISABLED,
        details={
            "template_code": requested_template_code,
            "capability_status": "disabled",
        },
    )


def _extract_requested_template_code(
    *,
    payload: dict[str, object],
    capability_index: dict[str, dict[str, object]],
    explicit_template_code: object | None = None,
) -> str | None:
    candidate_values: list[object] = [explicit_template_code]
    candidate_values.extend(
        cast(
            list[object],
            [
                payload.get("template_code"),
                payload.get("form_template_code"),
                payload.get("form_type"),
            ],
        )
    )
    for nested_key in ("mapped_output", "form_ready_output", "form_version_binding"):
        nested_value = payload.get(nested_key)
        if not isinstance(nested_value, dict):
            continue
        nested_value_map = cast(dict[str, object], nested_value)
        candidate_values.extend(
            cast(
                list[object],
                [
                    nested_value_map.get("template_code"),
                    nested_value_map.get("form_template_code"),
                    nested_value_map.get("form_type"),
                ],
            )
        )

    for candidate in candidate_values:
        normalized_candidate = _normalize_template_request_value(candidate)
        if normalized_candidate is None:
            continue
        if normalized_candidate in capability_index:
            return normalized_candidate
    return None


def _normalize_template_request_value(value: object) -> str | None:
    from services.forms.app.template_registry import normalize_template_code

    normalized_value = normalize_template_code(value)
    if normalized_value is None:
        return None
    if len(normalized_value) > 8:
        return None
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for character in normalized_value
    ):
        return None
    return normalized_value


def _get_forms_template_capability_index(
    *,
    request: Request,
) -> dict[str, dict[str, object]]:
    from services.forms.app.template_registry import build_forms_template_capability_index

    existing_index = getattr(request.app.state, "forms_template_capability_index", None)
    if isinstance(existing_index, dict):
        return cast(dict[str, dict[str, object]], existing_index)
    loaded_index = build_forms_template_capability_index()
    request.app.state.forms_template_capability_index = loaded_index
    return loaded_index


def _validate_request_object(
    *,
    request: Request,
    payload: Any,
    required_fields: tuple[str, ...],
    missing_field_reason: str = FORMS_CONTRACT_VIOLATION,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=FORMS_REQUEST_INVALID,
            message="Forms request payload is invalid.",
            reason=FORMS_REQUEST_INVALID,
            details={"reason": "request_body_must_be_object"},
        )
    typed_payload = cast(dict[str, object], payload)
    missing_fields = [
        field_name for field_name in required_fields if field_name not in typed_payload
    ]
    if missing_fields:
        raise create_forms_http_error(
            request=request,
            status_code=400,
            error_code=missing_field_reason,
            message="Forms request violates contract constraints.",
            reason=missing_field_reason,
            details={"missing_fields": missing_fields},
        )
    return typed_payload


def _map_income_tax_mapping_error_reason(reason: str) -> tuple[int, str]:
    if reason == "computation_not_finalized":
        return 409, FORMS_MAPPING_INPUT_NOT_FINALIZED

    unsupported_scope_reasons = {
        "unsupported_tax_type",
        "unsupported_regime_type",
        "unsupported_result_scope",
        "unsupported_historical_version",
        "unsupported_taxpayer_kind",
        "unsupported_income_domain",
        "unsupported_mixed_income_lane",
        "unsupported_mixed_income_treatment",
        "unsupported_income_lane",
    }
    if reason in unsupported_scope_reasons:
        return 409, FORMS_SCOPE_NOT_SUPPORTED

    invalid_request_reasons = {
        "invalid_mapping_input",
        "missing_required_field",
        "invalid_money_field",
        "invalid_list_item",
        "missing_required_domain_outcome",
        "missing_required_treatment_decision",
        "resident_status_relief_mismatch",
        "mixed_income_liability_mismatch",
    }
    if reason in invalid_request_reasons:
        return 400, FORMS_REQUEST_INVALID

    return 400, FORMS_REQUEST_INVALID


def _map_health_contribution_mapping_error_reason(reason: str) -> tuple[int, str]:
    if reason == "computation_not_finalized":
        return 409, FORMS_MAPPING_INPUT_NOT_FINALIZED

    unsupported_scope_reasons = {
        "unsupported_tax_type",
        "unsupported_regime_type",
        "unsupported_result_scope",
        "unsupported_historical_version",
    }
    if reason in unsupported_scope_reasons:
        return 409, FORMS_SCOPE_NOT_SUPPORTED

    invalid_request_reasons = {
        "invalid_mapping_input",
        "missing_required_field",
        "invalid_money_field",
        "invalid_list_item",
    }
    if reason in invalid_request_reasons:
        return 400, FORMS_REQUEST_INVALID

    return 400, FORMS_REQUEST_INVALID


def _map_income_tax_version_binding_error_reason(reason: str) -> tuple[int, str]:
    if reason == "ambiguous_form_version_context":
        return 409, FORMS_VERSION_BINDING_AMBIGUOUS

    if reason == "unsupported_form_version_binding":
        return 409, FORMS_VERSION_NOT_SUPPORTED

    if reason == "unsupported_form_type":
        return 409, FORMS_SCOPE_NOT_SUPPORTED

    invalid_request_reasons = {
        "invalid_form_ready_output",
        "invalid_mapping_status",
        "missing_required_field",
        "invalid_list_item",
    }
    if reason in invalid_request_reasons:
        return 400, FORMS_REQUEST_INVALID

    return 400, FORMS_REQUEST_INVALID


def _map_income_tax_artifact_generation_error_reason(reason: str) -> tuple[int, str]:
    if reason == "unsupported_tax_type":
        return 409, FORMS_SCOPE_NOT_SUPPORTED

    if reason == "lineage_mismatch":
        return 409, FORMS_VERSION_NOT_SUPPORTED

    invalid_request_reasons = {
        "invalid_finalized_output",
        "invalid_form_ready_output",
        "invalid_form_version_binding",
        "missing_required_field",
        "invalid_list_item",
    }
    if reason in invalid_request_reasons:
        return 400, FORMS_GENERATION_PRECONDITION_MISSING

    precondition_reasons = {
        "computation_not_finalized",
        "invalid_mapping_status",
        "invalid_binding_status",
        "unsupported_mapped_fields",
    }
    if reason in precondition_reasons:
        return 409, FORMS_GENERATION_PRECONDITION_MISSING

    return 409, FORMS_ARTIFACT_GENERATION_FAILED


def _build_generation_path_audit_evidence(
    *,
    request: Request,
    event_type: str,
    event_timestamp: str,
    lineage_reference: dict[str, object],
    pipeline_output: dict[str, object],
) -> dict[str, object]:
    if not event_timestamp.strip():
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_AUDIT_EVIDENCE_MISSING,
            message="Forms response is missing required audit evidence linkage.",
            reason=FORMS_AUDIT_EVIDENCE_MISSING,
            details={"field": "event_timestamp"},
        )
    audit_payload = pipeline_output.get("audit_evidence")
    if not isinstance(audit_payload, dict):
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_AUDIT_EVIDENCE_MISSING,
            message="Forms response is missing required audit evidence linkage.",
            reason=FORMS_AUDIT_EVIDENCE_MISSING,
            details={"field": "audit_evidence"},
        )
    audit_payload_map = cast(dict[str, object], audit_payload)
    raw_audit_event_id = audit_payload_map.get("audit_evidence_id")
    if not isinstance(raw_audit_event_id, str) or not raw_audit_event_id.strip():
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_AUDIT_EVIDENCE_MISSING,
            message="Forms response is missing required audit evidence linkage.",
            reason=FORMS_AUDIT_EVIDENCE_MISSING,
            details={"field": "audit_event_id"},
        )

    return build_forms_audit_evidence_envelope(
        audit_event_id=raw_audit_event_id,
        event_type=event_type,
        event_timestamp=event_timestamp,
        trace_id=get_trace_id(request),
        correlation_id=get_correlation_id(request),
        lineage_reference=lineage_reference,
        actor_context={
            "actor_type": "user",
            "user_id": _resolve_history_user_id(request=request),
        },
    )


def _build_generation_block_response(
    *,
    request: Request,
    validation_result: Mapping[str, object],
) -> dict[str, object]:
    findings = validation_result.get("findings")
    if not isinstance(findings, list):
        findings = []
    return {
        "status": "blocked",
        "reason": FORMS_GENERATION_BLOCKED_BY_VALIDATION,
        "validation": {
            "is_valid": False,
            "findings": findings,
        },
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


def _build_governed_validation_block_response(
    *,
    request: Request,
    governed_validation: Mapping[str, object],
) -> dict[str, object]:
    issues = governed_validation.get("issues")
    if not isinstance(issues, list):
        issues = []
    return {
        "status": "blocked",
        "reason": FORMS_GENERATION_BLOCKED_BY_VALIDATION,
        "validation": {
            "is_valid": False,
            "findings": issues,
        },
        "governed_validation": dict(governed_validation),
        "traceability": {
            "trace_id": get_trace_id(request),
            "correlation_id": get_correlation_id(request),
        },
    }


def _enforce_artifact_retention_access(
    *,
    request: Request,
    retention_metadata: Mapping[str, object],
    details: dict[str, object] | None = None,
) -> None:
    try:
        evaluate_forms_artifact_retention_access(retention_metadata=retention_metadata)
    except FormsRetentionPolicyError as error:
        denial_lineage = {
            **(details or {}),
            "denial_reason": error.reason,
        }
        emit_forms_audit_log_event(
            {
                "audit_event_id": build_forms_audit_event_id(
                    {
                        "event_type": FORMS_AUDIT_EVENT_ACCESS_DENIED,
                        "trace_id": get_trace_id(request),
                        "correlation_id": get_correlation_id(request),
                        "lineage_reference": denial_lineage,
                    }
                ),
                "event_type": FORMS_AUDIT_EVENT_ACCESS_DENIED,
                "event_timestamp": get_forms_audit_event_timestamp(),
                "trace_id": get_trace_id(request),
                "correlation_id": get_correlation_id(request),
                "lineage_reference": denial_lineage,
                "actor_context": {
                    "actor_type": "user",
                    "user_id": _resolve_history_user_id(request=request),
                },
            }
        )
        reason = (
            error.reason
            if error.reason in {FORMS_ARTIFACT_RETENTION_EXPIRED, FORMS_ARTIFACT_ACCESS_RESTRICTED}
            else FORMS_REQUEST_INVALID
        )
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=reason,
            message=error.message,
            reason=reason,
            details={
                **(details or {}),
                "retention_metadata": retention_metadata,
                "upstream_reason": error.reason,
                **error.details(),
            },
        ) from error


def _enforce_download_access(
    *,
    request: Request,
    retention_metadata: Mapping[str, object],
    details: dict[str, object] | None = None,
) -> None:
    try:
        evaluate_forms_download_access(retention_metadata=retention_metadata)
    except FormsRetentionPolicyError as error:
        denial_lineage = {
            **(details or {}),
            "denial_reason": error.reason,
        }
        emit_forms_audit_log_event(
            {
                "audit_event_id": build_forms_audit_event_id(
                    {
                        "event_type": FORMS_AUDIT_EVENT_ACCESS_DENIED,
                        "trace_id": get_trace_id(request),
                        "correlation_id": get_correlation_id(request),
                        "lineage_reference": denial_lineage,
                    }
                ),
                "event_type": FORMS_AUDIT_EVENT_ACCESS_DENIED,
                "event_timestamp": get_forms_audit_event_timestamp(),
                "trace_id": get_trace_id(request),
                "correlation_id": get_correlation_id(request),
                "lineage_reference": denial_lineage,
                "actor_context": {
                    "actor_type": "user",
                    "user_id": _resolve_history_user_id(request=request),
                },
            }
        )
        reason = (
            error.reason if error.reason == FORMS_DOWNLOAD_LINK_EXPIRED else FORMS_REQUEST_INVALID
        )
        raise create_forms_http_error(
            request=request,
            status_code=403,
            error_code=reason,
            message=error.message,
            reason=reason,
            details={
                **(details or {}),
                "retention_metadata": retention_metadata,
                "upstream_reason": error.reason,
                **error.details(),
            },
        ) from error


def _build_form_history_record_from_artifact_response(
    artifact_response: dict[str, object],
    *,
    request: Request,
) -> dict[str, object]:
    historical_version_id = artifact_response.get("historical_version_id")
    normalized_historical_version_id: str | None = None
    if isinstance(historical_version_id, str) and historical_version_id.strip():
        normalized_historical_version_id = historical_version_id

    storage_metadata = artifact_response.get("storage_metadata")
    if not isinstance(storage_metadata, dict):
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_STORAGE_REFERENCE_MISSING,
            message="Forms storage reference metadata is missing.",
            reason=FORMS_STORAGE_REFERENCE_MISSING,
            details={"field": "storage_metadata", "constraint": "object"},
        )
    retention_metadata = artifact_response.get("retention_metadata")
    if not isinstance(retention_metadata, dict):
        raise create_forms_http_error(
            request=request,
            status_code=500,
            error_code=FORMS_CONTRACT_VIOLATION,
            message="Forms artifact retention metadata violates contract requirements.",
            reason=FORMS_CONTRACT_VIOLATION,
            details={"field": "retention_metadata", "constraint": "object"},
        )
    artifact_output = artifact_response.get("artifact_output")
    pre_population_source_fields: dict[str, object] = {}
    if isinstance(artifact_output, dict):
        artifact_output_map = cast(dict[str, object], artifact_output)
        generated_content_payload = artifact_output_map.get("generated_content_payload")
        if isinstance(generated_content_payload, dict):
            pre_population_source_fields = build_pre_population_source_fields(
                generated_content_payload=cast(dict[str, object], generated_content_payload)
            )

    return {
        "user_id": _resolve_history_user_id(request=request),
        "artifact_id": artifact_response.get("artifact_id"),
        "form_type": artifact_response.get("form_type"),
        "form_version_id": artifact_response.get("form_version_id"),
        "tax_year": artifact_response.get("tax_year"),
        "historical_version_id": normalized_historical_version_id,
        "lineage_reference": artifact_response.get("lineage_reference"),
        "artifact_hash": artifact_response.get("artifact_hash"),
        "created_at": artifact_response.get("created_at"),
        "status": "current",
        "storage_metadata": storage_metadata,
        "retention_metadata": retention_metadata,
        "pre_population_source_fields": pre_population_source_fields,
    }


def _normalize_forms_tax_domain(scope: str) -> str | None:
    normalized_scope = scope.strip().lower()
    if not normalized_scope:
        return None
    return RECOGNIZED_FORMS_TAX_DOMAINS.get(normalized_scope)


def _resolve_history_user_id(*, request: Request) -> str:
    requested_user_id = request.headers.get("X-User-ID", "").strip()
    if requested_user_id:
        return requested_user_id
    return "anonymous_user"


app = create_app()
