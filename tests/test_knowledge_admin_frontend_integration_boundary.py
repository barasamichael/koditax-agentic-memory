"""
Phase C guardrail test suite — knowledge admin frontend integration boundary.

Asserts that the canonical integration document at
docs/integration/knowledge-admin-frontend-integration.md encodes the
orchestration-first end-user boundary, the admin-only direct-use rule, and
the shared-frontend authorization model. Tests fail deterministically if the
document drifts away from the intended product boundary.
"""

from pathlib import Path

import pytest

DOCUMENT_PATH = (
    Path(__file__).parent.parent
    / "docs"
    / "integration"
    / "knowledge-admin-frontend-integration.md"
)


@pytest.fixture(scope="module")
def document_text() -> str:
    assert DOCUMENT_PATH.exists(), (
        f"Integration document not found at {DOCUMENT_PATH}. "
        "Create docs/integration/knowledge-admin-frontend-integration.md."
    )
    return DOCUMENT_PATH.read_text(encoding="utf-8")


def test_document_exists() -> None:
    assert DOCUMENT_PATH.exists(), f"Required integration document missing: {DOCUMENT_PATH}"


# --- Required sections ---


def test_section_scope_exists(document_text: str) -> None:
    assert "## scope" in document_text, "Document must contain '## scope' section."


def test_section_product_boundary_exists(document_text: str) -> None:
    assert "## product_boundary" in document_text, (
        "Document must contain '## product_boundary' section."
    )


def test_section_end_user_integration_rule_exists(document_text: str) -> None:
    assert "## end_user_integration_rule" in document_text, (
        "Document must contain '## end_user_integration_rule' section."
    )


def test_section_admin_only_direct_routes_exists(document_text: str) -> None:
    assert "## admin_only_direct_routes" in document_text, (
        "Document must contain '## admin_only_direct_routes' section."
    )


def test_section_forbidden_public_frontend_routes_exists(document_text: str) -> None:
    assert "## forbidden_public_frontend_routes" in document_text, (
        "Document must contain '## forbidden_public_frontend_routes' section."
    )


def test_section_admin_workflows_exists(document_text: str) -> None:
    assert "## admin_workflows" in document_text, (
        "Document must contain '## admin_workflows' section."
    )


def test_section_auth_and_role_model_exists(document_text: str) -> None:
    assert "## auth_and_role_model" in document_text, (
        "Document must contain '## auth_and_role_model' section."
    )


def test_section_shared_frontend_authorization_model_exists(document_text: str) -> None:
    assert "## shared_frontend_authorization_model" in document_text, (
        "Document must contain '## shared_frontend_authorization_model' section."
    )


def test_section_non_technical_workflow_requirements_exists(document_text: str) -> None:
    assert "## non_technical_workflow_requirements" in document_text, (
        "Document must contain '## non_technical_workflow_requirements' section."
    )


def test_section_route_mapping_reference_exists(document_text: str) -> None:
    assert "## route_mapping_reference" in document_text, (
        "Document must contain '## route_mapping_reference' section."
    )


def test_section_integration_rules_exists(document_text: str) -> None:
    assert "## integration_rules" in document_text, (
        "Document must contain '## integration_rules' section."
    )


# --- Orchestration-first end-user rule ---


def test_orchestration_first_rule_stated(document_text: str) -> None:
    assert "orchestration" in document_text.lower(), (
        "Document must mention 'orchestration' as the end-user integration boundary."
    )


def test_end_user_goes_through_orchestration(document_text: str) -> None:
    text = document_text.lower()
    assert "end-user" in text or "end user" in text, (
        "Document must describe end-user knowledge interaction."
    )
    assert "orchestration" in text, (
        "Document must state that end-user knowledge interaction goes through orchestration."
    )


def test_public_frontend_must_not_call_knowledge_directly(document_text: str) -> None:
    text = document_text.lower()
    assert "public frontend must not call knowledge routes directly" in text or (
        "must not call" in text and "knowledge" in text and "directly" in text
    ), (
        "Document must explicitly state that the public frontend must not call "
        "Knowledge routes directly."
    )


# --- Admin-only direct Knowledge rule ---


def test_direct_knowledge_integration_is_admin_only(document_text: str) -> None:
    text = document_text.lower()
    assert "admin" in text and "direct" in text, (
        "Document must state that direct Knowledge integration is for admins only."
    )
    assert "administrator" in text, "Document must reference the 'Administrator' role."


def test_admin_only_route_protection_stated(document_text: str) -> None:
    text = document_text.lower()
    assert "x-auth-context" in text or "auth-context" in text, (
        "Document must reference the X-Auth-Context authorization header for protected routes."
    )
    assert "administrator" in text, (
        "Document must state that Knowledge routes require Administrator role."
    )


# --- Forbidden public route guidance ---


