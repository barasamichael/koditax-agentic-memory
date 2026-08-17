"""Milestone 25 guardrails for governed orchestration document evidence."""

from __future__ import annotations

from uuid import uuid4

from services.orchestration.app.action_adapter_registry import DocumentAIServiceActionAdapter
from services.orchestration.app.action_adapter_registry import SUPPORTED_ROUTE_ACTIONS
from services.orchestration.app.action_result_mapping import map_action_result


def _request(action_type: str) -> dict[str, object]:
    return {
        "action_type": action_type,
        "correlation_id": "corr-milestone-25",
        "submission_payload_ref": str(uuid4()),
        "capability_context": {
            "supported_lane_id": None,
            "historical_version_id": None,
            "tax_year": None,
        },
    }


def test_document_routes_expose_only_governed_evidence_actions() -> None:
    operations = {
        operation
        for service, operation in SUPPORTED_ROUTE_ACTIONS
        if service == "document_ai"
    }
    assert operations == {
        "get_document_processing_status",
        "search_document_evidence",
        "retrieve_document_evidence",
        "derive_document_evidence",
        "create_workflow_evidence_projection",
    }
    assert "create_document_extraction" not in operations


def test_document_evidence_requires_real_service_and_document() -> None:
    response = DocumentAIServiceActionAdapter(base_url=None).dispatch(
        _request("document_ai_search_document_evidence")  # type: ignore[arg-type]
    )
    assert response["action_result_code"] == "document_ai_integration_unconfigured"
    assert response["adapter_status"] == "unsupported"


def test_processing_pending_and_evidence_limitations_survive_result_mapping() -> None:
    response = {
        "adapter_status": "accepted",
        "provider_reference": "document-1",
        "action_result_code": "document_evidence_processing_pending",
        "message": "Document processing is still pending; evidence limitations were preserved.",
        "trace": {
            "correlation_id": "corr-milestone-25",
            "trace_id": "trace-milestone-25",
            "adapter_request_id": "request-milestone-25",
            "adapter_name": "document_ai_service_adapter_v1",
            "submission_payload_ref": "document-1",
        },
        "error": None,
        "result_payload": {
            "document_id": "document-1",
            "lifecycle_status": "processing",
            "evidence_limitations": ["canonical_content_not_ready"],
        },
    }
    result = map_action_result(
        idempotency_key="idem-milestone-25",
        correlation_id="corr-milestone-25",
        trace_id="trace-milestone-25",
        execution_status="accepted",
        adapter_response=response,  # type: ignore[arg-type]
        execution_error=None,
    )
    assert result["action_status"] == "pending"
