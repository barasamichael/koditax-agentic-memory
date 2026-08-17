from __future__ import annotations

from pathlib import Path

NUMERIC_POLICY_PATH = Path("docs/phase-4/health-contribution/nhif_legacy_numeric_policy.md")

REQUIRED_SECTION_HEADINGS = [
    "## 1. Supported Version Windows",
    "## 2. Authority and Interpretation Rules",
    "## 3. Computation-Safe Rule Table",
    "## 4. Supported Rule-Pack Shape",
    "## 5. Explicit Out-of-Scope Items for the First NHIF Rule Pack",
    "## 6. Source Conflicts and Narrowly Bounded Ambiguities",
    "## 7. Sufficiency for Phase 4B-R Subtask 7",
]

REQUIRED_TABLE_HEADER = (
    "| Rule ID | Policy statement | Numeric value(s) | Effective period | "
    "Official source citation(s) | Implementation relevance | Ambiguity note |"
)

VALID_RELEVANCE = {
    "version binding",
    "computation",
    "filing; computation",
    "computation; validation",
}


def _read_text() -> str:
    return NUMERIC_POLICY_PATH.read_text(encoding="utf-8")


def _extract_rule_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line != REQUIRED_TABLE_HEADER:
            continue

        assert index + 1 < len(lines), "Rule table is missing the separator row."
        assert lines[index + 1].startswith(
            "| --- |"
        ), "Rule table separator row must follow the required header."

        rows: list[list[str]] = []
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].startswith("|"):
            cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
            rows.append(cells)
            row_index += 1
        return rows

    raise AssertionError("Required NHIF numeric policy table header was not found.")


def test_nhif_numeric_policy_exists_and_has_required_sections() -> None:
    assert NUMERIC_POLICY_PATH.exists(), "NHIF legacy numeric policy artifact must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_nhif_numeric_policy_rule_rows_are_well_formed() -> None:
    text = _read_text()
    rows = _extract_rule_rows(text)

    assert rows, "NHIF numeric policy table must contain at least one data row."

    rule_ids: list[str] = []
    required_rule_ids = {
        "HC-NHIF-NPOL-0001",
        "HC-NHIF-NPOL-2010-001",
        "HC-NHIF-NPOL-2015-001",
        "HC-NHIF-NPOL-2021-001",
        "HC-NHIF-NPOL-2022-001",
        "HC-NHIF-NPOL-9001",
    }

    for cells in rows:
        assert len(cells) == 7, "Every rule row must include exactly 7 metadata columns."

        rule_id = cells[0]
        numeric_values = cells[2]
        effective_period = cells[3]
        citations = cells[4]
        relevance = cells[5]

        assert rule_id.startswith(
            "HC-NHIF-NPOL-"
        ), "Each NHIF numeric rule must use the HC-NHIF-NPOL- prefix."
        assert numeric_values not in {
            "",
            "None.",
        }, "Every NHIF numeric rule must declare numeric values or identifiers."
        assert effective_period not in {
            "",
            "None.",
        }, "Every NHIF numeric rule must declare an effective period."
        assert citations not in {"", "None."}, "Every NHIF numeric rule must declare citations."
        assert "HC-" in citations, "NHIF numeric citations must reference committed HC anchors."
        assert relevance in VALID_RELEVANCE, f"Invalid implementation relevance value: {relevance}"

        rule_ids.append(rule_id)

    assert len(rule_ids) == len(set(rule_ids)), "Duplicate NHIF numeric rule IDs are not allowed."
    assert rule_ids == sorted(rule_ids), "NHIF numeric rule IDs must remain sorted."
    assert required_rule_ids.issubset(
        set(rule_ids)
    ), "Required NHIF numeric policy records are missing from the artifact."


def test_nhif_numeric_policy_section_order_is_stable() -> None:
    text = _read_text()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required NHIF numeric section ordering must be stable."
