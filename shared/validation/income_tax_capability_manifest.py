"""Load and validate the governed income-tax vertical-slice capability manifest."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path


class CapabilityManifestError(RuntimeError):
    """Represent deterministic capability-manifest validation failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured error details."""

        return {"reason": self.reason, **self._details}


def load_income_tax_vertical_slice_manifest(repo_root: Path | None = None) -> dict[str, object]:
    """Load and minimally validate the governed income-tax capability manifest."""

    target_root = repo_root if repo_root is not None else Path.cwd()
    manifest_path = (
        target_root
        / "contracts"
        / "capabilities"
        / "income_tax_vertical_slice_capability_manifest.json"
    )
    if not manifest_path.exists():
        raise CapabilityManifestError(
            reason="manifest_not_found",
            message="Income-tax capability manifest file is missing.",
            details={"path": str(manifest_path)},
        )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CapabilityManifestError(
            reason="invalid_manifest_json",
            message="Income-tax capability manifest JSON is invalid.",
            details={"line": error.lineno, "column": error.colno},
        ) from error

    if not isinstance(raw, dict):
        raise CapabilityManifestError(
            reason="invalid_manifest_shape",
            message="Income-tax capability manifest must be a JSON object.",
        )

    manifest = cast(dict[str, object], raw)
    _require_string(manifest, "manifest_version")
    scope = _require_string(manifest, "capability_scope")
    if scope != "income_tax_vertical_slice":
        raise CapabilityManifestError(
            reason="invalid_capability_scope",
            message="Capability manifest scope must be 'income_tax_vertical_slice'.",
            details={"capability_scope": scope},
        )
    _require_string(manifest, "generated_at")
    _require_list_of_strings(manifest, "unsupported_domains")
    _validate_supported_lanes(manifest)
    return manifest


def supported_lane_contexts(manifest: dict[str, object]) -> set[tuple[str, str, int]]:
    """Return supported lane contexts as `(lane_id, historical_version_id, tax_year)` tuples."""

    lanes = _require_supported_lane_list(manifest)
    contexts: set[tuple[str, str, int]] = set()
    for lane in lanes:
        contexts.add(
            (
                _require_string(lane, "supported_lane_id"),
                _require_string(lane, "historical_version_id"),
                _require_int(lane, "tax_year"),
            )
        )
    return contexts


def assert_supported_lane(
    manifest: dict[str, object],
    *,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    """Raise when one lane/version/year context is outside governed supported capability."""

    context = (supported_lane_id, historical_version_id, tax_year)
    if context in supported_lane_contexts(manifest):
        return
    raise CapabilityManifestError(
        reason="unsupported_lane_context",
        message="Lane context is not supported by the governed capability manifest.",
        details={
            "supported_lane_id": supported_lane_id,
            "historical_version_id": historical_version_id,
            "tax_year": tax_year,
        },
    )


def _validate_supported_lanes(manifest: dict[str, object]) -> None:
    lanes = _require_supported_lane_list(manifest)
    for lane in lanes:
        _require_string(lane, "supported_lane_id")
        _require_string(lane, "historical_version_id")
        _require_int(lane, "tax_year")
        status = _require_string(lane, "status")
        if status != "supported":
            raise CapabilityManifestError(
                reason="invalid_lane_status",
                message="Supported lane entries must use status 'supported'.",
                details={
                    "supported_lane_id": lane.get("supported_lane_id"),
                    "status": status,
                },
            )
        notes = lane.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise CapabilityManifestError(
                reason="invalid_lane_notes",
                message="Supported lane notes must be a string when provided.",
                details={"supported_lane_id": lane.get("supported_lane_id")},
            )


def _require_supported_lane_list(manifest: dict[str, object]) -> list[dict[str, object]]:
    value = manifest.get("supported_lanes")
    if not isinstance(value, list):
        raise CapabilityManifestError(
            reason="missing_required_field",
            message="Capability manifest requires 'supported_lanes' list.",
            details={"field_name": "supported_lanes"},
        )

    lanes: list[dict[str, object]] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, dict):
            raise CapabilityManifestError(
                reason="invalid_lane_shape",
                message="Each supported lane entry must be a JSON object.",
            )
        lanes.append(cast(dict[str, object], item))
    return lanes


def _require_string(source: dict[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityManifestError(
            reason="missing_required_field",
            message=f"Capability manifest field '{field_name}' is required and must be string.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: dict[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise CapabilityManifestError(
            reason="missing_required_field",
            message=f"Capability manifest field '{field_name}' is required and must be integer.",
            details={"field_name": field_name},
        )
    return value


def _require_list_of_strings(source: dict[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise CapabilityManifestError(
            reason="missing_required_field",
            message=f"Capability manifest field '{field_name}' is required and must be list.",
            details={"field_name": field_name},
        )

    items: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise CapabilityManifestError(
                reason="invalid_list_item",
                message=f"Capability manifest field '{field_name}' must contain strings only.",
                details={"field_name": field_name},
            )
        items.append(item)
    return items
