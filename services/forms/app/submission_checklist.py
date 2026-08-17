"""Deterministic submission-checklist generation for supported forms scope."""

from __future__ import annotations

from typing import cast
from typing import Final
from typing import TypedDict
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

CHECKLIST_STATUS_READY: Final[str] = "ready"
CHECKLIST_STATUS_NOT_READY: Final[str] = "not_ready"


class SubmissionChecklistItem(TypedDict):
    """Represent one deterministic submission-readiness checklist item."""

    code: str
    description: str
    status: str
    blocking: bool
    evidence_ref: str


def build_submission_checklist(
    *,
    artifact_id: str,
    form_type: str,
    tax_year: int,
    form_version_id: str,
    lineage_reference: Mapping[str, object],
    storage_metadata: Mapping[str, object],
    retention_metadata: Mapping[str, object],
    pre_population_source_fields: Mapping[str, object],
) -> dict[str, object]:
    """Build deterministic submission checklist and overall readiness status."""

    checks = (
        _build_source_record_resolved_item(artifact_id=artifact_id),
        _build_lineage_complete_item(
            artifact_id=artifact_id,
            form_version_id=form_version_id,
            lineage_reference=lineage_reference,
        ),
        _build_storage_reference_item(artifact_id=artifact_id, storage_metadata=storage_metadata),
        _build_retention_active_item(
            artifact_id=artifact_id,
            retention_metadata=retention_metadata,
        ),
        _build_download_window_item(
            artifact_id=artifact_id,
            retention_metadata=retention_metadata,
        ),
        _build_validation_gate_item(artifact_id=artifact_id),
        _build_pre_population_snapshot_item(
            artifact_id=artifact_id,
            pre_population_source_fields=pre_population_source_fields,
        ),
    )
    items = list(checks)
    has_blocking_failure = any(item["blocking"] and item["status"] == "fail" for item in items)
    overall_status = CHECKLIST_STATUS_NOT_READY if has_blocking_failure else CHECKLIST_STATUS_READY
    checklist_id = _build_checklist_id(
        artifact_id=artifact_id,
        form_version_id=form_version_id,
        tax_year=tax_year,
        form_type=form_type,
        items=items,
        overall_status=overall_status,
    )
    return {
        "checklist_id": checklist_id,
        "form_type": form_type,
        "tax_year": tax_year,
        "overall_status": overall_status,
        "items": items,
    }


def _build_source_record_resolved_item(*, artifact_id: str) -> SubmissionChecklistItem:
    return {
        "code": "source_artifact_record_resolved",
        "description": "Artifact metadata and history record resolved for submission context.",
        "status": "pass",
        "blocking": True,
        "evidence_ref": f"artifact:{artifact_id}",
    }


def _build_lineage_complete_item(
    *,
    artifact_id: str,
    form_version_id: str,
    lineage_reference: Mapping[str, object],
) -> SubmissionChecklistItem:
    required_fields = ("computation_id", "historical_version_id", "form_version_id", "input_hash")
    missing_fields = [
        field_name
        for field_name in required_fields
        if not isinstance(lineage_reference.get(field_name), str)
        or not cast(str, lineage_reference.get(field_name)).strip()
    ]
    form_version_matches = lineage_reference.get("form_version_id") == form_version_id
    has_failures = bool(missing_fields) or not form_version_matches
    status = "fail" if has_failures else "pass"
    evidence_ref = (
        f"artifact:{artifact_id}:lineage_missing:{','.join(sorted(missing_fields))}"
        if missing_fields
        else (
            f"artifact:{artifact_id}:lineage_form_version_mismatch"
            if not form_version_matches
            else f"artifact:{artifact_id}:lineage_valid"
        )
    )
    return {
        "code": "artifact_lineage_complete",
        "description": "Lineage identity is complete and matches the requested form version.",
        "status": status,
        "blocking": True,
        "evidence_ref": evidence_ref,
    }


