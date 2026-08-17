"""Stable logical-document and immutable source-version operations.

This boundary implements Document Policy FR-001, FR-002, FR-018, FR-020 and
SR-012.  A source artifact is deliberately accepted only as private command
input; public results contain stable document and version identities only.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import UUID
from uuid import uuid4

from services.document_ai.app.document_lifecycle import DocumentLifecycleState
from services.document_ai.app.document_registry import PersistedDocumentRecord


class DocumentVersioningError(ValueError):
    """Reject an identity/version command without exposing source internals."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceArtifactCommand:
    """Private immutable source-artifact attributes for one new version."""

    storage_key: str
    checksum_sha256: str
    content_type: str
    size_bytes: int
    idempotency_key: str


@dataclass(frozen=True)
class DocumentVersionRecord:
    """Tenant-scoped source version without a storage-provider identifier."""

    document_version_id: UUID
    document_id: UUID
    tenant_id: str
    version_number: int
    version_state: str
    source_artifact_id: UUID
    supersedes_document_version_id: UUID | None = None


@dataclass(frozen=True)
class LogicalDocumentRecord:
    """Stable logical document state and optimistic revision."""

    document_id: UUID
    tenant_id: str
    owner_user_id: UUID
    state: DocumentLifecycleState
    display_name: str | None
    revision: int
    active_document_version_id: UUID | None


