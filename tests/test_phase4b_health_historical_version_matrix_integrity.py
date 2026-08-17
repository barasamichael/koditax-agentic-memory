from __future__ import annotations

from pathlib import Path
from datetime import date

MATRIX_PATH = Path("docs/phase-4/health-contribution/historical_version_matrix.md")

REQUIRED_SECTION_HEADINGS = [
    "## Coverage Status Definitions",
    "## Version Window Matrix",
    "## Rule-Binding Use Rules",
    "## Coverage-Planning Use Rules",
    "## Immediate Planning Implications",
]

REQUIRED_TABLE_HEADER = (
    "| Version ID | Effective start | Effective end | Window scope | "
    "Governing change anchors | Source anchors | Affected domains | "
    "Coverage status | Notes |"
)

VALID_COVERAGE_STATUS = {
    "governed_boundary_only",
    "partially_specified",
    "implementation_ready",
}


def _read_text() -> str:
    return MATRIX_PATH.read_text(encoding="utf-8")


def _extract_matrix_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == REQUIRED_TABLE_HEADER:
            assert index + 1 < len(lines), "Version matrix table is missing the separator row."
            assert lines[index + 1].startswith(
                "| --- |"
            ), "Version matrix separator row must follow the required table header."
            rows: list[list[str]] = []
            row_index = index + 2
            while row_index < len(lines) and lines[row_index].startswith("|"):
                cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
                rows.append(cells)
                row_index += 1
            return rows

    raise AssertionError("Required version matrix table header was not found.")


def _parse_date(value: str) -> date:
    year, month, day = [int(part) for part in value.split("-")]
    return date(year, month, day)


def test_historical_version_matrix_exists_and_has_required_sections() -> None:
    assert MATRIX_PATH.exists(), "Health historical version matrix file must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_matrix_rows_are_well_formed_and_chronology_safe() -> None:
    text = _read_text()
    rows = _extract_matrix_rows(text)

    assert rows, "Version matrix must contain at least one data row."

    row_map: dict[str, list[str]] = {}
    version_ids: list[str] = []
    parsed_order: list[tuple[date, str]] = []
    required_version_ids = {
        "HCH-VER-19990215-A",
        "HCH-VER-20221231-REG",
        "HCH-VER-20231122-REPEAL",
        "HCH-VER-20241001-A",
        "HCH-VER-20250228-PIT",
    }

    for cells in rows:
        assert len(cells) == 9, "Every matrix row must include exactly 9 metadata columns."

        version_id = cells[0]
        start_value = cells[1]
        end_value = cells[2]
        governing_anchors = cells[4]
        source_anchors = cells[5]
        affected_domains = cells[6]
        coverage_status = cells[7]

        assert version_id.startswith("HCH-VER-"), "Each version row must use the HCH-VER- prefix."
        assert governing_anchors not in {
            "",
            "None.",
        }, "Every version row must declare governing change anchors."
        assert source_anchors not in {"", "None."}, "Every version row must declare source anchors."
        assert affected_domains not in {
            "",
            "None.",
        }, "Every version row must declare affected domains."
        assert (
            "HC-" in governing_anchors
        ), "Governing change anchors must reference committed HC change anchors."
        assert (
            "HC-" in source_anchors
        ), "Source anchors must reference committed HC source or policy anchors."
        assert (
            "HCD-" in affected_domains
        ), "Affected domains must reference committed HCD domain identifiers."
        assert (
            coverage_status in VALID_COVERAGE_STATUS
        ), f"Invalid coverage status: {coverage_status}"

        start_date = _parse_date(start_value)
        if end_value != "open":
            end_date = _parse_date(end_value)
            assert start_date <= end_date, "Effective start must not be after effective end."

        version_ids.append(version_id)
        row_map[version_id] = cells
        parsed_order.append((start_date, version_id))

    assert len(version_ids) == len(set(version_ids)), "Duplicate Version ID values are not allowed."
    assert required_version_ids.issubset(
        set(version_ids)
    ), "Required historical version windows are missing from the matrix."
    assert parsed_order == sorted(
        parsed_order
    ), "Matrix rows must remain ordered by effective start, then Version ID."
    for version_id in {"HCH-VER-20241001-A", "HCH-VER-20250228-PIT"}:
        cells = row_map[version_id]
        source_anchors = cells[5]
        coverage_status = cells[7]

        assert (
            coverage_status == "implementation_ready"
        ), f"{version_id} must be marked implementation_ready after SHA numeric extraction."
        assert (
            "HC-SHI-NPOL-" in source_anchors
        ), f"{version_id} must cite committed SHA/SHIF numeric policy rules."
        assert (
            "HCP-POL-U02" not in source_anchors
        ), f"{version_id} must not rely on unresolved HCP-POL-U02 as an implementation anchor."
        assert (
            "HCP-POL-U03" not in source_anchors
        ), f"{version_id} must not rely on unresolved HCP-POL-U03 as an implementation anchor."

    for version_id in {"HCH-VER-20240920-AMD", "HCH-VER-20250228-AMD"}:
        assert (
            row_map[version_id][7] == "governed_boundary_only"
        ), f"{version_id} must remain a governed_boundary_only amendment-layer row."

    baseline_2003_row = row_map["HCH-VER-20031205-A"]
    baseline_2003_source_anchors = baseline_2003_row[5]
    baseline_2003_status = baseline_2003_row[7]

    assert (
        "HC-NHIF-2003-NPOL-" in baseline_2003_source_anchors
    ), "HCH-VER-20031205-A must cite the dedicated NHIF 2003 baseline artifact."
    assert (
        "HC-NHIF-2003-IVER-9001" in baseline_2003_source_anchors
    ), "HCH-VER-20031205-A must cite the dedicated 2003 supplemental-authority verdict."
    if baseline_2003_status == "implementation_ready":
        assert (
            "HCP-POL-U01" not in baseline_2003_source_anchors
        ), "HCH-VER-20031205-A must not remain anchored to HCP-POL-U01 if promoted."
    else:
        assert baseline_2003_status == "partially_specified", (
            "HCH-VER-20031205-A must remain partially_specified "
            "until exact original text is harvested."
        )
        assert (
            "HCP-POL-U01" in baseline_2003_source_anchors
        ), "HCH-VER-20031205-A must remain explicitly anchored to HCP-POL-U01 while unresolved."


def test_matrix_section_order_is_stable() -> None:
    text = _read_text()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
    assert (
        "nhif_2003_intermediate_schedule_source_assessment.md" in text
    ), "Historical version matrix must cite the dedicated 2003 supplemental-authority assessment."
    assert (
        "mixed_context_policy_extraction.md" in text
    ), "Historical version matrix must reference the governed mixed-context artifact."
    assert (
        "mixed_context_decision_table.md" in text
    ), "Historical version matrix must reference the mixed-context decision table."
    assert (
        "no mixed-context combination" in text
    ), "Historical version matrix must preserve the current fail-closed mixed-context posture."
