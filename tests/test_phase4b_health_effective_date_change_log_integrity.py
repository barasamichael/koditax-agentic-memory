from __future__ import annotations

import re
from pathlib import Path

CHANGE_LOG_PATH = Path("docs/phase-4/health-contribution/effective_date_change_log.md")
SOURCE_INVENTORY_PATH = Path("docs/phase-4/health-contribution/source_inventory.md")

REQUIRED_SECTION_HEADINGS = [
    "## Canonical Change-Log Rules",
    "## Chronological Change Log",
    "## What This Log Is Already Good For",
    "## What Remains Unresolved",
]

REQUIRED_TABLE_HEADER = (
    "| Change ID | Source ID | Instrument | Official URL | Effective date | "
    "Boundary class | Affected contribution area | Evidence note | Ambiguity note |"
)

BOUNDARY_CLASSES = {
    "nhif_legacy",
    "transition_boundary",
    "sha_shif_active",
    "governed_boundary_only",
}

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_inventory_source_ids(text: str) -> set[str]:
    source_ids: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("| HC-"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells:
            source_ids.add(cells[0])
    return source_ids


def _extract_change_log_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == REQUIRED_TABLE_HEADER:
            assert index + 1 < len(lines), "Change log table is missing the separator row."
            assert lines[index + 1].startswith(
                "| --- |"
            ), "Change log table separator row must follow the required table header."
            rows: list[list[str]] = []
            row_index = index + 2
            while row_index < len(lines) and lines[row_index].startswith("|"):
                cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
                rows.append(cells)
                row_index += 1
            return rows

    raise AssertionError("Required change log table header was not found.")


def test_effective_date_change_log_exists_and_has_required_sections() -> None:
    assert CHANGE_LOG_PATH.exists(), "Health effective-date change log must exist."

    text = _read_text(CHANGE_LOG_PATH)
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_effective_date_change_log_rows_are_well_formed_and_source_anchored() -> None:
    change_log_text = _read_text(CHANGE_LOG_PATH)
    inventory_text = _read_text(SOURCE_INVENTORY_PATH)

    valid_source_ids = _extract_inventory_source_ids(inventory_text)
    rows = _extract_change_log_rows(change_log_text)

    assert rows, "Change log must contain at least one data row."

    change_ids: list[str] = []
    parsed_order: list[tuple[str, str]] = []

    for cells in rows:
        assert len(cells) == 9, "Every change-log row must include exactly 9 metadata columns."

        change_id = cells[0]
        source_id = cells[1]
        effective_date = cells[4]
        boundary_class = cells[5]

        assert change_id.startswith("HC-CHG-"), "Each change ID must use the HC-CHG- prefix."
        assert source_id in valid_source_ids, f"Unknown source ID in change log: {source_id}"
        assert DATE_PATTERN.match(
            effective_date
        ), f"Effective date must be present in YYYY-MM-DD form: {effective_date}"
        assert (
            boundary_class in BOUNDARY_CLASSES
        ), f"Invalid boundary class in change log: {boundary_class}"

        change_ids.append(change_id)
        parsed_order.append((effective_date, change_id))

    assert len(change_ids) == len(set(change_ids)), "Duplicate Change ID values are not allowed."
    assert parsed_order == sorted(
        parsed_order
    ), "Change-log rows must remain ordered by effective date, then Change ID."


def test_effective_date_change_log_section_order_is_stable() -> None:
    text = _read_text(CHANGE_LOG_PATH)
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
