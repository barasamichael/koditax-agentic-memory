from __future__ import annotations

from pathlib import Path

SOURCE_INVENTORY_PATH = Path("docs/phase-4/health-contribution/source_inventory.md")

REQUIRED_SECTION_HEADINGS = [
    "## Classification Rules",
    "## Core Legal Base",
    "## Amending Instruments",
    "## Subsidiary and Regulatory Instruments",
    "## Operational Guidance",
    "## Coverage Assessment",
    "## Ambiguities",
    "## Next-Source-Controlled Follow-up",
]

REQUIRED_TABLE_HEADER = (
    "| Source ID | Title | Issuing authority | Source type | Official URL | "
    "Date / version / effective info | Temporal coverage | Authority class | "
    "Why it matters for later codification |"
)


def _read_inventory() -> str:
    return SOURCE_INVENTORY_PATH.read_text(encoding="utf-8")


def _iter_source_tables(text: str) -> list[list[str]]:
    tables: list[list[str]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        if line == REQUIRED_TABLE_HEADER:
            assert index + 1 < len(lines), "Source table is missing the separator row."
            assert lines[index + 1].startswith(
                "| --- |"
            ), "Source table separator row must follow the required table header."
            row_index = index + 2
            rows: list[str] = []
            while row_index < len(lines) and lines[row_index].startswith("|"):
                rows.append(lines[row_index])
                row_index += 1
            tables.append(rows)
            index = row_index
            continue
        index += 1

    return tables


def _extract_source_ids(row: str) -> str:
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    assert len(cells) == 9, "Every source row must include exactly 9 metadata columns."
    return cells[0]


def test_source_inventory_exists_and_has_required_sections() -> None:
    assert SOURCE_INVENTORY_PATH.exists(), "Health source inventory file must exist."

    text = _read_inventory()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_source_inventory_uses_fixed_schema_and_unique_source_ids() -> None:
    text = _read_inventory()
    tables = _iter_source_tables(text)

    assert len(tables) == 4, "Inventory must keep exactly four source tables for this subtask."

    source_ids: list[str] = []
    for table in tables:
        assert table, "Each source table must contain at least one source row."
        for row in table:
            source_id = _extract_source_ids(row)
            assert source_id.startswith("HC-"), "Every source ID must use the HC- prefix."
            assert (
                source_id == source_id.upper()
            ), "Source IDs must be deterministic uppercase tokens."
            source_ids.append(source_id)

    assert len(source_ids) == len(set(source_ids)), "Duplicate source IDs are not allowed."


def test_source_inventory_required_section_order_is_stable() -> None:
    text = _read_inventory()
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