class InMemoryDocumentVersioningStore:
    """Atomic reference implementation used by non-production document tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[UUID, LogicalDocumentRecord] = {}
        self._versions: dict[UUID, list[DocumentVersionRecord]] = {}
        self._version_commands: dict[tuple[str, UUID, str], tuple[SourceArtifactCommand, UUID]] = {}
        self._legacy_mappings: dict[tuple[str, str, str], UUID] = {}

    def register_document(self, document: PersistedDocumentRecord, display_name: str | None = None) -> None:
        """Register an existing stable document ID without replacing it."""

        with self._lock:
            existing = self._documents.get(document.document_id)
            if existing is None:
                self._documents[document.document_id] = LogicalDocumentRecord(
                    document_id=document.document_id,
                    tenant_id=document.tenant_id,
                    owner_user_id=document.owner_user_id,
                    state=document.state,
                    display_name=display_name,
                    revision=0,
                    active_document_version_id=None,
                )

    def add_legacy_mapping(
        self, *, tenant_id: str, legacy_system: str, legacy_record_id: str, document_id: UUID
    ) -> None:
        with self._lock:
            self._legacy_mappings[(tenant_id, legacy_system, legacy_record_id)] = document_id

    def resolve_legacy_document(
        self, *, tenant_id: str, legacy_system: str, legacy_record_id: str, owner_user_id: UUID
    ) -> LogicalDocumentRecord | None:
        with self._lock:
            document_id = self._legacy_mappings.get((tenant_id, legacy_system, legacy_record_id))
            if document_id is None:
                return None
            return self._require_document(document_id, tenant_id, owner_user_id)

    def get_document(
        self, *, document_id: UUID, tenant_id: str, owner_user_id: UUID
    ) -> LogicalDocumentRecord:
        with self._lock:
            return self._require_document(document_id, tenant_id, owner_user_id)

    def rename_document(
        self,
        *,
        document_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
        display_name: str,
        expected_revision: int,
    ) -> LogicalDocumentRecord:
        """Change logical metadata only; no source version is created."""

        with self._lock:
            document = self._require_mutable_document(document_id, tenant_id, owner_user_id)
            self._require_revision(document, expected_revision)
            updated = LogicalDocumentRecord(
                **{**document.__dict__, "display_name": display_name, "revision": document.revision + 1}
            )
            self._documents[document_id] = updated
            return updated

    def add_source_version(
        self,
        *,
        document_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
        artifact: SourceArtifactCommand,
        expected_revision: int,
        activate: bool = True,
    ) -> DocumentVersionRecord:
        """Append an immutable source version and atomically select it when requested."""

        with self._lock:
            document = self._require_mutable_document(document_id, tenant_id, owner_user_id)
            command_key = (tenant_id, document_id, artifact.idempotency_key)
            prior = self._version_commands.get(command_key)
            if prior is not None:
                if prior[0] != artifact:
                    raise DocumentVersioningError("idempotency_key_payload_mismatch")
                return self._find_version(document_id, prior[1])
            self._require_revision(document, expected_revision)
            prior_versions = self._versions.setdefault(document_id, [])
            version = DocumentVersionRecord(
                document_version_id=uuid4(),
                document_id=document_id,
                tenant_id=tenant_id,
                version_number=len(prior_versions) + 1,
                version_state="current" if activate else "superseded",
                supersedes_document_version_id=document.active_document_version_id,
                source_artifact_id=uuid4(),
            )
            if activate:
                prior_versions[:] = [
                DocumentVersionRecord(**{**item.__dict__, "version_state": "superseded"})
                    if item.version_state == "current"
                    else item
                    for item in prior_versions
                ]
                self._documents[document_id] = LogicalDocumentRecord(
                    **{
                        **document.__dict__,
                        "active_document_version_id": version.document_version_id,
                        "revision": document.revision + 1,
                    }
                )
            prior_versions.append(version)
            self._version_commands[command_key] = (artifact, version.document_version_id)
            return version

    def list_versions(
        self, *, document_id: UUID, tenant_id: str, owner_user_id: UUID
    ) -> list[DocumentVersionRecord]:
        with self._lock:
            self._require_document(document_id, tenant_id, owner_user_id)
            return sorted(self._versions.get(document_id, []), key=lambda item: item.version_number)

    def get_active_version(
        self, *, document_id: UUID, tenant_id: str, owner_user_id: UUID
    ) -> DocumentVersionRecord | None:
        with self._lock:
            document = self._require_document(document_id, tenant_id, owner_user_id)
            if document.active_document_version_id is None:
                return None
            return self._find_version(document_id, document.active_document_version_id)

    def activate_version(
        self,
        *,
        document_id: UUID,
        document_version_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
        expected_revision: int,
    ) -> LogicalDocumentRecord:
        with self._lock:
            document = self._require_mutable_document(document_id, tenant_id, owner_user_id)
            self._require_revision(document, expected_revision)
            target = self._find_version(document_id, document_version_id)
            versions = self._versions[document_id]
            self._versions[document_id] = [
                DocumentVersionRecord(**{**item.__dict__, "version_state": "current"})
                if item.document_version_id == target.document_version_id
                else DocumentVersionRecord(**{**item.__dict__, "version_state": "superseded"})
                if item.version_state == "current"
                else item
                for item in versions
            ]
            updated = LogicalDocumentRecord(
                **{
                    **document.__dict__,
                    "active_document_version_id": target.document_version_id,
                    "revision": document.revision + 1,
                }
            )
            self._documents[document_id] = updated
            return updated

    def _require_document(self, document_id: UUID, tenant_id: str, owner_user_id: UUID) -> LogicalDocumentRecord:
        document = self._documents.get(document_id)
        if document is None or document.tenant_id != tenant_id or document.owner_user_id != owner_user_id:
            raise DocumentVersioningError("document_not_found")
        return document

    def _require_mutable_document(self, document_id: UUID, tenant_id: str, owner_user_id: UUID) -> LogicalDocumentRecord:
        document = self._require_document(document_id, tenant_id, owner_user_id)
        if document.state in {"eligible_for_purge", "purged"}:
            raise DocumentVersioningError("document_lifecycle_blocks_mutation")
        return document

    @staticmethod
    def _require_revision(document: LogicalDocumentRecord, expected_revision: int) -> None:
        if document.revision != expected_revision:
            raise DocumentVersioningError("stale_document_revision")

    def _find_version(self, document_id: UUID, document_version_id: UUID) -> DocumentVersionRecord:
        for version in self._versions.get(document_id, []):
            if version.document_version_id == document_version_id:
                return version
        raise DocumentVersioningError("document_version_not_found")
