"""Models for deterministic reports generation request/response flows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportGenerationRequestModel:
    """Canonical lineage-constrained report-generation request model."""

    computation_id: str
    form_id: str
    report_type: str
    tax_year: int
    historical_version_id: str
    supported_lane_id: str
    output_format: str = "pdf"


@dataclass(frozen=True)
class ReportLineageModel:
    """Canonical lineage response model for generated reports."""

    computation_id: str
    form_id: str
    report_id: str
    report_version_id: str
    historical_version_id: str
    supported_lane_id: str
    tax_type: str
    tax_year: int
    policy_anchor_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportArtifactMetadataModel:
    """Canonical rendered report artifact metadata for deterministic outputs."""

    format: str
    artifact_kind: str
    report_id: str
    report_version_id: str
    content_sha256: str


@dataclass(frozen=True)
class ReportRetentionMetadataModel:
    """Canonical retention lifecycle metadata for one report artifact."""

    retention_class: str
    retention_expires_at: str
    cleanup_status: str


@dataclass(frozen=True)
class ReportDownloadCapabilityModel:
    """Canonical download capability response model for report retrieval flow."""

    report_id: str
    capability_id: str
    download_url: str
    expires_at: str


@dataclass(frozen=True)
class ReportGenerationResponseModel:
    """Canonical deterministic generated report response payload model."""

    status: str
    report_id: str
    report_type: str
    tax_year: int
    report_version_id: str
    lineage_reference: ReportLineageModel
    artifact_metadata: ReportArtifactMetadataModel | None = None


@dataclass(frozen=True)
class ReportAuditLineageModel:
    computation_id: str | None
    form_id: str | None
    historical_version_id: str | None
    supported_lane_id: str | None
    tax_type: str | None
    tax_year: int | None


@dataclass(frozen=True)
class ReportAuditEventModel:
    event_id: str
    event_type: str
    occurred_at: str
    correlation_id: str
    report_id: str | None
    report_version_id: str | None
    tenant_id: str
    actor_id: str
    lineage: ReportAuditLineageModel
    error_code: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    retention_metadata: ReportRetentionMetadataModel | None = None
