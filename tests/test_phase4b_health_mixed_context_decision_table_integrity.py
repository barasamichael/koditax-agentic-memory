from __future__ import annotations

from pathlib import Path

DECISION_TABLE_PATH = Path("docs/phase-4/health-contribution/mixed_context_decision_table.md")
CANONICAL_POLICY_PATH = Path("docs/phase-4/health-contribution/canonical_policy_specification.md")
MATRIX_PATH = Path("docs/phase-4/health-contribution/historical_version_matrix.md")

REQUIRED_SECTION_HEADINGS = [
    "## Search Target Classes",
    "## Exact Official Sources Checked",
    "## Mixed-Context Decision Table",
    "## Final Authority Verdicts",
    "## Governed Outcome Rules",
]

SOURCE_TABLE_HEADER = (
    "| Source check ID | Target class ID | Official source checked | Source ID | "
    "Authority classification | Exact result | Supplies exact mixed-context normalization rule? |"
)

DECISION_TABLE_HEADER = (
    "| Mixed-context ID | Factual pattern summary | Exact official sources checked | "
    "Authority classification basis | Exact authority verdict | Governed outcome | "
    "Promotion impact |"
)

VERDICT_TABLE_HEADER = (
    "| Verdict ID | Mixed-context ID | Authority status | Deterministic authority decision | "
    "Promotion impact | Notes |"
)

