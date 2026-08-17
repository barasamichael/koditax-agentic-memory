from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import cast
from collections.abc import Mapping

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-requirement-id-registry.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-deferral-register.schema.json"
REGISTER_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-deferral-register.json"


def _load_registry_ids() -> set[str]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    rows: list[Mapping[str, object]] = [
        cast(Mapping[str, object], row)
        for row in cast(list[object], payload)
        if isinstance(row, Mapping)
    ]
    return {str(row.get("requirement_id", "")) for row in rows if row.get("requirement_id")}


def _load_schema() -> dict[str, Any]:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _load_register() -> dict[str, Any]:
    payload = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _to_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _has_security_signoff(deferral: Mapping[str, Any]) -> bool:
    approved_by = deferral.get("approved_by")
    if not isinstance(approved_by, list):
        return False
    for approver in cast(list[object], approved_by):
        if not isinstance(approver, Mapping):
            continue
        approver_map = cast(Mapping[str, object], approver)
        if approver_map.get("role") == "Security Lead":
            return True
    return False


def _release_gate_passes(register_payload: Mapping[str, Any], now: datetime) -> bool:
    deferrals = register_payload.get("deferrals")
    if not isinstance(deferrals, list):
        return False
    for deferral in cast(list[object], deferrals):
        if not isinstance(deferral, Mapping):
            return False
        deferral_map = cast(Mapping[str, object], deferral)
        lifecycle_state = deferral_map.get("lifecycle_state")
        if lifecycle_state != "active":
            continue
        expires_at = deferral_map.get("expires_at")
        if not isinstance(expires_at, str):
            return False
        if _to_utc_datetime(expires_at) <= now:
            return False
    return True


def test_deferral_register_validates_against_schema() -> None:
    schema = _load_schema()
    register = _load_register()
    validator = Draft202012Validator(schema)
    errors = sorted(
        cast(Any, validator).iter_errors(cast(Any, register)),
        key=lambda item: item.path,
    )
    assert errors == []


def test_deferral_ids_are_unique() -> None:
    register = _load_register()
    deferrals = register.get("deferrals")
    assert isinstance(deferrals, list)
    deferral_ids = [
        str(cast(Mapping[str, object], item).get("deferral_id", ""))
        for item in cast(list[object], deferrals)
        if isinstance(item, Mapping)
    ]
    assert len(deferral_ids) == len(set(deferral_ids))


def test_every_deferral_links_to_existing_requirement_id() -> None:
    register = _load_register()
    deferrals = register.get("deferrals")
    assert isinstance(deferrals, list)
    registry_ids = _load_registry_ids()
    for deferral in cast(list[object], deferrals):
        assert isinstance(deferral, Mapping)
        requirement_id = cast(Mapping[str, object], deferral).get("requirement_id")
        assert isinstance(requirement_id, str)
        assert requirement_id in registry_ids


def test_active_deferrals_have_future_expiry() -> None:
    register = _load_register()
    deferrals = register.get("deferrals")
    assert isinstance(deferrals, list)
    now = datetime.now(UTC)
    for deferral in cast(list[object], deferrals):
        assert isinstance(deferral, Mapping)
        deferral_map = cast(Mapping[str, object], deferral)
        if deferral_map.get("lifecycle_state") != "active":
            continue
        expires_at = deferral_map.get("expires_at")
        assert isinstance(expires_at, str)
        assert _to_utc_datetime(expires_at) > now


def test_high_and_critical_deferrals_require_security_signoff() -> None:
    register = _load_register()
    deferrals = register.get("deferrals")
    assert isinstance(deferrals, list)
    for deferral in cast(list[object], deferrals):
        assert isinstance(deferral, Mapping)
        deferral_map = cast(Mapping[str, object], deferral)
        risk_class = deferral_map.get("risk_class")
        if risk_class not in {"high", "critical"}:
            continue
        assert _has_security_signoff(deferral_map)

    sample_missing_security = {
        "risk_class": "critical",
        "approved_by": [
            {"role": "Engineering Lead", "name": "eng"},
            {"role": "Product Owner", "name": "product"},
            {"role": "Compliance Lead", "name": "compliance"},
        ],
    }
    assert _has_security_signoff(sample_missing_security) is False


def test_expired_deferral_fails_release_gate_rule() -> None:
    now = datetime(2026, 4, 9, 0, 0, tzinfo=UTC)
    register_payload = {
        "deferrals": [
            {
                "deferral_id": "DFR-2026-001",
                "lifecycle_state": "active",
                "expires_at": "2026-04-08T23:59:59+00:00",
            }
        ]
    }
    assert _release_gate_passes(register_payload, now=now) is False
