"""Bounded, format-aware technical inspection before provider processing."""

from __future__ import annotations

from typing import Literal
from dataclasses import field
from dataclasses import dataclass

from services.document_ai.app.config import MAX_UPLOAD_SIZE_BYTES
from services.document_ai.app.config import SOURCE_INSPECTION_POLICY_VERSION
from services.document_ai.app.document_formats import detect_format
from services.document_ai.app.document_formats import DocumentFamily
from services.document_ai.app.document_formats import normalize_media_type
from services.document_ai.app.document_formats import family_for_media_type

InspectionDisposition = Literal["accepted", "quarantined"]
InspectionReason = Literal[
    "accepted",
    "source_empty",
    "source_too_large",
    "unsupported_format",
    "declared_media_type_mismatch",
    "malformed_document",
    "encrypted_document",
    "unsafe_active_content",
    "archive_not_permitted",
    "invalid_office_container",
    "image_dimensions_too_large",
    "structured_text_too_deep",
]


@dataclass(frozen=True)
class SourceInspectionResult:
    policy_version: str
    disposition: InspectionDisposition
    reason: InspectionReason
    observed_media_type: str | None
    observed_source_family: DocumentFamily | None
    observed_source_format: str | None
    declared_media_type: str
    source_size_bytes: int
    page_count: int | None
    structural_scopes: tuple[tuple[int, int], ...]
    diagnostic_payload: dict[str, object] = field(default_factory=dict)

    @property
    def accepted_for_processing(self) -> bool:
        return self.disposition == "accepted"


class SourceInspectionError(ValueError):
    """A safe source-inspection infrastructure failure."""


def inspect_source_bytes(
    payload: bytes,
    *,
    declared_media_type: str,
    file_name: str | None = None,
    policy_version: str = SOURCE_INSPECTION_POLICY_VERSION,
) -> SourceInspectionResult:
    """Confirm actual content without executing, unpacking, or trusting names."""

    del file_name
    if not payload:
        return _rejected(
            "source_empty",
            declared_media_type,
            source_size_bytes=0,
            policy_version=policy_version,
        )
    if len(payload) > MAX_UPLOAD_SIZE_BYTES:
        return _rejected(
            "source_too_large",
            declared_media_type,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    observed, reason, detail = detect_format(payload)
    observed_family = family_for_media_type(observed) if observed is not None else None
    # A recognizable PDF with a conflicting declaration is a type mismatch
    # even when its later structural check also finds it malformed.
    if observed == "application/pdf" and normalize_media_type(declared_media_type) != observed:
        return _rejected(
            "declared_media_type_mismatch",
            declared_media_type,
            observed=observed,
            observed_source_family=observed_family,
            observed_source_format=detail,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    if reason is not None:
        return _rejected(
            reason,
            declared_media_type,
            observed=observed,
            observed_source_family=observed_family,
            observed_source_format=detail,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    if observed is None:
        return _rejected(
            "unsupported_format",
            declared_media_type,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    if observed == "text/plain" and normalize_media_type(declared_media_type) == "application/pdf":
        return _rejected(
            "unsupported_format",
            declared_media_type,
            observed=observed,
            observed_source_family=observed_family,
            observed_source_format=detail,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    if normalize_media_type(declared_media_type) != observed:
        return _rejected(
            "declared_media_type_mismatch",
            declared_media_type,
            observed=observed,
            observed_source_family=observed_family,
            observed_source_format=detail,
            source_size_bytes=len(payload),
            policy_version=policy_version,
        )
    return SourceInspectionResult(
        policy_version=policy_version,
        disposition="accepted",
        reason="accepted",
        observed_media_type=observed,
        observed_source_family=observed_family,
        observed_source_format=detail,
        declared_media_type=declared_media_type,
        source_size_bytes=len(payload),
        page_count=None,
        structural_scopes=((1, 1),),
        diagnostic_payload={
            "declared_media_type": normalize_media_type(declared_media_type),
            "observed_media_type": observed,
            "observed_source_family": observed_family,
            "observed_source_format": detail,
            "source_size_bytes": len(payload),
        },
    )


def _rejected(
    reason: InspectionReason,
    declared_media_type: str,
    *,
    source_size_bytes: int,
    observed: str | None = None,
    observed_source_family: DocumentFamily | None = None,
    observed_source_format: str | None = None,
    policy_version: str = SOURCE_INSPECTION_POLICY_VERSION,
) -> SourceInspectionResult:
    return SourceInspectionResult(
        policy_version,
        "quarantined",
        reason,
        observed,
        observed_source_family,
        observed_source_format,
        declared_media_type,
        source_size_bytes,
        None,
        (),
        {
            "declared_media_type": normalize_media_type(declared_media_type),
            "observed_media_type": observed,
            "observed_source_family": observed_source_family,
            "observed_source_format": observed_source_format,
            "source_size_bytes": source_size_bytes,
            "reason_code": reason,
        },
    )