VALID_AUTHORITY_CLASSIFICATIONS = {"binding_law", "official_admin"}
VALID_GOVERNED_OUTCOMES = {"fail_closed", "computation_safe"}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_rows(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == header:
            assert index + 1 < len(lines), f"Table after {header} is missing the separator row."
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


def test_mixed_context_decision_table_exists_and_has_required_sections() -> None:
    assert DECISION_TABLE_PATH.exists(), "Mixed-context decision table file must exist."

    text = _read_text(DECISION_TABLE_PATH)
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_source_checks_and_decision_rows_are_well_formed_and_stable() -> None:
    text = _read_text(DECISION_TABLE_PATH)
    source_rows = _extract_rows(text, SOURCE_TABLE_HEADER)
    decision_rows = _extract_rows(text, DECISION_TABLE_HEADER)
    verdict_rows = _extract_rows(text, VERDICT_TABLE_HEADER)

    assert len(source_rows) == 7, "The mixed-context source-check table drifted unexpectedly."
    assert (
        len(decision_rows) == 4
    ), "The mixed-context decision table must contain 4 candidate rows."
    assert len(verdict_rows) == 4, "The mixed-context verdict table must contain 4 verdict rows."

    source_check_ids: list[str] = []
    for cells in source_rows:
        assert len(cells) == 7, "Each source-check row must contain 7 metadata columns."
        source_check_id = cells[0]
        target_class_id = cells[1]
        source_id = cells[3]
        authority_classification = cells[4]
        exact_result = cells[5]
        exact_schedule_flag = cells[6]

        assert source_check_id.startswith(
            "HC-MCTX-ISRC-"
        ), "Source check IDs must use the HC-MCTX-ISRC- prefix."
        assert target_class_id.startswith(
            "HC-MCTX-ITGT-"
        ), "Target class IDs must use the HC-MCTX-ITGT- prefix."
        assert source_id.startswith(
            "HC-"
        ), "Every source-check row must cite a committed HC source ID."
        assert (
            authority_classification in VALID_AUTHORITY_CLASSIFICATIONS
        ), f"Invalid authority classification: {authority_classification}"
        assert exact_result not in {
            "",
            "None.",
        }, "Every source-check row must record an exact result."
        assert (
            exact_schedule_flag == "no"
        ), "No checked source may claim an exact mixed-context normalization rule in this pass."
        source_check_ids.append(source_check_id)

    assert source_check_ids == sorted(
        source_check_ids
    ), "Source-check rows must remain sorted by Source check ID."
    assert len(source_check_ids) == len(
        set(source_check_ids)
    ), "Duplicate mixed-context source-check IDs are not allowed."

    decision_row_map: dict[str, list[str]] = {}
    for cells in decision_rows:
        assert len(cells) == 7, "Each decision-table row must contain 7 metadata columns."
        mixed_context_id = cells[0]
        exact_official_sources_checked = cells[2]
        authority_classification_basis = cells[3]
        exact_authority_verdict = cells[4]
        governed_outcome = cells[5]
        promotion_impact = cells[6]

        assert mixed_context_id.startswith(
            "HC-MCTX-CMB-"
        ), "Decision-table rows must use the HC-MCTX-CMB- prefix."
        assert (
            "HC-MCTX-ISRC-" in exact_official_sources_checked
        ), "Decision-table rows must cite the exact checked source IDs."
        assert authority_classification_basis not in {
            "",
            "None.",
        }, "Decision-table rows must declare an authority classification basis."
        assert exact_authority_verdict not in {
            "",
            "None.",
        }, "Decision-table rows must declare an exact authority verdict."
        assert (
            governed_outcome in VALID_GOVERNED_OUTCOMES
        ), f"Invalid governed outcome: {governed_outcome}"
        assert promotion_impact not in {
            "",
            "None.",
        }, "Decision-table rows must declare a promotion impact."
        decision_row_map[mixed_context_id] = cells

    assert sorted(decision_row_map) == [
        "HC-MCTX-CMB-0001",
        "HC-MCTX-CMB-0002",
        "HC-MCTX-CMB-0003",
        "HC-MCTX-CMB-0004",
    ], "The governed mixed-context candidate set drifted unexpectedly."

    verdict_ids: list[str] = []
    verdict_row_map: dict[str, list[str]] = {}
    for cells in verdict_rows:
        assert len(cells) == 6, "Each verdict-table row must contain 6 metadata columns."
        verdict_id = cells[0]
        mixed_context_id = cells[1]
        authority_status = cells[2]
        deterministic_authority_decision = cells[3]
        promotion_impact = cells[4]

        assert verdict_id.startswith(
            "HC-MCTX-IVER-"
        ), "Verdict IDs must use the HC-MCTX-IVER- prefix."
        assert (
            mixed_context_id in decision_row_map
        ), f"Verdict row references an unknown mixed-context ID: {mixed_context_id}"
        assert authority_status not in {
            "",
            "None.",
        }, "Verdict rows must declare an authority status."
        assert deterministic_authority_decision not in {
            "",
            "None.",
        }, "Verdict rows must declare the deterministic authority decision."
        assert promotion_impact not in {
            "",
            "None.",
        }, "Verdict rows must declare a promotion impact."

        verdict_ids.append(verdict_id)
        verdict_row_map[verdict_id] = cells

    assert verdict_ids == sorted(verdict_ids), "Verdict rows must remain sorted by Verdict ID."
    assert sorted(verdict_row_map) == [
        "HC-MCTX-IVER-9001",
        "HC-MCTX-IVER-9002",
        "HC-MCTX-IVER-9003",
        "HC-MCTX-IVER-9004",
    ], "The governed mixed-context verdict set drifted unexpectedly."

    for mixed_context_id, cells in decision_row_map.items():
        assert (
            cells[5] == "fail_closed"
        ), f"{mixed_context_id} must remain fail_closed without exact official authority."


def test_decision_table_preserves_fail_closed_promotion_discipline() -> None:
    decision_text = _read_text(DECISION_TABLE_PATH)
    canonical_text = _read_text(CANONICAL_POLICY_PATH)
    matrix_text = _read_text(MATRIX_PATH)

    assert "No mixed-context combination is promoted to `implementation_ready` in this pass." in (
        decision_text
    ), "The decision table must state the current fail-closed promotion result explicitly."
    assert (
        "HCP-POL-304" in decision_text
    ), "The decision table must keep HCP-POL-304 explicit while mixed contexts remain unresolved."
    assert (
        "HCP-POL-U03" in decision_text
    ), "The decision table must keep HCP-POL-U03 explicit for exemption-dependent combinations."
    assert (
        "HC-MCTX-IVER-9001" in canonical_text
    ), "Canonical policy must cite the mixed-context authority verdicts."
    assert (
        "mixed_context_decision_table.md" in matrix_text
    ), "Historical version matrix must reference the mixed-context decision table."


def test_mixed_context_decision_table_section_order_is_stable() -> None:
    text = _read_text(DECISION_TABLE_PATH)
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
