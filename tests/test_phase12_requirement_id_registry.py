from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-requirement-id-registry.json"
MATRIX_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-traceability-matrix.md"


def _load_registry() -> list[Mapping[str, object]]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return [
        cast(Mapping[str, object], row)
        for row in cast(list[object], payload)
        if isinstance(row, Mapping)
    ]


def _expected_fr_ids() -> list[str]:
    expected: list[str] = []
    families = {
        "AUTH": 13,
        "DOC": 7,
        "CALC": 12,
        "NLP": 8,
        "RPT": 5,
        "FORM": 11,
    }
    for prefix in ("AUTH", "DOC", "CALC", "NLP", "RPT", "FORM"):
        expected.extend([f"FR-{prefix}-{index:03d}" for index in range(1, families[prefix] + 1)])
    return expected


def _sort_key(row: Mapping[str, object]) -> tuple[int, int, int]:
    requirement_id = str(row["requirement_id"])
    requirement_family = str(row["requirement_family"])
    if requirement_family == "FR":
        match = re.fullmatch(r"FR-([A-Z]+)-(\d{3})", requirement_id)
        assert match is not None
        prefix_order = {
            "AUTH": 0,
            "DOC": 1,
            "CALC": 2,
            "NLP": 3,
            "RPT": 4,
            "FORM": 5,
        }
        return (0, prefix_order[match.group(1)], int(match.group(2)))
    if requirement_family == "ARCH":
        return (1, 0, int(requirement_id.split("-")[-1]))
    if requirement_family == "SEC":
        return (2, 0, int(requirement_id.split("-")[-1]))
    raise AssertionError(f"Unknown requirement_family: {requirement_family}")


def test_registry_has_unique_requirement_ids() -> None:
    rows = _load_registry()
    requirement_ids = [str(row["requirement_id"]) for row in rows]
    assert len(requirement_ids) == len(set(requirement_ids))


def test_registry_contains_all_required_fr_ids() -> None:
    rows = _load_registry()
    actual_fr_ids = [
        str(row["requirement_id"]) for row in rows if row.get("requirement_family") == "FR"
    ]
    assert actual_fr_ids == _expected_fr_ids()


def test_registry_order_is_deterministic() -> None:
    rows = _load_registry()
    assert rows == sorted(rows, key=_sort_key)


def test_traceability_matrix_ids_match_registry_exactly() -> None:
    rows = _load_registry()
    registry_ids = [str(row["requirement_id"]) for row in rows]
    matrix_text = MATRIX_PATH.read_text(encoding="utf-8")
    matrix_ids = re.findall(
        r"^\|\s*((?:FR-[A-Z]+-\d{3})|(?:ARCH-SYS-\d{3})|(?:ARCH-SEC-\d{3}))\s*\|",
        matrix_text,
        flags=re.MULTILINE,
    )
    assert sorted(matrix_ids) == sorted(registry_ids)
