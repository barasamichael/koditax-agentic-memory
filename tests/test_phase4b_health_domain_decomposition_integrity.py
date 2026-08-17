from __future__ import annotations

from pathlib import Path

DOMAIN_DECOMPOSITION_PATH = Path("docs/phase-4/health-contribution/domain_decomposition.md")

REQUIRED_SECTION_HEADINGS = [
    "## Decomposition Rules",
    "## Core Computation Domains",
    "## Transition Domains",
    "## Cross-Cutting Governance Domains",
    "## Unresolved Boundary Questions",
    "## Next Design Dependencies",
    "## What Is Out Of Scope Here",
]

REQUIRED_TABLE_HEADER = (
    "| Domain ID | Domain name | Role | Description | Source anchors | "
    "Effective-window dependencies | Upstream dependencies | "
    "Downstream dependencies | Status |"
)

VALID_STATUSES = {
    "governed_ready",
    "boundary_only",
    "unresolved_fail_closed",
}


def _read_text() -> str:
    return DOMAIN_DECOMPOSITION_PATH.read_text(encoding="utf-8")


def _extract_domain_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        if lines[index] == REQUIRED_TABLE_HEADER:
            assert index + 1 < len(lines), "Domain table is missing the separator row."
            assert lines[index + 1].startswith(
                "| --- |"
            ), "Domain table separator row must follow the required table header."
            rows: list[list[str]] = []
            row_index = index + 2
            while row_index < len(lines) and lines[row_index].startswith("|"):
                cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
                rows.append(cells)
                row_index += 1
            tables.append(rows)
            index = row_index
            continue
        index += 1

    return tables


def test_domain_decomposition_exists_and_has_required_sections() -> None:
    assert DOMAIN_DECOMPOSITION_PATH.exists(), "Health domain decomposition file must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_domain_decomposition_tables_are_well_formed_and_deterministic() -> None:
    text = _read_text()
    tables = _extract_domain_tables(text)

    assert len(tables) == 3, "Domain decomposition must contain exactly three domain tables."

    domain_ids: list[str] = []
    required_domain_ids = {
        "HCD-CORE-NHIF-LEGACY",
        "HCD-CORE-SHI-NONSALARIED",
        "HCD-CORE-SHI-SALARIED",
        "HCD-TRANS-REGIME-SELECTION",
        "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
        "HCD-XCUT-MIXED-CONTEXT-PATHS",
    }

    for table in tables:
        assert table, "Each domain table must include at least one domain row."
        table_domain_ids: list[str] = []
        for cells in table:
            assert len(cells) == 9, "Every domain row must include exactly 9 metadata columns."

            domain_id = cells[0]
            source_anchors = cells[4]
            effective_dependencies = cells[5]
            upstream_dependencies = cells[6]
            downstream_dependencies = cells[7]
            status = cells[8]

            assert domain_id.startswith("HCD-"), "Each domain ID must use the HCD- prefix."
            assert (
                domain_id == domain_id.upper()
            ), "Domain IDs must be deterministic uppercase tokens."
            assert source_anchors not in {"", "None."}, "Every domain must declare source anchors."
            assert (
                effective_dependencies != ""
            ), "Every domain must declare effective-window dependencies."
            assert upstream_dependencies != "", "Every domain must declare upstream dependencies."
            assert (
                downstream_dependencies != ""
            ), "Every domain must declare downstream dependencies."
            assert status in VALID_STATUSES, f"Invalid domain status: {status}"

            domain_ids.append(domain_id)
            table_domain_ids.append(domain_id)

        assert table_domain_ids == sorted(
            table_domain_ids
        ), "Domain rows in each table must remain sorted by Domain ID."

    assert len(domain_ids) == len(set(domain_ids)), "Duplicate Domain ID values are not allowed."
    assert required_domain_ids.issubset(
        set(domain_ids)
    ), "Required governed domains are missing from the decomposition."
    assert (
        "HCD-UQ-005" in text
    ), "The unresolved mixed-context normalization question must remain explicit."
    assert (
        "HC-MCTX-CMB-" in text
    ), "The mixed-context domain must cite the committed mixed-context governance artifact."


def test_domain_decomposition_section_order_is_stable() -> None:
    text = _read_text()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
