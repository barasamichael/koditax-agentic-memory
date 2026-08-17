"""Source-reference projection checks for document-grounded orchestration answers."""

from __future__ import annotations

from services.orchestration.app.llm_synthesis_context import build_governed_synthesis_context


def test_document_extraction_context_projects_user_facing_source_references() -> None:
    context = build_governed_synthesis_context(
        prompt_text="What does the document say about the filing deadline?",
        tax_domain_hint="income_tax",
        intent_class="document_extraction",
        plan={
            "plan_id": "plan-1",
            "plan_status": "resolved",
            "planning_mode": "single_step",
            "execution_ready": True,
            "steps": [],
        },
        mapped_result={
            "action_status": "pending",
            "reason_code": None,
        },
        final_outcome={
            "message": "Document evidence resolved.",
            "result": {},
        },
        selected_route={
            "target_service": "document_ai",
        },
        adapter_response={
            "result_payload": {
                "document_id": "doc-123",
                "lifecycle_status": "active",
                "evidence": [
                    {
                        "document_id": "doc-123",
                        "display_name": "Quarterly return.pdf",
                        "source_filename": "Quarterly return.pdf",
                        "element_type": "paragraph",
                        "source_region": {
                            "page_number": 3,
                        },
                    }
                ],
            }
        },
        step_results=None,
        step_summary=None,
        grounded_evidence=None,
        explanation_items=None,
        citations=None,
        authority_summary=None,
        temporal_applicability=None,
    )

    assert context["source_references"] == [
        {
            "document_id": "doc-123",
            "document_label": "Quarterly return.pdf",
            "document_status": "available",
            "source_location": {
                "location_kind": "page",
                "location_label": "Page 3",
                "location_status": "partial",
                "page_number": 3,
                "slide_number": None,
                "sheet_name": None,
                "line_start": None,
                "line_end": None,
                "cell_reference": None,
                "section_name": None,
            },
            "openable": True,
            "accessibility_label": "Source available",
        }
    ]
