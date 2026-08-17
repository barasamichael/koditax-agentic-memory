from __future__ import annotations

from pathlib import Path

MIXED_CONTEXT_POLICY_PATH = Path(
    "docs/phase-4/health-contribution/mixed_context_policy_extraction.md"
)

REQUIRED_SECTION_HEADINGS = [
    "## Mixed-Context Classification Rules",
    "## Supported Candidate Combinations",
    "## Unsupported Combinations",
    "## Normalization Order",
    "## Regime/Window Interaction Rules",
    "## Exemption Dependency Boundaries",
    "## Readiness Verdict By Combination",
    "## Unresolved Areas Carried Forward",
]

REQUIRED_VERDICT_TABLE_HEADER = (
    "| Mixed-context ID | Candidate combination | Effective-window linkage | "
    "Source anchors | Policy anchors | Normalization-order status | "
    "Regime/window interaction rule | Support verdict | Notes |"
)

VALID_SUPPORT_VERDICTS = {
    "implementation_ready",
    "partially_specified",
    "fail_closed",
}


def _read_text() -> str:
    return MIXED_CONTEXT_POLICY_PATH.read_text(encoding="utf-8")


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


def test_mixed_context_policy_exists_and_has_required_sections() -> None:
    assert MIXED_CONTEXT_POLICY_PATH.exists(), "Mixed-context policy extraction file must exist."

    text = _read_text()
    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in text, f"Missing required section heading: {heading}"


def test_mixed_context_verdict_table_is_well_formed_and_deterministic() -> None:
    text = _read_text()
    rows = _extract_rows(text, REQUIRED_VERDICT_TABLE_HEADER)

    assert rows, "Mixed-context verdict table must contain at least one candidate row."

    row_map: dict[str, list[str]] = {}
    mixed_context_ids: list[str] = []

    for cells in rows:
        assert len(cells) == 9, "Each mixed-context verdict row must contain 9 metadata columns."

        mixed_context_id = cells[0]
        effective_window_linkage = cells[2]
        source_anchors = cells[3]
        policy_anchors = cells[4]
        normalization_order_status = cells[5]
        interaction_rule = cells[6]
        support_verdict = cells[7]

        assert mixed_context_id.startswith(
            "HC-MCTX-CMB-"
        ), "Mixed-context IDs must use the HC-MCTX-CMB- prefix."
        assert effective_window_linkage not in {
            "",
            "None.",
        }, "Every verdict row must declare effective-window linkage."
        assert source_anchors not in {"", "None."}, "Every verdict row must declare source anchors."
        assert (
            "HC-" in source_anchors
        ), "Source anchors must reference committed HC source or change anchors."
        assert policy_anchors not in {"", "None."}, "Every verdict row must declare policy anchors."
        assert (
            "HCP-POL-" in policy_anchors
        ), "Policy anchors must reference committed canonical policy IDs."
        assert normalization_order_status not in {
            "",
            "None.",
        }, "Every verdict row must declare normalization-order status."
        assert interaction_rule not in {
            "",
            "None.",
        }, "Every verdict row must declare regime/window interaction rules."
        assert (
            support_verdict in VALID_SUPPORT_VERDICTS
        ), f"Invalid mixed-context support verdict: {support_verdict}"

        mixed_context_ids.append(mixed_context_id)
        row_map[mixed_context_id] = cells

    assert mixed_context_ids == sorted(
        mixed_context_ids
    ), "Mixed-context verdict rows must remain sorted by Mixed-context ID."
    assert len(mixed_context_ids) == len(
        set(mixed_context_ids)
    ), "Duplicate mixed-context IDs are not allowed."
    assert set(row_map) == {
        "HC-MCTX-CMB-0001",
        "HC-MCTX-CMB-0002",
        "HC-MCTX-CMB-0003",
        "HC-MCTX-CMB-0004",
    }, "The governed mixed-context candidate set drifted unexpectedly."

    for mixed_context_id, cells in row_map.items():
        support_verdict = cells[7]
        if support_verdict == "implementation_ready":
            assert (
                cells[5] != "screening_only_fail_closed"
            ), f"{mixed_context_id} cannot be implementation_ready with fail-closed normalization."
            assert (
                "HCH-VER-" in cells[2]
            ), f"{mixed_context_id} must cite explicit governed windows before promotion."


def test_mixed_context_policy_preserves_fail_closed_runtime_posture_when_no_path_is_ready() -> None:
    text = _read_text()

    assert (
        "No mixed-context combination is `implementation_ready` in this pass." in text
    ), "The artifact must explicitly state when no mixed-context path is promoted."
    assert (
        "mixed_context_decision_table.md" in text
    ), "The mixed-context policy artifact must cite the dedicated decision table."
    assert (
        "HC-MCTX-IVER-9001" in text
    ), "The mixed-context policy artifact must surface the stable authority verdict IDs."
    assert "HCP-POL-U03" in text, "Exemption-dependent mixed-context blocking must remain explicit."
    assert (
        "HCD-UQ-005" in text
    ), "The unresolved mixed-context normalization question must remain explicit."
