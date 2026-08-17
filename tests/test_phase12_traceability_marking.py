from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-requirement-id-registry.json"
LEDGER_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-implementation-status-ledger.json"
MATRIX_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-traceability-matrix.md"
VALID_STATUS = {"full", "partial", "not_implemented"}


def _load_registry() -> list[Mapping[str, object]]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    payload_list = cast(list[object], payload)
    rows: list[Mapping[str, object]] = [
        cast(Mapping[str, object], row) for row in payload_list if isinstance(row, Mapping)
    ]
    assert len(rows) == len(payload_list)
    return rows


def _load_ledger() -> list[Mapping[str, object]]:
    payload = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    payload_list = cast(list[object], payload)
    rows: list[Mapping[str, object]] = [
        cast(Mapping[str, object], row) for row in payload_list if isinstance(row, Mapping)
    ]
    assert len(rows) == len(payload_list)
    return rows


def _matrix_ids() -> list[str]:
    text = MATRIX_PATH.read_text(encoding="utf-8")
    return re.findall(
        r"^\|\s*((?:FR-[A-Z]+-\d{3})|(?:ARCH-SYS-\d{3})|(?:ARCH-SEC-\d{3}))\s*\|",
        text,
        flags=re.MULTILINE,
    )


def test_all_registry_ids_have_exactly_one_status() -> None:
    registry = _load_registry()
    ledger = _load_ledger()
    registry_ids = [str(row["requirement_id"]) for row in registry]
    ledger_ids = [str(row["requirement_id"]) for row in ledger]
    assert len(ledger_ids) == len(set(ledger_ids))
    assert sorted(ledger_ids) == sorted(registry_ids)


def test_status_values_are_valid_enum() -> None:
    ledger = _load_ledger()
    for row in ledger:
        status = row.get("status")
        assert isinstance(status, str)
        assert status in VALID_STATUS


def test_full_status_has_code_and_test_evidence() -> None:
    ledger = _load_ledger()
    for row in ledger:
        if row.get("status") != "full":
            continue
        evidence = row.get("evidence")
        assert isinstance(evidence, list)
        evidence_types = [
            cast(Mapping[str, object], item).get("type")
            for item in cast(list[object], evidence)
            if isinstance(item, Mapping)
        ]
        assert "code" in evidence_types
        assert "test" in evidence_types


def test_partial_or_not_implemented_have_gaps_and_target_milestone() -> None:
    ledger = _load_ledger()
    for row in ledger:
        status = row.get("status")
        if status == "full":
            continue
        gaps = row.get("gaps")
        target = row.get("target_phase_milestone")
        assert isinstance(gaps, list)
        gaps_list = cast(list[object], gaps)
        assert len(gaps_list) >= 1
        assert isinstance(target, str)
        assert target.strip() != ""


def test_traceability_matrix_ids_match_status_ledger() -> None:
    ledger = _load_ledger()
    ledger_ids = [str(row["requirement_id"]) for row in ledger]
    assert _matrix_ids() == ledger_ids


def test_all_evidence_paths_exist_in_repo() -> None:
    ledger = _load_ledger()
    for row in ledger:
        evidence = row.get("evidence")
        assert isinstance(evidence, list)
        for item in cast(list[object], evidence):
            assert isinstance(item, Mapping)
            path_value = cast(Mapping[str, object], item).get("path")
            assert isinstance(path_value, str)
            assert (REPO_ROOT / path_value).exists(), f"Missing evidence path: {path_value}"
