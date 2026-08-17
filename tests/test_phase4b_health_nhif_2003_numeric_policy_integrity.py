from __future__ import annotations

from pathlib import Path

NUMERIC_POLICY_PATH = Path("docs/phase-4/health-contribution/nhif_2003_baseline_numeric_policy.md")

REQUIRED_SECTION_HEADINGS = [
    "## 1. Candidate Window and Readiness Verdict",
    "## 2. Authority and Interpretation Rules",
    "## 3. Computation-Safe Rule Table",
    "## 4. Readiness Decision for HCH-VER-20031205-A",
    "## 5. Explicit Blocking Ambiguities",
    "## 6. Sufficiency for Phase 4B-R Subtask 12",
]

WINDOW_TABLE_HEADER = (
    "| Version ID | Effective period | Source-proven schedule status | "
    "Source-proven remittance wording status | Numeric policy basis | "
    "Computation-safety verdict | Notes |"
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
    "computation; validation",
}

EXPECTED_2003_WINDOW_ROW = [
    "HCH-VER-20031205-A",
    "2003-12-05 to 2010-07-15",
    "exact original Regulation 3 text harvested; it states only the lower endpoint, upper cap, "
    "and that the rate is graduated",
    "exact original Regulation 5 wording harvested from the original 2003 text",
    "`HC-NHIF-2003-NPOL-0001`; `HC-NHIF-2003-NPOL-0002`; "
    "`HC-NHIF-2003-NPOL-0003`; `HC-NHIF-2003-NPOL-0004`; "
    "`HC-NHIF-2003-NPOL-0005`; "
    "`HC-NHIF-2003-NPOL-9001`",
    "partially_specified",
    "The original 2003 text is now harvested, and the dedicated supplemental-authority "
    "assessment found no currently discoverable official binding-law source that publishes a "
    "computation-safe intermediate graduated schedule between `KSh 1,000` and `KSh 15,000 "
    "and above`, so this window remains fail-closed and is not promoted to "
    "`implementation_ready`.",
]


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


def test_nhif_2003_numeric_policy_exists_and_has_required_sections() -> None:
    assert NUMERIC_POLICY_PATH.exists(), "NHIF 2003 baseline numeric policy artifact must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_nhif_2003_window_row_is_well_formed_and_not_over_promoted() -> None:
    text = _read_text()
    rows = _extract_table_rows(text, WINDOW_TABLE_HEADER)

    assert rows == [
        EXPECTED_2003_WINDOW_ROW
    ], "The NHIF 2003 readiness row must remain deterministic and fail-closed in this pass."


def test_nhif_2003_rule_rows_are_well_formed() -> None:
    text = _read_text()
    rows = _extract_table_rows(text, RULE_TABLE_HEADER)

    assert rows, "NHIF 2003 numeric policy table must contain at least one rule row."

    rule_ids: list[str] = []
    required_rule_ids = {
        "HC-NHIF-2003-NPOL-0001",
        "HC-NHIF-2003-NPOL-0002",
        "HC-NHIF-2003-NPOL-0003",
        "HC-NHIF-2003-NPOL-0004",
        "HC-NHIF-2003-NPOL-0005",
        "HC-NHIF-2003-NPOL-9001",
    }

    for cells in rows:
        assert len(cells) == 7, "Every NHIF 2003 rule row must include exactly 7 metadata columns."

        rule_id = cells[0]
        numeric_values = cells[2]
        effective_period = cells[3]
        citations = cells[4]
        relevance = cells[5]

        assert rule_id.startswith(
            "HC-NHIF-2003-NPOL-"
        ), "Each NHIF 2003 rule must use the HC-NHIF-2003-NPOL- prefix."
        assert numeric_values not in {
            "",
            "None.",
        }, "Every NHIF 2003 rule must declare extracted values or blocker status."
        assert effective_period not in {
            "",
            "None.",
        }, "Every NHIF 2003 rule must declare an effective period."
        assert citations not in {
            "",
            "None.",
        }, "Every NHIF 2003 rule must declare official citations."
        assert "HC-" in citations, "NHIF 2003 citations must reference committed HC anchors."
        assert relevance in VALID_RELEVANCE, f"Invalid implementation relevance value: {relevance}"

        rule_ids.append(rule_id)

    assert len(rule_ids) == len(set(rule_ids)), "Duplicate NHIF 2003 rule IDs are not allowed."
    assert rule_ids == sorted(rule_ids), "NHIF 2003 rule IDs must remain sorted."
    assert required_rule_ids.issubset(
        set(rule_ids)
    ), "Required NHIF 2003 numeric policy records are missing from the artifact."


def test_nhif_2003_numeric_policy_section_order_is_stable() -> None:
    text = _read_text()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required NHIF 2003 section ordering must remain stable."
    assert (
        "nhif_2003_intermediate_schedule_source_assessment.md" in text
    ), "NHIF 2003 baseline artifact must cite the dedicated supplemental-authority assessment."
    assert (
        "minimum of `KSh 30` at `KSh 1,000`" in text
    ), "NHIF 2003 baseline artifact must preserve the exact harvested lower endpoint."
    assert (
        "upper cap: `KSh 320` at `KSh 15,000 and above`" in text
    ), "NHIF 2003 baseline artifact must preserve the exact harvested upper cap."
    assert (
        "the first day of the month following" in text
    ), "NHIF 2003 baseline artifact must preserve the exact harvested remittance wording."
    assert (
        "no discoverable official binding-law source found" in text
    ), "NHIF 2003 baseline artifact must preserve the explicit supplemental-authority verdict."