def test_forbidden_public_routes_guidance_present(document_text: str) -> None:
    text = document_text.lower()
    assert "forbidden" in text or "must not" in text, (
        "Document must contain forbidden-route or must-not guidance."
    )
    assert "/knowledge/search" in document_text or "knowledge/search" in document_text, (
        "Document must reference the /knowledge/search route in its forbidden or protected context."
    )


def test_forbidden_routes_include_retrieval_routes(document_text: str) -> None:
    assert "/knowledge/retrieve" in document_text or "knowledge/retrieve" in document_text, (
        "Document must reference the /knowledge/retrieve route."
    )
    assert (
        "/knowledge/timeline/search" in document_text
        or "knowledge/timeline/search" in document_text
    ), "Document must reference the /knowledge/timeline/search route."


# --- Shared frontend authorization model ---


def test_shared_frontend_application_allowed(document_text: str) -> None:
    text = document_text.lower()
    assert "same frontend application" in text or "shared frontend" in text, (
        "Document must explicitly state that the same frontend application may host both surfaces."
    )


def test_shared_frontend_requires_role_gating(document_text: str) -> None:
    text = document_text.lower()
    assert "role" in text and ("guard" in text or "gate" in text or "gat" in text), (
        "Document must describe role-gating or route guards for the shared frontend."
    )


# --- Backend authorization as source of truth ---


def test_backend_authorization_is_authoritative(document_text: str) -> None:
    text = document_text.lower()
    assert "backend authorization" in text or "backend" in text, (
        "Document must state that backend authorization is the source of truth."
    )
    assert "source of truth" in text, (
        "Document must use the phrase 'source of truth' when describing authorization."
    )


def test_ui_hiding_alone_is_insufficient(document_text: str) -> None:
    text = document_text.lower()
    assert "hiding" in text or "ui hiding" in text or "insufficient" in text, (
        "Document must state that UI hiding alone is insufficient for access control."
    )


# --- Non-technical workflow guidance ---


def test_non_technical_workflow_guidance_present(document_text: str) -> None:
    text = document_text.lower()
    assert "novice" in text or "non-technical" in text, (
        "Document must contain non-technical workflow guidance suitable for a novice administrator."
    )
    assert "raw json" in text or "raw payload" in text or "json" in text, (
        "Document must state that admins must not be required to compose raw JSON payloads."
    )


def test_action_labels_present(document_text: str) -> None:
    required_labels = ["Upload", "Review", "Approve", "Reject", "Publish"]
    for label in required_labels:
        assert label in document_text, f"Document must include the admin action label '{label}'."


def test_archive_and_supersede_labels_present(document_text: str) -> None:
    assert "Supersede" in document_text, "Document must include the 'Supersede' admin action label."
    assert "Archive" in document_text, "Document must include the 'Archive' admin action label."


# --- Required admin workflow coverage ---


def test_upload_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "upload" in text, "Document must describe the Upload (ingestion) admin workflow."
    assert "ingestion" in text, "Document must describe the ingestion workflow."


def test_review_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "review" in text, "Document must describe the Review admin workflow."


def test_approve_reject_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "approve" in text or "approval" in text, (
        "Document must describe the Approve admin workflow."
    )
    assert "reject" in text or "rejection" in text, (
        "Document must describe the Reject admin workflow."
    )


def test_publish_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "publish" in text, "Document must describe the Publish admin workflow."


def test_metadata_correction_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "metadata" in text and ("correct" in text or "correction" in text), (
        "Document must describe the metadata correction admin workflow."
    )


def test_supersede_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "supersede" in text or "supersession" in text, (
        "Document must describe the Supersede admin workflow."
    )


def test_archive_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "archive" in text, "Document must describe the Archive admin workflow."


def test_inspect_details_workflow_covered(document_text: str) -> None:
    text = document_text.lower()
    assert "inspect" in text or "detail" in text, (
        "Document must describe the Inspect Details admin workflow."
    )


# --- Integration rules completeness ---


def test_integration_rules_cover_orchestration_boundary(document_text: str) -> None:
    text = document_text.lower()
    assert "end-user integration boundary" in text or (
        "end-user" in text and "boundary" in text and "orchestration" in text
    ), "Integration rules must state that orchestration is the end-user integration boundary."


def test_integration_rules_cover_admin_only_direct_use(document_text: str) -> None:
    text = document_text.lower()
    assert "admin-only" in text or ("admin" in text and "only" in text and "direct" in text), (
        "Integration rules must state that direct Knowledge use is admin-only."
    )


def test_no_public_knowledge_frontend_exposure_endorsed(document_text: str) -> None:
    text = document_text.lower()
    assert (
        "public direct" not in text
        or "does not claim public direct" in text
        or ("not" in text and "public" in text and "direct" in text)
    ), "Document must not endorse public direct Knowledge frontend access."
    assert "not a public" in text or "is not a public" in text or "must not" in text, (
        "Document must explicitly state that Knowledge is not a public browser-facing API surface."
    )
