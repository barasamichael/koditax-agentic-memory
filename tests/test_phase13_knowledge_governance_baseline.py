from __future__ import annotations

from pathlib import Path

SOURCE_TAXONOMY_PATH = Path("docs/governance/phase-13-knowledge-source-taxonomy.md")
AUTHORITY_POLICY_PATH = Path(
    "docs/governance/phase-13-knowledge-authority-and-temporal-ranking-policy.md"
)
PUBLICATION_STATE_PATH = Path("docs/governance/phase-13-knowledge-publication-state-model.md")
LINEAGE_CONTRACT_PATH = Path("docs/governance/phase-13-knowledge-lineage-contract.md")
ERROR_TAXONOMY_PATH = Path("docs/governance/phase-13-knowledge-canonical-error-taxonomy.md")
KNOWLEDGE_ARCH_PATH = Path("docs/architecture/Knowledge Service Architecture.md")
DATA_SCHEMA_PATH = Path("docs/architecture/Data Schema Specification.md")

TAXONOMY_HEADINGS = [
    "## Governing Rules",
    "## Controlled Source Classes",
    "## Inclusion Rules",
    "## Exclusion Rules",
    "## Shared-Corpus Boundary",
    "## Source-Version Form Rules",
    "## Normalization Mapping Contract",
]
AUTHORITY_HEADINGS = [
    "## Governing Rules",
    "## Authority Ordering",
    "## Retrieval Precedence",
    "## Effective-Window Applicability Rules",
    "## Supersession Rules",
    "## Same-Family Point-in-Time Version Rules",
    "## Cross-Period Query Rules",
    "## Prohibited Selection Modes",
]
PUBLICATION_HEADINGS = [
    "## Governing Rules",
    "## Publication State Enum",
    "## Allowed State Transitions",
    "## Searchability and Editability Rules",
    "## Supersede Archive and Reject Semantics",
    "## Fail-Closed Production Rules",
]
LINEAGE_HEADINGS = [
    "## Governing Rules",
    "## Allowed Source Input Origins",
    "## Lineage Layers",
    "## Required Lineage Fields",
    "## File-Backed and URL-Backed Provenance Rules",
    "## Publication Event Linkage",
    "## Anchor and Chunk Derivation Rules",
    "## Cardinality Rules",
    "## Fail-Closed Lineage Rules",
]
ERROR_HEADINGS = [
    "## Governing Rules",
    "## Error Envelope Contract",
    "## Error Families",
    "## Controlled Error Codes",
    "## Workflow Stage Applicability",
    "## Retryability Rules",
    "## Bulk Operation Error Semantics",
    "## Fail-Closed Production Rules",
]

SOURCE_CLASS_TABLE_HEADER = (
    "| Source class | Canonical authority level | Definition | Inclusion rules | "
    "Exclusion rules | Shared corpus eligible |"
)
SOURCE_VERSION_FORM_TABLE_HEADER = "| Source-version form | Definition | Allowed source classes |"
NORMALIZATION_MAPPING_HEADER = (
    "| Input origin | Shared corpus outcome | Required normalized record fields |"
)
AUTHORITY_ORDERING_HEADER = "| Rank | Authority level | Source class coverage |"
RETRIEVAL_PRECEDENCE_HEADER = "| Step | Deterministic selector | Rule |"
EFFECTIVE_WINDOW_HEADER = "| Rule ID | Governed rule |"
CROSS_PERIOD_HEADER = "| Query shape | Governed retrieval rule |"
PUBLICATION_STATE_HEADER = "| Publication state | Searchable | Editable | Required approvals |"
TRANSITION_HEADER = "| From state | Allowed to |"
SOURCE_INPUT_ORIGIN_HEADER = (
    "| Source input origin | Shared-corpus eligible | Provenance basis | Notes |"
)
LINEAGE_LAYER_HEADER = "| Lineage layer | Record type | Parent lineage layer | Stable lineage key |"
REQUIRED_LINEAGE_FIELDS_HEADER = (
    "| Lineage layer | Required lineage fields | Immutable after publication |"
)
ERROR_ENVELOPE_HEADER = "| Field | Requirement |"
ERROR_FAMILY_HEADER = "| Error family | Purpose |"
CONTROLLED_ERROR_HEADER = (
    "| Error family | Error code | Reason / reason_code | Workflow stage | Retryability |"
)
WORKFLOW_STAGE_HEADER = "| Workflow stage | Required error families |"