def _build_storage_reference_item(
    *,
    artifact_id: str,
    storage_metadata: Mapping[str, object],
) -> SubmissionChecklistItem:
    required_fields = (
        "storage_object_id",
        "storage_backend",
        "content_type",
        "size_bytes",
        "artifact_hash",
    )
    missing_fields = [
        field_name
        for field_name in required_fields
        if storage_metadata.get(field_name) in {None, ""}
    ]
    status = "fail" if missing_fields else "pass"
    evidence_ref = (
        f"artifact:{artifact_id}:storage_missing:{','.join(sorted(missing_fields))}"
        if missing_fields
        else f"artifact:{artifact_id}:storage_ready"
    )
    return {
        "code": "storage_reference_available",
        "description": "Governed storage reference is available for artifact retrieval lifecycle.",
        "status": status,
        "blocking": True,
        "evidence_ref": evidence_ref,
    }


def _build_retention_active_item(
    *,
    artifact_id: str,
    retention_metadata: Mapping[str, object],
) -> SubmissionChecklistItem:
    retention_status = retention_metadata.get("retention_status")
    status = "pass" if retention_status == "active" else "fail"
    evidence_ref = f"artifact:{artifact_id}:retention_status:{retention_status}"
    return {
        "code": "retention_policy_active",
        "description": (
            "Artifact retention policy is active and allows submission-readiness processing."
        ),
        "status": status,
        "blocking": True,
        "evidence_ref": evidence_ref,
    }


def _build_download_window_item(
    *,
    artifact_id: str,
    retention_metadata: Mapping[str, object],
) -> SubmissionChecklistItem:
    download_expires_at = retention_metadata.get("download_expires_at")
    has_window = isinstance(download_expires_at, str) and bool(download_expires_at.strip())
    status = "pass" if has_window else "fail"
    evidence_ref = (
        f"artifact:{artifact_id}:download_window:{download_expires_at}"
        if has_window
        else f"artifact:{artifact_id}:download_window_not_issued"
    )
    return {
        "code": "download_window_issued",
        "description": (
            "Time-bounded download window has been issued for artifact delivery readiness."
        ),
        "status": status,
        "blocking": True,
        "evidence_ref": evidence_ref,
    }


def _build_validation_gate_item(*, artifact_id: str) -> SubmissionChecklistItem:
    return {
        "code": "pre_generation_validation_passed",
        "description": "Artifact exists only after deterministic pre-generation validation passed.",
        "status": "pass",
        "blocking": True,
        "evidence_ref": f"artifact:{artifact_id}:generation_validation_gate",
    }


def _build_pre_population_snapshot_item(
    *,
    artifact_id: str,
    pre_population_source_fields: Mapping[str, object],
) -> SubmissionChecklistItem:
    has_snapshot = bool(pre_population_source_fields)
    status = "pass" if has_snapshot else "warn"
    evidence_ref = (
        f"artifact:{artifact_id}:pre_population_snapshot_present"
        if has_snapshot
        else f"artifact:{artifact_id}:pre_population_snapshot_missing"
    )
    return {
        "code": "pre_population_snapshot_available",
        "description": (
            "Prior-year pre-population snapshot is available for submission preparation reuse."
        ),
        "status": status,
        "blocking": False,
        "evidence_ref": evidence_ref,
    }


def _build_checklist_id(
    *,
    artifact_id: str,
    form_version_id: str,
    tax_year: int,
    form_type: str,
    items: list[SubmissionChecklistItem],
    overall_status: str,
) -> str:
    identity = {
        "artifact_id": artifact_id,
        "form_version_id": form_version_id,
        "tax_year": tax_year,
        "form_type": form_type,
        "overall_status": overall_status,
        "items": [
            {
                "code": item["code"],
                "status": item["status"],
                "blocking": item["blocking"],
                "evidence_ref": item["evidence_ref"],
            }
            for item in items
        ],
    }
    return hashlib.sha256(
        f"forms-submission-checklist:{canonical_json_dumps(identity)}".encode()
    ).hexdigest()
