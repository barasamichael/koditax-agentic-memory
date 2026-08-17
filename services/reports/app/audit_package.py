"""Deterministic audit package ZIP builder for supported income-tax reports."""

from __future__ import annotations

from io import BytesIO
import json
from typing import cast
import hashlib
from zipfile import ZipFile
from zipfile import ZipInfo
from zipfile import ZIP_DEFLATED

from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportArtifactMetadataModel
from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_AUDIT_PACKAGE_ARTIFACT_KINDS: frozenset[str] = frozenset({"audit_package"})
AUDIT_PACKAGE_FILE_ORDER: tuple[str, ...] = (
    "summary/summary.json",
    "worksheet/worksheet.json",
    "exports/exports.json",
    "lineage/manifest.json",
)
_FIXED_ZIP_DATETIME = (2026, 1, 1, 0, 0, 0)


class ReportAuditPackageError(RuntimeError):
    """Represent deterministic audit-package ZIP failures."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def render_audit_package_zip(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> ReportArtifactMetadataModel:
    """Render deterministic audit ZIP package and return canonical metadata."""

    normalized_kind = artifact_kind.strip().lower()
    if normalized_kind not in SUPPORTED_AUDIT_PACKAGE_ARTIFACT_KINDS:
        raise ReportAuditPackageError(
            reason_code="report_generation_not_supported",
            message="Requested audit package artifact kind is not supported.",
        )
    try:
        source_artifacts = _build_source_artifacts(
            report_id=report_id,
            report_version_id=report_version_id,
            report_type=report_type,
            tax_year=tax_year,
            lineage=lineage,
        )
        zip_bytes = build_audit_package_zip_bytes(
            report_id=report_id,
            report_version_id=report_version_id,
            lineage=lineage,
            source_artifacts=source_artifacts,
        )
    except ReportAuditPackageError:
        raise
    except Exception as error:  # pragma: no cover - defensive canonical mapping
        raise ReportAuditPackageError(
            reason_code="report_packaging_failed",
            message="Failed to build deterministic audit package zip.",
        ) from error

    return ReportArtifactMetadataModel(
        format="zip",
        artifact_kind=normalized_kind,
        report_id=report_id,
        report_version_id=report_version_id,
        content_sha256=hashlib.sha256(zip_bytes).hexdigest(),
    )


def build_audit_package_zip_bytes(
    *,
    report_id: str,
    report_version_id: str,
    lineage: ReportLineageModel,
    source_artifacts: dict[str, dict[str, object]],
) -> bytes:
    """Build deterministic ZIP bytes with canonical folder layout and manifest."""

    _validate_lineage(lineage=lineage)
    _validate_required_sources(source_artifacts=source_artifacts)

    included_files = list(AUDIT_PACKAGE_FILE_ORDER)
    manifest = _build_manifest(
        report_id=report_id,
        report_version_id=report_version_id,
        lineage=lineage,
        included_files=included_files,
    )
    file_content: dict[str, bytes] = {
        "summary/summary.json": _encode_json(source_artifacts["summary"]),
        "worksheet/worksheet.json": _encode_json(source_artifacts["worksheet"]),
        "exports/exports.json": _encode_json(source_artifacts["exports"]),
        "lineage/manifest.json": _encode_json(manifest),
    }

    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for entry_name in AUDIT_PACKAGE_FILE_ORDER:
            _write_zip_entry(
                archive=archive, entry_name=entry_name, content=file_content[entry_name]
            )
    return buffer.getvalue()


def _build_source_artifacts(
    *,
    report_id: str,
    report_version_id: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> dict[str, dict[str, object]]:
    return {
        "summary": {
            "report_id": report_id,
            "report_version_id": report_version_id,
            "report_type": report_type,
            "tax_year": tax_year,
            "scope": "income_tax",
        },
        "worksheet": {
            "tax_year": tax_year,
            "supported_lane_id": lineage.supported_lane_id,
            "historical_version_id": lineage.historical_version_id,
        },
        "exports": {
            "available_formats": ["pdf", "xlsx", "csv", "zip"],
            "artifact_kind": "audit_package",
            "report_id": report_id,
        },
    }


def _build_manifest(
    *,
    report_id: str,
    report_version_id: str,
    lineage: ReportLineageModel,
    included_files: list[str],
) -> dict[str, object]:
    return {
        "report_id": report_id,
        "report_version_id": report_version_id,
        "computation_id": lineage.computation_id,
        "form_id": lineage.form_id,
        "included_files": included_files,
        "generated_at": "2026-01-01T00:00:00Z",
    }


def _validate_required_sources(*, source_artifacts: dict[str, dict[str, object]]) -> None:
    required_keys = {"summary", "worksheet", "exports"}
    missing = sorted(required_keys - set(source_artifacts))
    if missing:
        raise ReportAuditPackageError(
            reason_code="report_packaging_failed",
            message="Audit package source artifacts are incomplete.",
        )


def _validate_lineage(*, lineage: ReportLineageModel) -> None:
    for anchor in lineage.policy_anchor_ids:
        if not anchor.strip():
            raise ValueError("policy_anchor_ids must contain non-empty strings.")
    for anchor in lineage.source_anchor_ids:
        if not anchor.strip():
            raise ValueError("source_anchor_ids must contain non-empty strings.")


def _encode_json(payload: dict[str, object]) -> bytes:
    serialized = canonical_json_dumps(payload)
    return f"{serialized}\n".encode()


def _write_zip_entry(*, archive: ZipFile, entry_name: str, content: bytes) -> None:
    info = ZipInfo(filename=entry_name, date_time=_FIXED_ZIP_DATETIME)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


def read_manifest_from_zip(*, zip_bytes: bytes) -> dict[str, object]:
    """Read manifest payload for deterministic unit tests."""

    with ZipFile(BytesIO(zip_bytes), mode="r") as archive:
        raw = archive.read("lineage/manifest.json")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("lineage/manifest.json must be a JSON object.")
    return cast(dict[str, object], parsed)