EXPECTED_SOURCE_CLASSES = ["tax_law", "regulation", "guidance", "commentary"]
EXPECTED_AUTHORITY_ORDER = ["statute", "regulation", "guidance", "commentary"]
EXPECTED_SOURCE_VERSION_FORMS = ["as_issued", "point_in_time_consolidation"]
EXPECTED_PUBLICATION_STATES = [
    "draft",
    "review_pending",
    "approved",
    "published",
    "superseded",
    "archived",
    "rejected",
]
EXPECTED_SOURCE_INPUT_ORIGINS = [
    "official_source_upload",
    "official_source_url",
    "customer_uploaded_document",
]
EXPECTED_LINEAGE_LAYERS = [
    "source_input",
    "source_family",
    "source_version",
    "anchor",
    "chunk",
]
EXPECTED_ERROR_FAMILIES = [
    "request_validation",
    "scope_and_classification",
    "lineage_and_provenance",
    "publication_and_review_state",
    "retrieval_safety",
    "conflict_and_idempotency",
    "bulk_operation",
]
EXPECTED_CONTROLLED_ERROR_CODES = [
    "invalid_knowledge_request",
    "unsupported_source_input_origin",
    "unsupported_source_class",
    "unsupported_knowledge_scope",
    "invalid_authority_source_class_binding",
    "invalid_effective_window_metadata",
    "invalid_knowledge_lineage",
    "forbidden_customer_document_lineage",
    "invalid_publication_state_transition",
    "knowledge_publication_safety_rejected",
    "knowledge_record_not_published",
    "knowledge_temporal_scope_mismatch",
    "knowledge_supersession_conflict",
    "knowledge_idempotency_conflict",
    "knowledge_bulk_operation_partial_failure",
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_table_rows(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == header:
            assert index + 1 < len(lines), f"Missing separator row after table: {header}"
            assert lines[index + 1].startswith(
                "| --- |"
            ), f"Table separator row must follow the header: {header}"
            rows: list[list[str]] = []
            row_index = index + 2
            while row_index < len(lines) and lines[row_index].startswith("|"):
                rows.append(
                    [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
                )
                row_index += 1
            return rows
    raise AssertionError(f"Required table header was not found: {header}")


def _strip_code_ticks(value: str) -> str:
    return value.strip().strip("`")


def test_governance_docs_exist_and_use_stable_headings() -> None:
    for path, headings in [
        (SOURCE_TAXONOMY_PATH, TAXONOMY_HEADINGS),
        (AUTHORITY_POLICY_PATH, AUTHORITY_HEADINGS),
        (PUBLICATION_STATE_PATH, PUBLICATION_HEADINGS),
        (LINEAGE_CONTRACT_PATH, LINEAGE_HEADINGS),
        (ERROR_TAXONOMY_PATH, ERROR_HEADINGS),
    ]:
        assert path.exists(), f"Required governance document is missing: {path}"
        text = _read_text(path)
        for heading in headings:
            assert heading in text, f"Missing required section heading {heading} in {path}"
        positions = [text.index(heading) for heading in headings]
        assert positions == sorted(positions), f"Required section ordering drifted in {path.name}."


def test_source_taxonomy_uses_exact_controlled_classes_and_no_catch_alls() -> None:
    text = _read_text(SOURCE_TAXONOMY_PATH)
    class_rows = _extract_table_rows(text, SOURCE_CLASS_TABLE_HEADER)
    version_form_rows = _extract_table_rows(text, SOURCE_VERSION_FORM_TABLE_HEADER)
    mapping_rows = _extract_table_rows(text, NORMALIZATION_MAPPING_HEADER)

    assert len(class_rows) == 4, "Source taxonomy must define exactly four governed classes."
    extracted_source_classes: list[str] = []
    extracted_authority_levels: list[str] = []
    for cells in class_rows:
        assert len(cells) == 6, "Each source-class row must contain 6 metadata columns."
        source_class = _strip_code_ticks(cells[0])
        authority_level = _strip_code_ticks(cells[1])
        shared_corpus_eligible = _strip_code_ticks(cells[5])

        extracted_source_classes.append(source_class)
        extracted_authority_levels.append(authority_level)
        assert shared_corpus_eligible == "yes", (
            "All governed source classes must remain explicitly eligible for shared-corpus "
            "publication once reviewed and approved."
        )

    assert (
        extracted_source_classes == EXPECTED_SOURCE_CLASSES
    ), "Source taxonomy must keep the controlled source-class enum exact and ordered."
    assert (
        extracted_authority_levels == EXPECTED_AUTHORITY_ORDER
    ), "Source taxonomy must keep the controlled class-to-authority mapping exact."

    extracted_version_forms = [
        _strip_code_ticks(cells[0]) for cells in version_form_rows if len(cells) == 3
    ]
    assert (
        extracted_version_forms == EXPECTED_SOURCE_VERSION_FORMS
    ), "Source-version form rules must stay exact and ordered."
    assert (
        "Consolidated point-in-time legal text is therefore represented as "
        "`source_version_form = point_in_time_consolidation`, never as a separate "
        "`source_class`." in text
    ), "The taxonomy must make the consolidated-text modeling decision explicit."
    assert "Customer-uploaded documents can never enter the shared knowledge corpus." in text, (
        "Shared-corpus boundary must explicitly forbid customer uploads from entering the "
        "shared corpus."
    )
    assert len(mapping_rows) == 3, "Normalization mapping contract must contain 3 fixed rows."
    assert (
        _strip_code_ticks(mapping_rows[2][0]) == "customer_uploaded_document"
    ), "The normalization mapping contract must keep the customer-uploaded-document row."
    assert (
        _strip_code_ticks(mapping_rows[2][1]) == "never_shared_corpus"
    ), "Customer-uploaded documents must remain explicitly outside the shared corpus."

    forbidden_tokens = ["`other`", "`misc`", "`unknown`", "`general_reference`"]
    for token in forbidden_tokens:
        assert token in text, f"Source taxonomy must forbid catch-all class token {token}."


def test_authority_and_temporal_policy_is_total_and_defines_effective_windows() -> None:
    text = _read_text(AUTHORITY_POLICY_PATH)
    authority_rows = _extract_table_rows(text, AUTHORITY_ORDERING_HEADER)
    precedence_rows = _extract_table_rows(text, RETRIEVAL_PRECEDENCE_HEADER)
    effective_window_rows = _extract_table_rows(text, EFFECTIVE_WINDOW_HEADER)
    supersession_rows = _extract_table_rows(text, "| Rule ID | Governed rule |")
    cross_period_rows = _extract_table_rows(text, CROSS_PERIOD_HEADER)

    assert len(authority_rows) == 4, "Authority ordering must define all four authority levels."
    extracted_authorities = [_strip_code_ticks(cells[1]) for cells in authority_rows]
    assert (
        extracted_authorities == EXPECTED_AUTHORITY_ORDER
    ), "Authority ordering must remain total, exact, and ordered."

    expected_selectors = [
        "publication_state",
        "tax_domain",
        "source_class",
        "effective_window",
        "authority_level",
        "supersession_status",
        "same_family_source_version_form",
        "lexical_or_vector_ranking",
    ]
    extracted_selectors = [_strip_code_ticks(cells[1]) for cells in precedence_rows]
    assert (
        extracted_selectors == expected_selectors
    ), "Retrieval precedence must remain exact and ordered."

    effective_window_rule_ids = [_strip_code_ticks(cells[0]) for cells in effective_window_rows]
    assert effective_window_rule_ids == [
        "KAT-001",
        "KAT-002",
        "KAT-003",
        "KAT-004",
        "KAT-005",
    ], "Temporal applicability rules must remain explicit and complete."

    assert (
        "Vector-only selection is prohibited." in text
    ), "Authority policy must explicitly forbid vector-only selection."
    assert (
        "Similarity-led retrieval without explicit temporal and authority controls is prohibited."
        in text
    ), "Authority policy must explicitly forbid similarity-led temporal guessing."

    assert len(cross_period_rows) == 3, (
        "Cross-period query rules must keep the fixed single_year/multi_year_range/"
        "change_over_time set."
    )
    extracted_query_shapes = [_strip_code_ticks(cells[0]) for cells in cross_period_rows]
    assert extracted_query_shapes == [
        "single_year",
        "multi_year_range",
        "change_over_time",
    ], "Cross-period query rules must remain exact and ordered."

    assert len(supersession_rows) >= 5, (
        "Authority and temporal policy must define governed rule tables instead of "
        "narrative-only guidance."
    )


def test_publication_state_model_fail_closes_unpublished_records() -> None:
    text = _read_text(PUBLICATION_STATE_PATH)
    state_rows = _extract_table_rows(text, PUBLICATION_STATE_HEADER)
    transition_rows = _extract_table_rows(text, TRANSITION_HEADER)

    assert len(state_rows) == len(
        EXPECTED_PUBLICATION_STATES
    ), "Publication state model must keep the fixed governed state enum."
    extracted_states = [_strip_code_ticks(cells[0]) for cells in state_rows]
    assert (
        extracted_states == EXPECTED_PUBLICATION_STATES
    ), "Publication states must remain exact and ordered."

    searchability = {
        _strip_code_ticks(cells[0]): _strip_code_ticks(cells[1]) for cells in state_rows
    }
    editability = {_strip_code_ticks(cells[0]): _strip_code_ticks(cells[2]) for cells in state_rows}

    assert searchability["draft"] == "no", "Draft records must not be searchable."
    assert searchability["review_pending"] == "no", "Review-pending records must not be searchable."
    assert searchability["published"] == "yes", "Published records must be searchable."
    assert (
        searchability["superseded"] == "yes"
    ), "Superseded records must remain searchable for historical retrieval."
    assert (
        editability["approved"] == "no"
    ), "Approved records must not be directly editable; they must return to draft first."

    expected_transitions = {
        "draft": "review_pending",
        "review_pending": "approved|draft|rejected",
        "approved": "draft|published|rejected",
        "published": "archived|superseded",
        "superseded": "archived",
        "rejected": "draft",
        "archived": "none",
    }
    for cells in transition_rows:
        from_state = _strip_code_ticks(cells[0])
        allowed_to = "|".join(
            sorted(_strip_code_ticks(part.strip()) for part in cells[1].split(","))
        )
        assert (
            expected_transitions[from_state] == allowed_to
        ), f"Unexpected transition set for publication state {from_state}."

    assert (
        "Direct lookup by identifier must fail closed for non-searchable states rather than "
        "leaking unpublished metadata." in text
    ), "Publication model must keep explicit fail-closed direct-retrieval behavior."


def test_lineage_contract_requires_governed_provenance_and_excludes_customer_uploads() -> None:
    text = _read_text(LINEAGE_CONTRACT_PATH)
    origin_rows = _extract_table_rows(text, SOURCE_INPUT_ORIGIN_HEADER)
    layer_rows = _extract_table_rows(text, LINEAGE_LAYER_HEADER)
    required_field_rows = _extract_table_rows(text, REQUIRED_LINEAGE_FIELDS_HEADER)

    extracted_origins = [_strip_code_ticks(cells[0]) for cells in origin_rows]
    assert (
        extracted_origins == EXPECTED_SOURCE_INPUT_ORIGINS
    ), "Lineage contract must keep the exact governed source-input-origin set."
    eligibility = {
        _strip_code_ticks(cells[0]): _strip_code_ticks(cells[1]) for cells in origin_rows
    }
    assert eligibility["official_source_upload"] == "yes"
    assert eligibility["official_source_url"] == "yes"
    assert (
        eligibility["customer_uploaded_document"] == "no"
    ), "Customer-uploaded documents must stay outside shared-corpus lineage."

    extracted_layers = [_strip_code_ticks(cells[0]) for cells in layer_rows]
    assert (
        extracted_layers == EXPECTED_LINEAGE_LAYERS
    ), "Lineage contract must keep the exact lineage-layer chain."

    immutable_flags = {
        _strip_code_ticks(cells[0]): _strip_code_ticks(cells[2]) for cells in required_field_rows
    }
    assert all(
        flag == "yes" for flag in immutable_flags.values()
    ), "All published lineage layers must remain immutable after publication."

    assert (
        "Every publishable shared-corpus knowledge record must trace to exactly one "
        "governed source-input origin." in text
    )
    assert (
        "`customer_uploaded_document` may support private evidence workflows in "
        "`document_ai`, but it is never eligible for shared `knowledge_sources` or "
        "`knowledge_source_versions`." in text
    )
    assert (
        "`point_in_time_consolidation` must preserve provenance to one governed "
        "source-input origin" in text
    )
    assert "If `source_input_origin` is missing, publication must fail closed." in text
    assert (
        "If `source_input_ref` is ambiguous, unverifiable, or broken, publication must "
        "fail closed." in text
    )


def test_error_taxonomy_uses_controlled_families_and_fail_closed_codes() -> None:
    text = _read_text(ERROR_TAXONOMY_PATH)
    envelope_rows = _extract_table_rows(text, ERROR_ENVELOPE_HEADER)
    family_rows = _extract_table_rows(text, ERROR_FAMILY_HEADER)
    controlled_rows = _extract_table_rows(text, CONTROLLED_ERROR_HEADER)
    workflow_rows = _extract_table_rows(text, WORKFLOW_STAGE_HEADER)

    assert len(envelope_rows) == 7, "Error envelope contract must keep the fixed 7-field shape."
    extracted_families = [_strip_code_ticks(cells[0]) for cells in family_rows]
    assert (
        extracted_families == EXPECTED_ERROR_FAMILIES
    ), "Error taxonomy must keep the exact controlled error families."

    extracted_codes = [_strip_code_ticks(cells[1]) for cells in controlled_rows]
    assert (
        extracted_codes == EXPECTED_CONTROLLED_ERROR_CODES
    ), "Error taxonomy must keep the exact controlled error-code set and ordering."

    workflow_stages = [_strip_code_ticks(cells[0]) for cells in workflow_rows]
    assert workflow_stages == [
        "ingestion",
        "review",
        "publication",
        "retrieval",
        "bulk_management",
    ], "Workflow stage applicability must keep the fixed stage set."

    assert (
        "unknown_error" not in extracted_codes
    ), "Vague catch-all error bucket `unknown_error` is forbidden."
    assert (
        "misc_failure" not in extracted_codes
    ), "Vague catch-all error bucket `misc_failure` is forbidden."
    assert (
        "Retrieval of `draft`, `review_pending`, `approved`, `archived`, or `rejected` "
        "records must fail with `knowledge_record_not_published`." in text
    ), "Unpublished retrieval must remain explicitly fail-closed."
    assert (
        "Mixed-success batches must return `knowledge_bulk_operation_partial_failure`." in text
    ), "Bulk-operation semantics must remain deterministic and explicit."


def test_architecture_docs_reference_the_governance_baseline_consistently() -> None:
    knowledge_arch_text = _read_text(KNOWLEDGE_ARCH_PATH)
    data_schema_text = _read_text(DATA_SCHEMA_PATH)

    for filename in [
        "phase-13-knowledge-source-taxonomy.md",
        "phase-13-knowledge-authority-and-temporal-ranking-policy.md",
        "phase-13-knowledge-publication-state-model.md",
        "phase-13-knowledge-lineage-contract.md",
        "phase-13-knowledge-canonical-error-taxonomy.md",
    ]:
        assert (
            filename in knowledge_arch_text
        ), "Knowledge Service Architecture must reference all new governance control docs."

    assert (
        "`source_class`" in knowledge_arch_text
    ), "Knowledge Service Architecture must align to the governed source-class terminology."
    assert (
        "`source_version_form`" in knowledge_arch_text
    ), "Knowledge Service Architecture must align to governed source-version-form terminology."
    assert (
        "publication_state IN ('published', 'superseded')" in knowledge_arch_text
    ), "Knowledge Service Architecture must align retrieval to the searchable publication states."
    assert (
        "`source_input_origin`" in knowledge_arch_text
    ), "Knowledge Service Architecture must align to the governed source-input-origin terminology."
    assert (
        "`publication_event_id`" in knowledge_arch_text
    ), "Knowledge Service Architecture must align to governed publication-event linkage."
    assert (
        "customer_uploaded_document" in knowledge_arch_text
    ), "Knowledge Service Architecture must keep the customer-upload exclusion explicit."
    assert (
        "canonical error taxonomy" in knowledge_arch_text.lower()
    ), "Knowledge Service Architecture must reference the controlled failure model."

    assert (
        "source_class VARCHAR NOT NULL" in data_schema_text
    ), "Data Schema Specification must align the knowledge source column to source_class."
    assert (
        "source_version_form VARCHAR NOT NULL" in data_schema_text
    ), "Data Schema Specification must align the knowledge version model to source_version_form."
    assert (
        "source_input_origin VARCHAR NOT NULL" in data_schema_text
    ), "Data Schema Specification must align the knowledge version model to source_input_origin."
    assert (
        "source_input_ref TEXT NOT NULL" in data_schema_text
    ), "Data Schema Specification must align the knowledge version model to source_input_ref."
    assert (
        "publication_event_id UUID REFERENCES audit_events(event_id)" in data_schema_text
    ), "Data Schema Specification must align the knowledge version model to publication_event_id."
    assert (
        "publication_state` constrained exactly to `draft`, `review_pending`, `approved`, "
        "`published`, `superseded`, `archived`, `rejected`." in data_schema_text
    ), "Data Schema Specification must keep the exact governed publication-state enum."
    assert (
        "`source_class` constrained exactly to `tax_law`, `regulation`, `guidance`, "
        "`commentary`." in data_schema_text
    ), "Data Schema Specification must keep the exact governed source-class enum."
    assert (
        "Vector-only selection is prohibited." in data_schema_text
    ), "Data Schema Specification must encode the prohibition on vector-only selection."
    assert (
        "`source_input_origin` constrained exactly to `official_source_upload`, "
        "`official_source_url`." in data_schema_text
    ), "Data Schema Specification must keep the exact governed source-input-origin enum."
    assert (
        "`customer_uploaded_document` is not a valid shared-corpus source-input origin."
        in data_schema_text
    ), "Data Schema Specification must keep the customer-upload lineage exclusion explicit."
    assert (
        "kra_guidance" not in data_schema_text
    ), "Data Schema Specification must not keep the old bootstrap-only kra_guidance class token."
