"""
Phase C guardrail test suite — knowledge admin dashboard specification.

Asserts that the canonical dashboard specification at
docs/integration/knowledge-admin-dashboard-spec.md encodes the required
information architecture, the shared-frontend authorization model, the novice-admin
usability rules, the real-user testing requirement, and the out-of-scope exclusions.
Tests fail deterministically if the document drifts away from the intended spec.
"""

from pathlib import Path

import pytest

SPEC_PATH = (
    Path(__file__).parent.parent / "docs" / "integration" / "knowledge-admin-dashboard-spec.md"
)

BOUNDARY_DOC_PATH = (
    Path(__file__).parent.parent
    / "docs"
    / "integration"
    / "knowledge-admin-frontend-integration.md"
)


@pytest.fixture(scope="module")
def spec_text() -> str:
    assert SPEC_PATH.exists(), (
        f"Dashboard spec not found at {SPEC_PATH}. "
        "Create docs/integration/knowledge-admin-dashboard-spec.md."
    )
    return SPEC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Document existence
# ---------------------------------------------------------------------------


def test_dashboard_spec_exists() -> None:
    assert SPEC_PATH.exists(), f"Required dashboard spec missing: {SPEC_PATH}"


def test_boundary_doc_still_exists() -> None:
    assert BOUNDARY_DOC_PATH.exists(), (
        f"C.1 boundary document missing at {BOUNDARY_DOC_PATH}. The dashboard spec depends on it."
    )


# ---------------------------------------------------------------------------
# Required sections
# ---------------------------------------------------------------------------


def test_section_scope_exists(spec_text: str) -> None:
    assert "## scope" in spec_text, "Spec must contain '## scope' section."


def test_section_dashboard_goal_exists(spec_text: str) -> None:
    assert "## dashboard_goal" in spec_text, "Spec must contain '## dashboard_goal' section."


def test_section_user_types_exists(spec_text: str) -> None:
    assert "## user_types" in spec_text, "Spec must contain '## user_types' section."


def test_section_navigation_structure_exists(spec_text: str) -> None:
    assert "## navigation_structure" in spec_text, (
        "Spec must contain '## navigation_structure' section."
    )


def test_section_approved_pages_exists(spec_text: str) -> None:
    assert "## approved_pages" in spec_text, "Spec must contain '## approved_pages' section."


def test_section_workflow_entry_points_exists(spec_text: str) -> None:
    assert "## workflow_entry_points" in spec_text, (
        "Spec must contain '## workflow_entry_points' section."
    )


def test_section_page_by_page_workflows_exists(spec_text: str) -> None:
    assert "## page_by_page_workflows" in spec_text, (
        "Spec must contain '## page_by_page_workflows' section."
    )


def test_section_backend_route_mapping_exists(spec_text: str) -> None:
    assert "## backend_route_mapping" in spec_text, (
        "Spec must contain '## backend_route_mapping' section."
    )


def test_section_authorization_and_visibility_rules_exists(spec_text: str) -> None:
    assert "## authorization_and_visibility_rules" in spec_text, (
        "Spec must contain '## authorization_and_visibility_rules' section."
    )


def test_section_testing_and_seed_user_guidance_exists(spec_text: str) -> None:
    assert "## testing_and_seed_user_guidance" in spec_text, (
        "Spec must contain '## testing_and_seed_user_guidance' section."
    )


def test_section_non_technical_ux_rules_exists(spec_text: str) -> None:
    assert "## non_technical_ux_rules" in spec_text, (
        "Spec must contain '## non_technical_ux_rules' section."
    )


def test_section_out_of_scope_exists(spec_text: str) -> None:
    assert "## out_of_scope" in spec_text, "Spec must contain '## out_of_scope' section."


def test_section_implementation_rules_exists(spec_text: str) -> None:
    assert "## implementation_rules" in spec_text, (
        "Spec must contain '## implementation_rules' section."
    )


# ---------------------------------------------------------------------------
# Shared-frontend model
# ---------------------------------------------------------------------------


def test_shared_frontend_application_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "shared frontend" in text or "same frontend application" in text, (
        "Spec must state that the admin dashboard lives in the shared frontend application."
    )


def test_admin_pages_hidden_for_non_admin_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "hidden" in text or "hide" in text, (
        "Spec must state that admin pages are hidden for non-administrator sessions."
    )
    assert "unauthorized" in text or "non-administrator" in text or "non-admin" in text, (
        "Spec must describe access restriction for non-administrator users."
    )


def test_backend_authorization_is_authoritative_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "backend authorization" in text or ("backend" in text and "authoritative" in text), (
        "Spec must state that backend authorization is authoritative."
    )
    assert "source of truth" in text or "authoritative" in text, (
        "Spec must use 'source of truth' or 'authoritative' language for backend authorization."
    )


def test_route_guards_are_defense_in_depth_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert (
        "defense-in-depth" in text or "defence-in-depth" in text or ("defense in depth" in text)
    ), "Spec must describe route guards and navigation hiding as defense-in-depth only."


