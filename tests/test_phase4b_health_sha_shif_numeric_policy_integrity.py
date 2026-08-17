from __future__ import annotations

from pathlib import Path

NUMERIC_POLICY_PATH = Path("docs/phase-4/health-contribution/sha_shif_numeric_policy_extraction.md")

REQUIRED_SECTION_HEADINGS = [
    "## 1. Candidate Active Windows and Readiness Verdicts",
    "## 2. Authority and Interpretation Rules",
    "## 3. Computation-Safe Rule Table",
    "## 4. Supported SHA/SHIF Rule-Pack Shape",
    "## 5. Explicit Out-of-Scope Items for the First SHA/SHIF Rule Pack",
    "## 6. Source Conflicts and Narrowly Bounded Ambiguities",
    "## 7. Sufficiency for Phase 4B-R Subtask 8",
]

WINDOW_TABLE_HEADER = (
    "| Version ID | Effective period | Contributor lane(s) | Numeric policy basis | "
    "Means-testing parameter status | Computation-safety verdict | Notes |"
)

RULE_TABLE_HEADER = (
    "| Rule ID | Policy statement | Numeric value(s) | Effective period | "
    "Official source citation(s) | Implementation relevance | Ambiguity note |"
)

VALID_VERDICTS = {
    "governed_boundary_only",
    "partially_specified",
    "implementation_ready",
}

VALID_RELEVANCE = {
    "version binding",
    "computation",
    "computation; filing",
    "computation; validation",
}


def _read_text() -> str:
    return NUMERIC_POLICY_PATH.read_text(encoding="utf-8")


def _extract_table_rows(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line != header:
            continue

        assert index + 1 < len(lines), "Required table is missing the separator row."
        assert lines[index + 1].startswith(
            "| --- |"
        ), "Table separator row must follow the required header."

        rows: list[list[str]] = []
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].startswith("|"):
            cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
            rows.append(cells)
            row_index += 1
        return rows

    raise AssertionError(f"Required table header was not found: {header}")


def test_sha_numeric_policy_exists_and_has_required_sections() -> None:
    assert NUMERIC_POLICY_PATH.exists(), "SHA/SHIF numeric policy artifact must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_sha_numeric_policy_window_rows_are_well_formed() -> None:
    text = _read_text()
    rows = _extract_table_rows(text, WINDOW_TABLE_HEADER)

    assert rows, "SHA/SHIF numeric policy must contain at least one candidate window row."

    window_ids: list[str] = []
    expected_verdicts = {
        "HCH-VER-20241001-A": "implementation_ready",
        "HCH-VER-20250228-PIT": "implementation_ready",
        "HCH-VER-20240920-AMD": "governed_boundary_only",
        "HCH-VER-20250228-AMD": "governed_boundary_only",
    }

    for cells in rows:
        assert len(cells) == 7, "Every window row must include exactly 7 metadata columns."

        version_id = cells[0]
        numeric_basis = cells[3]
        means_testing_status = cells[4]
        verdict = cells[5]

        assert version_id.startswith(
            "HCH-VER-"
        ), "Each candidate window must use the HCH-VER- prefix."
        assert numeric_basis not in {
            "",
            "None.",
        }, "Every window row must declare numeric policy basis."
        assert means_testing_status not in {
            "",
            "None.",
        }, "Every window row must declare means-testing parameter status."
        assert verdict in VALID_VERDICTS, f"Invalid computation-safety verdict: {verdict}"

        window_ids.append(version_id)

    assert len(window_ids) == len(set(window_ids)), "Duplicate SHA/SHIF window IDs are not allowed."

    verdict_map = {cells[0]: cells[5] for cells in rows}
    for version_id, verdict in expected_verdicts.items():
        assert (
            verdict_map.get(version_id) == verdict
        ), f"Unexpected readiness verdict for {version_id}."


def test_sha_numeric_policy_rule_rows_are_well_formed() -> None:
    text = _read_text()
    rows = _extract_table_rows(text, RULE_TABLE_HEADER)

    assert rows, "SHA/SHIF numeric policy table must contain at least one rule row."

    rule_ids: list[str] = []
    required_rule_ids = {
        "HC-SHI-NPOL-0001",
        "HC-SHI-NPOL-2024-001",
        "HC-SHI-NPOL-2024-002",
        "HC-SHI-NPOL-2024-003",
        "HC-SHI-NPOL-2025-001",
        "HC-SHI-NPOL-2025-002",
        "HC-SHI-NPOL-2025-003",
        "HC-SHI-NPOL-9001",
    }

    for cells in rows:
        assert len(cells) == 7, "Every SHA/SHIF rule row must include exactly 7 metadata columns."

        rule_id = cells[0]
        numeric_values = cells[2]
        effective_period = cells[3]
        citations = cells[4]
        relevance = cells[5]

        assert rule_id.startswith(
            "HC-SHI-NPOL-"
        ), "Each SHA/SHIF numeric rule must use the HC-SHI-NPOL- prefix."
        assert numeric_values not in {
            "",
            "None.",
        }, "Every SHA/SHIF numeric rule must declare numeric values or identifiers."
        assert effective_period not in {
            "",
            "None.",
        }, "Every SHA/SHIF numeric rule must declare an effective period."
        assert citations not in {"", "None."}, "Every SHA/SHIF numeric rule must declare citations."
        assert "HC-" in citations, "SHA/SHIF numeric citations must reference committed HC anchors."
        assert relevance in VALID_RELEVANCE, f"Invalid implementation relevance value: {relevance}"

        rule_ids.append(rule_id)

    assert len(rule_ids) == len(
        set(rule_ids)
    ), "Duplicate SHA/SHIF numeric rule IDs are not allowed."
    assert rule_ids == sorted(rule_ids), "SHA/SHIF numeric rule IDs must remain sorted."
    assert required_rule_ids.issubset(
        set(rule_ids)
    ), "Required SHA/SHIF numeric policy records are missing from the artifact."


def test_sha_numeric_policy_section_order_is_stable() -> None:
    text = _read_text()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(
        positions
    ), "Required SHA/SHIF numeric section ordering must be stable."
