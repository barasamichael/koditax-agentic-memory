from __future__ import annotations

from pathlib import Path

ASSESSMENT_PATH = Path(
    "docs/phase-4/health-contribution/nhif_2003_intermediate_schedule_source_assessment.md"
)
POLICY_SPEC_PATH = Path("docs/phase-4/health-contribution/canonical_policy_specification.md")
MATRIX_PATH = Path("docs/phase-4/health-contribution/historical_version_matrix.md")

REQUIRED_SECTION_HEADINGS = [
    "## 1. Search Target Classes",
    "## 2. Exact Official Sources Checked",
    "## 3. Final Authority Verdict",
    "## 4. Governed Outcome Rules",
]

TARGET_TABLE_HEADER = (
    "| Target class ID | Search target class | Official source class | Success condition |"
)
SOURCE_TABLE_HEADER = (
    "| Source check ID | Target class ID | Official source checked | Official URL | "
    "Binding status | Exact result | Supplies exact intermediate schedule? |"
)
VERDICT_TABLE_HEADER = (
    "| Verdict ID | Version ID | Official supplemental authority status | "
    "Deterministic authority decision | Promotion impact | Notes |"
)

EXPECTED_SOURCE_CHECK_IDS = [
    "HC-NHIF-2003-ISRC-0001",
    "HC-NHIF-2003-ISRC-0002",
    "HC-NHIF-2003-ISRC-0003",
    "HC-NHIF-2003-ISRC-0004",
]

EXPECTED_VERDICT_ROW = [
    "HC-NHIF-2003-IVER-9001",
    "HCH-VER-20031205-A",
    "no_discoverable_supplemental_authority_found",
    "No currently discoverable official 2003 binding-law source in the checked Kenya Law "
    "legal-notice and gazette source family supplies the missing intermediate graduated NHIF "
    "contribution schedule between `KSh 1,000` and `KSh 15,000 and above`.",
    "Keep `HCH-VER-20031205-A` `partially_specified`; keep `HCP-POL-U01` explicit; do not "
    "promote to `implementation_ready`.",
    "Re-open only if a new official Kenya Law or Kenya Gazette binding source becomes "
    "discoverable.",
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def test_intermediate_schedule_assessment_exists_and_has_required_sections() -> None:
    assert (
        ASSESSMENT_PATH.exists()
    ), "NHIF 2003 intermediate schedule assessment artifact must exist."

    text = _read_text(ASSESSMENT_PATH)
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_intermediate_schedule_assessment_tables_are_well_formed() -> None:
    text = _read_text(ASSESSMENT_PATH)
    target_rows = _extract_table_rows(text, TARGET_TABLE_HEADER)
    source_rows = _extract_table_rows(text, SOURCE_TABLE_HEADER)
    verdict_rows = _extract_table_rows(text, VERDICT_TABLE_HEADER)

    assert len(target_rows) == 4, "Assessment must define exactly four search target classes."
    assert len(source_rows) == 4, "Assessment must define exactly four official source checks."
    assert verdict_rows == [
        EXPECTED_VERDICT_ROW
    ], "Assessment final authority verdict must remain deterministic."

    source_check_ids: list[str] = []
    for cells in source_rows:
        assert len(cells) == 7, "Every source-check row must include exactly 7 metadata columns."

        source_check_id = cells[0]
        source_url = cells[3]
        binding_status = cells[4]
        schedule_flag = cells[6]

        assert source_check_id.startswith(
            "HC-NHIF-2003-ISRC-"
        ), "Each source-check row must use the HC-NHIF-2003-ISRC- prefix."
        assert source_url.startswith(
            "https://new.kenyalaw.org/"
        ), "Every checked source must remain on the official Kenya Law domain."
        assert (
            binding_status == "binding_law"
        ), "This assessment must remain limited to binding-law source classes."
        assert (
            schedule_flag == "no"
        ), "No checked official source may silently imply an exact intermediate schedule."
        source_check_ids.append(source_check_id)

    assert (
        source_check_ids == EXPECTED_SOURCE_CHECK_IDS
    ), "Assessment source-check IDs must remain stable and ordered."


def test_not_found_verdict_keeps_policy_and_matrix_fail_closed() -> None:
    assessment_text = _read_text(ASSESSMENT_PATH)
    policy_text = _read_text(POLICY_SPEC_PATH)
    matrix_text = _read_text(MATRIX_PATH)

    assert (
        "no_discoverable_supplemental_authority_found" in assessment_text
    ), "Assessment must preserve the current not-found supplemental-authority verdict."
    assert "HCP-POL-U01" in policy_text, (
        "Canonical policy must keep HCP-POL-U01 explicit while no exact supplemental authority "
        "has been found."
    )

    matrix_rows = _extract_table_rows(
        matrix_text,
        "| Version ID | Effective start | Effective end | Window scope | "
        "Governing change anchors | Source anchors | Affected domains | "
        "Coverage status | Notes |",
    )
    baseline_row = next(cells for cells in matrix_rows if cells[0] == "HCH-VER-20031205-A")

    assert baseline_row[7] == "partially_specified", (
        "HCH-VER-20031205-A must remain partially_specified while the assessment shows no exact "
        "supplemental authority."
    )
    assert (
        "HC-NHIF-2003-IVER-9001" in baseline_row[5]
    ), "Historical version matrix must stay anchored to the assessment verdict while unresolved."


def test_intermediate_schedule_assessment_section_order_is_stable() -> None:
    text = _read_text(ASSESSMENT_PATH)
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(
        positions
    ), "Required assessment section ordering must remain stable."
    assert (
        "Kenya Gazette dated 2003-12-05 No. 120" in text
    ), "Assessment must preserve the checked official Gazette publication record."
    expected_reopen_rule = (
        "Re-open only if a new official Kenya Law or Kenya Gazette binding source "
        "becomes discoverable."
    )
    assert (
        expected_reopen_rule in text
    ), "Assessment must preserve the explicit re-open rule for future source discovery."