# ---------------------------------------------------------------------------
# Navigation structure
# ---------------------------------------------------------------------------


def test_knowledge_base_nav_label_present(spec_text: str) -> None:
    assert "Knowledge Base" in spec_text, (
        "Spec must include 'Knowledge Base' as the top-level admin nav label."
    )


def test_incoming_items_nav_label_present(spec_text: str) -> None:
    assert "Incoming Items" in spec_text, (
        "Spec must include 'Incoming Items' as a navigation label."
    )


def test_review_queue_nav_label_present(spec_text: str) -> None:
    assert "Review Queue" in spec_text, "Spec must include 'Review Queue' as a navigation label."


def test_published_sources_nav_label_present(spec_text: str) -> None:
    assert "Published Sources" in spec_text, (
        "Spec must include 'Published Sources' as a navigation label."
    )


def test_source_details_nav_label_present(spec_text: str) -> None:
    assert "Source Details" in spec_text or "Source Library" in spec_text, (
        "Spec must include 'Source Details' or 'Source Library' as a navigation label."
    )


def test_lifecycle_actions_nav_label_present(spec_text: str) -> None:
    assert "Lifecycle Actions" in spec_text, (
        "Spec must include 'Lifecycle Actions' as a contextual navigation label."
    )


# ---------------------------------------------------------------------------
# Approved admin pages
# ---------------------------------------------------------------------------


def test_ingestion_queue_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "ingestion queue" in text or "incoming items" in text, (
        "Spec must define an ingestion queue or Incoming Items page."
    )


def test_upload_form_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "upload form" in text or "upload new" in text, (
        "Spec must define an upload form page or entry point."
    )


def test_ingestion_job_detail_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "ingestion job detail" in text or "job detail" in text, (
        "Spec must define an ingestion job detail page."
    )


def test_published_sources_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "published sources" in text, "Spec must define a Published Sources page."


def test_source_version_detail_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "source version detail" in text or "version detail" in text, (
        "Spec must define a source version detail page."
    )


def test_source_library_or_detail_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "source library" in text or "source detail" in text, (
        "Spec must define a Source Library or Source Detail page."
    )


def test_anchor_detail_page_defined(spec_text: str) -> None:
    text = spec_text.lower()
    assert "anchor detail" in text, "Spec must define an anchor detail page or view."


# ---------------------------------------------------------------------------
# Novice-admin workflow language
# ---------------------------------------------------------------------------


def test_upload_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "upload" in text and "ingestion" in text, "Spec must describe the Upload workflow."


def test_review_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "review" in text and "notes" in text, (
        "Spec must describe the Review workflow including recording notes."
    )


def test_approve_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "approve" in text or "approval" in text, "Spec must describe the Approve workflow."


def test_reject_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "reject" in text or "rejection" in text, "Spec must describe the Reject workflow."


def test_publish_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "publish" in text, "Spec must describe the Publish workflow."


def test_metadata_correction_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "correct metadata" in text or "metadata correction" in text, (
        "Spec must describe the Correct Metadata workflow."
    )


def test_supersede_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "supersede" in text or "supersession" in text, (
        "Spec must describe the Supersede workflow."
    )


def test_archive_workflow_described(spec_text: str) -> None:
    text = spec_text.lower()
    assert "archive" in text, "Spec must describe the Archive workflow."


def test_no_raw_json_requirement_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "raw json" in text or "no raw json" in text or "must not" in text, (
        "Spec must state that admins must not be required to compose raw JSON."
    )


def test_no_route_names_in_admin_ui_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "route name" in text or "route names" in text or "no route" in text, (
        "Spec must state that route names must not appear in admin-facing UI."
    )


def test_state_translation_table_present(spec_text: str) -> None:
    assert "review_pending" in spec_text or "approved_for_publication" in spec_text, (
        "Spec must include a state translation table mapping backend states to plain labels."
    )


def test_approver_publisher_separation_stated(spec_text: str) -> None:
    text = spec_text.lower()
    assert "approver" in text and "publisher" in text or ("different administrator" in text), (
        "Spec must describe the approver-publisher separation rule."
    )


# ---------------------------------------------------------------------------
# Testing guidance — real users required, AI users forbidden
# ---------------------------------------------------------------------------


def test_real_set_users_required(spec_text: str) -> None:
    text = spec_text.lower()
    assert "real" in text and ("seed user" in text or "set user" in text or "local" in text), (
        "Spec must require the use of real set users from local/PowerShell setup for testing."
    )


def test_powershell_or_local_setup_mentioned(spec_text: str) -> None:
    text = spec_text.lower()
    assert "powershell" in text or "local development" in text or "local setup" in text, (
        "Spec must reference PowerShell or local development setup for seed users."
    )


