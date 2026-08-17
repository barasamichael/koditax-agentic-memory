from __future__ import annotations

from pathlib import Path

POLICY_SPEC_PATH = Path("docs/phase-4/health-contribution/canonical_policy_specification.md")
DOMAIN_DECOMPOSITION_PATH = Path("docs/phase-4/health-contribution/domain_decomposition.md")

REQUIRED_SECTION_HEADINGS = [
    "## Specification Rules",
    "## Cross-Cutting Policy Statements",
    "## NHIF Legacy Policies",
    "## SHA/SHIF Active-Window Policies",
    "## Transition-Boundary Policies",
    "## Unresolved Policy Areas",
    "## Immediate Downstream Design Use",
]

REQUIRED_TABLE_HEADER = (
    "| Policy ID | Domain ID | Policy statement | Effective period | "
    "Source citation(s) | Implementation relevance | Ambiguity note |"
)

VALID_RELEVANCE = {
    "computation",
    "validation",
    "filing",
    "boundary interaction",
    "computation; validation",
    "computation; filing",
    "computation; boundary interaction",
    "filing; boundary interaction",
}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_domain_ids(text: str) -> set[str]:
    domain_ids: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("| HCD-"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells:
            domain_ids.add(cells[0])
    return domain_ids


def _extract_policy_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        if lines[index] == REQUIRED_TABLE_HEADER:
            assert index + 1 < len(lines), "Policy table is missing the separator row."
            assert lines[index + 1].startswith(
                "| --- |"
            ), "Policy table separator row must follow the required table header."
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


def test_canonical_policy_spec_exists_and_has_required_sections() -> None:
    assert POLICY_SPEC_PATH.exists(), "Health canonical policy spec file must exist."

    text = _read_text(POLICY_SPEC_PATH)
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_policy_tables_are_well_formed_and_domain_anchored() -> None:
    policy_text = _read_text(POLICY_SPEC_PATH)
    domain_text = _read_text(DOMAIN_DECOMPOSITION_PATH)

    valid_domain_ids = _extract_domain_ids(domain_text)
    tables = _extract_policy_tables(policy_text)

    assert len(tables) == 5, "Canonical policy spec must contain exactly five policy tables."

    policy_rows: dict[str, list[str]] = {}
    policy_ids: list[str] = []
    required_policy_ids = {
        "HCP-POL-001",
        "HCP-POL-008",
        "HCP-POL-101",
        "HCP-POL-201",
        "HCP-POL-203",
        "HCP-POL-204",
        "HCP-POL-205",
        "HCP-POL-304",
        "HCP-POL-301",
        "HCP-POL-U01",
        "HCP-POL-U02",
        "HCP-POL-U03",
    }

    for table in tables:
        assert table, "Each policy table must include at least one policy row."
        table_policy_ids: list[str] = []

        for cells in table:
            assert len(cells) == 7, "Every policy row must include exactly 7 metadata columns."

            policy_id = cells[0]
            domain_id = cells[1]
            effective_period = cells[3]
            citations = cells[4]
            relevance = cells[5]

            assert policy_id.startswith("HCP-POL-"), "Each policy ID must use the HCP-POL- prefix."
            assert domain_id in valid_domain_ids, f"Unknown Domain ID in policy spec: {domain_id}"
            assert effective_period not in {
                "",
                "None.",
            }, "Every policy row must declare an effective period."
            assert citations not in {"", "None."}, "Every policy row must declare source citations."
            assert (
                "HC-" in citations
            ), "Source citations must reference committed HC source anchors."
            assert (
                relevance in VALID_RELEVANCE
            ), f"Invalid implementation relevance value: {relevance}"

            policy_ids.append(policy_id)
            policy_rows[policy_id] = cells
            table_policy_ids.append(policy_id)

        assert table_policy_ids == sorted(
            table_policy_ids
        ), "Policy rows in each table must remain sorted by Policy ID."

    assert len(policy_ids) == len(set(policy_ids)), "Duplicate Policy ID values are not allowed."
    assert required_policy_ids.issubset(
        set(policy_ids)
    ), "Required canonical policy records are missing from the specification."
    assert (
        "HC-SHI-NPOL-" in policy_rows["HCP-POL-203"][4]
    ), "HCP-POL-203 must cite the committed SHA/SHIF numeric policy artifact."
    assert (
        "HC-SHI-NPOL-" in policy_rows["HCP-POL-204"][4]
    ), "HCP-POL-204 must cite the committed SHA/SHIF numeric policy artifact."
    assert (
        "HC-SHI-NPOL-" in policy_rows["HCP-POL-205"][4]
    ), "HCP-POL-205 must cite the committed SHA/SHIF numeric policy artifact."
    assert (
        "HC-NHIF-2003-NPOL-" in policy_rows["HCP-POL-U01"][4]
    ), "HCP-POL-U01 must cite the dedicated NHIF 2003 baseline artifact."
    assert (
        "HC-NHIF-2003-IVER-9001" in policy_rows["HCP-POL-U01"][4]
    ), "HCP-POL-U01 must cite the dedicated 2003 supplemental-authority verdict."
    assert (
        "HC-MCTX-CMB-" in policy_rows["HCP-POL-008"][4]
    ), "HCP-POL-008 must cite the committed mixed-context governance artifact."
    assert (
        "HC-MCTX-CMB-" in policy_rows["HCP-POL-304"][4]
    ), "HCP-POL-304 must cite the committed mixed-context governance artifact."


def test_policy_spec_section_order_is_stable() -> None:
    text = _read_text(POLICY_SPEC_PATH)
    positions = [text.index(heading) for heading in REQUIRED_SECTION_HEADINGS]
    assert positions == sorted(positions), "Required section ordering must remain stable."
    assert (
        "nhif_2003_intermediate_schedule_source_assessment.md" in text
    ), "Canonical policy spec must cite the dedicated 2003 supplemental-authority assessment."
    assert (
        "mixed_context_decision_table.md" in text
    ), "Canonical policy spec must cite the dedicated mixed-context decision table."
    assert (
        "HC-MCTX-IVER-9001" in text
    ), "Canonical policy spec must cite the governed mixed-context authority verdicts."