def test_ai_user_explicitly_forbidden(spec_text: str) -> None:
    text = spec_text.lower()
    assert "ai user" in text or "invented" in text or "fabricated" in text, (
        "Spec must explicitly mention and forbid the use of invented or AI users for testing."
    )
    assert "do not" in text or "must not" in text or "do not invent" in text, (
        "Spec must use prohibitive language against invented test personas."
    )


def test_setup_gap_callout_required(spec_text: str) -> None:
    text = spec_text.lower()
    assert "setup gap" in text or "call it out" in text or "gap" in text, (
        "Spec must state that a missing Administrator account is a setup gap to call out, "
        "not to patch with a fictional user."
    )


def test_two_person_workflow_testing_addressed(spec_text: str) -> None:
    text = spec_text.lower()
    assert (
        "two" in text
        and ("administrator" in text or "admin" in text)
        and "distinct" in text
        or "two-person" in text
        or ("different administrator" in text and "test" in text)
    ), (
        "Spec must address that testing the publish workflow requires two distinct "
        "Administrator accounts."
    )


# ---------------------------------------------------------------------------
# Out-of-scope exclusions
# ---------------------------------------------------------------------------


def test_public_knowledge_search_excluded(spec_text: str) -> None:
    text = spec_text.lower()
    assert "public knowledge search" in text or (
        "public" in text
        and "search" in text
        and ("not" in text or "out of scope" in text or "excluded" in text)
    ), "Spec must explicitly exclude public Knowledge search UI from scope."


def test_generic_user_management_excluded(spec_text: str) -> None:
    text = spec_text.lower()
    assert "user management" in text or "user-management" in text, (
        "Spec must explicitly exclude or limit generic user management console from scope."
    )


def test_infrastructure_console_excluded(spec_text: str) -> None:
    text = spec_text.lower()
    assert "infrastructure" in text and (
        "out of scope" in text or "not" in text or "excluded" in text
    ), "Spec must explicitly exclude infrastructure or platform operations consoles from scope."


def test_customer_documents_excluded_from_corpus(spec_text: str) -> None:
    text = spec_text.lower()
    assert "customer" in text and ("corpus" in text or "shared" in text or "not" in text), (
        "Spec must state that customer documents must not enter the shared knowledge corpus."
    )


def test_destructive_purge_excluded(spec_text: str) -> None:
    text = spec_text.lower()
    assert "purge" in text and ("not" in text or "out of scope" in text or "does not" in text), (
        "Spec must explicitly state that destructive purge is not available in this phase."
    )


def test_chunk_body_access_excluded(spec_text: str) -> None:
    text = spec_text.lower()
    assert "chunk" in text and ("summar" in text or "not exposed" in text or "only" in text), (
        "Spec must state that raw chunk bodies are not exposed; only summaries are shown."
    )


# ---------------------------------------------------------------------------
# Backend route mapping completeness spot-checks
# ---------------------------------------------------------------------------


def test_route_mapping_includes_ingestion_list(spec_text: str) -> None:
    assert "/knowledge/ingestion" in spec_text, (
        "Backend route mapping must include GET /knowledge/ingestion."
    )


def test_route_mapping_includes_approve(spec_text: str) -> None:
    assert "/approve" in spec_text, "Backend route mapping must include the approve route."


def test_route_mapping_includes_publish(spec_text: str) -> None:
    assert "/publish" in spec_text, "Backend route mapping must include the publish route."


def test_route_mapping_includes_supersede(spec_text: str) -> None:
    assert "/supersede" in spec_text, "Backend route mapping must include the supersede route."


def test_route_mapping_includes_archive(spec_text: str) -> None:
    assert "/archive" in spec_text, "Backend route mapping must include the archive route."


def test_route_mapping_includes_source_versions(spec_text: str) -> None:
    assert "/knowledge/source-versions" in spec_text, (
        "Backend route mapping must include /knowledge/source-versions."
    )


def test_route_mapping_includes_sources(spec_text: str) -> None:
    assert "/knowledge/sources" in spec_text, (
        "Backend route mapping must include /knowledge/sources."
    )


def test_route_mapping_includes_anchors(spec_text: str) -> None:
    assert "/knowledge/anchors/" in spec_text or "/knowledge/anchors/{" in spec_text, (
        "Backend route mapping must include the anchors detail route."
    )


# ---------------------------------------------------------------------------
# Orchestration boundary not weakened
# ---------------------------------------------------------------------------


def test_orchestration_still_the_end_user_boundary(spec_text: str) -> None:
    text = spec_text.lower()
    assert "orchestration" in text, (
        "Spec must reference orchestration as the end-user knowledge boundary."
    )
    assert "end-user" in text or "end user" in text, (
        "Spec must address end-user access separately from admin access."
    )


def test_admin_search_routes_not_user_product(spec_text: str) -> None:
    text = spec_text.lower()
    assert (
        "testing only" in text
        or "admin testing" in text
        or ("not" in text and "user-facing" in text)
    ), (
        "Spec must state that admin search/retrieve routes are for testing only, "
        "not for a user-facing search product."
    )
