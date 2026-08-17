"""Expose document_ai ingestion boundary endpoints."""

import os
from uuid import UUID
from typing import cast
from typing import Literal
from typing import Protocol
from typing import Annotated
from hashlib import sha256
from pathlib import Path as PathlibPath
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from tempfile import SpooledTemporaryFile
from collections.abc import Callable

from dotenv import load_dotenv
from fastapi import Body
from fastapi import Query
from fastapi import Header
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.schedulers.background import BackgroundScheduler

from shared.authz.rbac import Principal
from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.authz.rbac import AUTHORIZATION_HEADER_NAME
from shared.authz.rbac import require_authenticated_principal
from shared.authz.rbac import build_authorized_principal_dependency
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from shared.idempotency.idempotency import require_idempotency_key
from services.document_ai.app.config import get_storage_endpoint_url
from services.document_ai.app.config import is_allowed_upload_mime_type
from services.document_ai.app.config import DEFAULT_STORAGE_ENDPOINT_URL
from services.document_ai.app.config import get_document_ai_runtime_mode
from services.document_ai.app.config import get_document_ai_worker_poll_interval_seconds
from services.document_ai.app.config import validate_document_ai_production_configuration
from services.document_ai.app.config import get_document_ai_worker_empty_queue_backoff_seconds
from services.document_ai.app.config import get_document_ai_worker_discovery_failure_backoff_seconds
from services.document_ai.app.outbox import ProcessingOutboxRelay
from services.document_ai.app.outbox import ProcessingOutboxRepository
from services.document_ai.app.metrics import DocumentAIMetricsEmitter
from services.document_ai.app.metrics import get_default_metrics_emitter
from services.document_ai.app.metrics import DOCUMENT_INGESTION_FAILURES_TOTAL
from services.document_ai.app.metrics import DOCUMENT_INGESTION_REQUESTS_TOTAL
from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.storage_keys import build_tenant_document_download_object_key
from services.document_ai.app.signed_access import SignedAccessStoreProtocol
from services.document_ai.app.signed_access import SignedDownloadAccessError
from services.document_ai.app.signed_access import PersistentSignedAccessStore
from services.document_ai.app.signed_access import SIGNED_DOWNLOAD_SECRET_ENV_VAR
from services.document_ai.app.signed_access import get_default_signed_access_store
from services.document_ai.app.signed_access import issue_signed_download_capability
from services.document_ai.app.signed_access import SignedDownloadCapabilityEnvelope
from services.document_ai.app.signed_access import validate_signed_download_capability
from services.document_ai.app.signed_access import decode_signed_download_capability_token
from services.document_ai.app.signed_access import SignedDownloadCapabilityValidationRequest
from services.document_ai.app.signed_access import SignedDownloadCapabilityValidationEnvelope
from services.document_ai.app.document_audit import LifecycleActionName
from services.document_ai.app.document_audit import ComplianceOverrideAction
from services.document_ai.app.document_audit import InMemoryDocumentAuditBackend
from services.document_ai.app.document_audit import PersistentDocumentAuditBackend
from services.document_ai.app.document_audit import configure_document_audit_backend
from services.document_ai.app.document_audit import emit_document_lifecycle_audit_evidence
from services.document_ai.app.document_audit import emit_document_compliance_override_audit_evidence
from services.document_ai.app.worker_polling import ProcessingWorkPollingPolicy
from services.document_ai.app.worker_polling import BoundedProcessingWorkPollingLoop
from services.document_ai.app.worker_polling import DocumentAIWorkerPollingController
from services.document_ai.app.worker_polling import ProcessingWorkCandidateHandoffProtocol
from services.document_ai.app.exact_retrieval import ExactRetrievalRequest
from services.document_ai.app.exact_retrieval import ExactRetrievalEnvelope
from services.document_ai.app.exact_retrieval import ExactRetrievalRepository
from services.document_ai.app.logging_context import emit_document_structured_log
from services.document_ai.app.storage_adapter import InMemoryStorageAdapter
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.storage_adapter import StorageAdapterPermanentError
from services.document_ai.app.storage_adapter import StorageAdapterTransientError
from services.document_ai.app.storage_adapter import build_runtime_storage_adapter
from services.document_ai.app.upload_sessions import UploadSessionRecord
from services.document_ai.app.upload_sessions import build_upload_session
from services.document_ai.app.upload_sessions import UploadSessionResponse
from services.document_ai.app.upload_sessions import get_upload_session_record
from services.document_ai.app.upload_sessions import is_upload_session_expired
from services.document_ai.app.upload_sessions import UploadSessionRequestError
from services.document_ai.app.upload_sessions import UploadSessionTraceability
from services.document_ai.app.upload_sessions import UploadSessionConflictError
from services.document_ai.app.upload_sessions import UploadSessionCreateRequest
from services.document_ai.app.upload_sessions import UploadSessionStoreProtocol
from services.document_ai.app.upload_sessions import PersistentUploadSessionStore
from services.document_ai.app.upload_sessions import mark_upload_session_completed
from services.document_ai.app.upload_sessions import get_default_upload_session_store
from services.document_ai.app.hybrid_retrieval import HybridRetrievalRequest
from services.document_ai.app.hybrid_retrieval import HybridRetrievalEnvelope
from services.document_ai.app.hybrid_retrieval import HybridRetrievalRepository
from services.document_ai.app.document_bindings import DocumentBindingRequest
from services.document_ai.app.document_bindings import DocumentBindingEnvelope
from services.document_ai.app.document_bindings import DocumentBindingListEnvelope
from services.document_ai.app.document_bindings import DocumentBindingStoreProtocol
from services.document_ai.app.document_bindings import InMemoryDocumentBindingStore
from services.document_ai.app.document_bindings import PersistentDocumentBindingStore
from services.document_ai.app.document_registry import to_document_record
from services.document_ai.app.document_registry import ExecutePurgeRequest
from services.document_ai.app.document_registry import DocumentListEnvelope
from services.document_ai.app.document_registry import DocumentRecordEnvelope
from services.document_ai.app.document_registry import CompletionConflictError
from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.document_registry import UploadCompletionRequest
from services.document_ai.app.document_registry import update_document_metadata
from services.document_ai.app.document_registry import CompletionValidationError
from services.document_ai.app.document_registry import MarkEligibleForPurgeRequest
from services.document_ai.app.document_registry import DocumentRetentionActionError
from services.document_ai.app.document_registry import DocumentMetadataConflictError
from services.document_ai.app.document_registry import DocumentMetadataUpdateRequest
from services.document_ai.app.document_registry import DocumentRegistryStoreProtocol
from services.document_ai.app.document_registry import apply_document_retention_action
from services.document_ai.app.document_registry import PersistentDocumentRegistryStore
from services.document_ai.app.document_registry import get_default_source_artifact_store
from services.document_ai.app.document_registry import get_default_document_registry_store
from services.document_ai.app.document_registry import register_durable_upload_confirmation
from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.security_controls import REQUIRED_ENCRYPTION_HEADERS
from services.document_ai.app.document_lifecycle import DocumentLifecycleState
from services.document_ai.app.document_lifecycle import DocumentStateTransitionError
from services.document_ai.app.document_lifecycle import is_document_compliance_lock_active
from services.document_ai.app.document_lifecycle import build_document_lifecycle_event_name
from services.document_ai.app.processing_workers import ProcessingWorkerRepository
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRequest
from services.document_ai.app.semantic_retrieval import SemanticRetrievalEnvelope
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRepository
from services.document_ai.app.compliance_override import ComplianceOverrideError
from services.document_ai.app.compliance_override import reject_compliance_override
from services.document_ai.app.compliance_override import approve_compliance_override
from services.document_ai.app.compliance_override import request_compliance_override
from services.document_ai.app.compliance_override import ComplianceOverrideStoreProtocol
from services.document_ai.app.compliance_override import ComplianceOverrideRequestPayload
from services.document_ai.app.compliance_override import ComplianceOverrideDecisionPayload
from services.document_ai.app.compliance_override import PersistentComplianceOverrideStore
from services.document_ai.app.compliance_override import ComplianceOverrideResponseEnvelope
from services.document_ai.app.compliance_override import get_default_compliance_override_store
from services.document_ai.app.compliance_override import consume_compliance_override_for_action
from services.document_ai.app.document_foundation import SourceArtifactStoreProtocol
from services.document_ai.app.document_foundation import PersistentDocumentFoundationStore
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.persistence_support import get_document_ai_connection_pool
from services.document_ai.app.persistence_support import get_document_ai_persistence_mode
from services.document_ai.app.persistence_support import close_document_ai_connection_pool
from services.document_ai.app.persistence_support import resolve_document_ai_persistence_status
from services.document_ai.app.persistence_support import DocumentAITransactionAmbiguousResultError
from services.document_ai.app.document_purge_safety import evaluate_document_purge_safety
from services.document_ai.app.document_purge_safety import DocumentPurgeSafetyDryRunEnvelope
from services.document_ai.app.document_purge import execute_document_purge
from services.document_ai.app.document_purge import recover_pending_document_purges
from services.document_ai.app.document_access_policy import DocumentAccessAction
from services.document_ai.app.document_access_policy import evaluate_document_access_policy
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository
from services.document_ai.app.processing_state_reconciler import ProcessingStateReconciler

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

INVALID_UPLOAD_SESSION_REQUEST = "invalid_upload_session_request"
INVALID_UPLOAD_COMPLETION_REQUEST = "invalid_upload_completion_request"
UPLOAD_SESSION_FORBIDDEN = "upload_session_forbidden"
UPLOAD_SESSION_INVALID_STATE = "upload_session_invalid_state"
UPLOAD_SESSION_NOT_FOUND = "upload_session_not_found"
UPLOAD_SESSION_EXPIRED = "upload_session_expired"
IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
UPLOAD_SESSION_DOCUMENT_MISMATCH = "upload_session_document_mismatch"
INVALID_DOCUMENT_STATE_TRANSITION = "invalid_document_state_transition"
STORAGE_RETRYABLE_FAILURE = "storage_retryable_failure"
STORAGE_NON_RETRYABLE_FAILURE = "storage_non_retryable_failure"
DOCUMENT_NOT_FOUND = "document_not_found"
INVALID_DOCUMENT_RETRIEVAL_REQUEST = "invalid_document_retrieval_request"
INVALID_DOCUMENT_RETENTION_REQUEST = "invalid_document_retention_request"
DOCUMENT_ACCESS_DENIED = "document_access_denied"
INVALID_DOWNLOAD_ACCESS_REQUEST = "invalid_download_access_request"
DOWNLOAD_ACCESS_DENIED = "download_access_denied"
DOWNLOAD_CAPABILITY_REJECTED = "download_capability_rejected"
INVALID_COMPLIANCE_OVERRIDE_REQUEST = "invalid_compliance_override_request"
COMPLIANCE_OVERRIDE_REJECTED = "compliance_override_rejected"
DOCUMENT_RETENTION_ACTION_FORBIDDEN = "document_retention_action_forbidden"
DOCUMENT_RETENTION_LOCK_ACTIVE = "document_retention_lock_active"
DOCUMENT_RESTORE_NOT_SUPPORTED = "document_restore_not_supported"
INVALID_DOCUMENT_BINDING_REQUEST = "invalid_document_binding_request"
DOCUMENT_BINDING_FORBIDDEN = "document_binding_forbidden"
DOCUMENT_BINDING_LIFECYCLE_BLOCKED = "document_binding_lifecycle_blocked"
DOCUMENT_PERSISTENCE_AMBIGUOUS_RESULT = "document_ai_persistence_ambiguous_result"
EXACT_RETRIEVAL_UNAVAILABLE = "exact_retrieval_unavailable"
SEMANTIC_RETRIEVAL_UNAVAILABLE = "semantic_retrieval_unavailable"
HYBRID_RETRIEVAL_UNAVAILABLE = "hybrid_retrieval_unavailable"
_ALLOWED_DOCUMENT_STATES: frozenset[str] = frozenset(
    {
        "uploaded",
        "active",
        "trashed",
        "purge_pending",
        "processing",
        "validated",
        "eligible_for_purge",
        "purged",
    }
)
_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES: tuple[str, ...] = (
    "document_ai_upload_sessions",
    "document_ai_documents",
    "document_ai_completion_idempotency",
    "document_ai_signed_access_usage",
    "document_ai_compliance_overrides",
    "document_ai_lifecycle_audit_evidence",
    "document_ai_compliance_override_audit_evidence",
    "document_ai_purge_operations",
    "document_ai_purge_targets",
    "document_ai_purge_attempts",
    "document_ai_document_bindings",
    "document_ai_processing_outbox",
    "document_ai_processing_outbox_attempts",
    "document_ai_processing_work_items",
    "document_ai_processing_attempts",
    "document_ai_processing_checkpoints",
)

ROUTER = APIRouter()


class _EventHandlerApplicationProtocol(Protocol):
    def add_event_handler(self, event_type: str, func: Callable[[], object]) -> None:
        """Register a synchronous application lifecycle callback."""

        ...


_DEFAULT_DOCUMENT_BINDING_STORE = InMemoryDocumentBindingStore()
_DOCUMENT_AI_ALLOWED_ROLES = frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"})
require_document_ai_auth_context_principal = build_authorized_principal_dependency(
    allowed_roles=_DOCUMENT_AI_ALLOWED_ROLES,
    allow_delegation=False,
)


def require_document_ai_principal(
    request: Request,
    auth_context_header: str | None = Header(default=None, alias=AUTH_CONTEXT_HEADER_NAME),
    authorization: str | None = Header(default=None, alias=AUTHORIZATION_HEADER_NAME),
) -> Principal:
    """Resolve principal using canonical auth-context with Authorization fallback."""

    if auth_context_header is not None and auth_context_header.strip():
        return require_document_ai_auth_context_principal(request, auth_context_header)

    return require_authenticated_principal(request, authorization)


def get_upload_session_store(request: Request) -> UploadSessionStoreProtocol:
    """Resolve optional test override or default upload-session idempotency store."""

    configured_store = getattr(request.app.state, "upload_session_store", None)
    if configured_store is not None:
        return cast(UploadSessionStoreProtocol, configured_store)

    return get_default_upload_session_store()


def get_document_registry_store(request: Request) -> DocumentRegistryStoreProtocol:
    """Resolve optional test override or default document registry store."""

    configured_store = getattr(request.app.state, "document_registry_store", None)
    if configured_store is not None:
        return cast(DocumentRegistryStoreProtocol, configured_store)

    return get_default_document_registry_store()


def get_document_binding_store(request: Request) -> DocumentBindingStoreProtocol:
    """Resolve the authoritative document-binding repository."""

    configured_store = getattr(request.app.state, "document_binding_store", None)
    if configured_store is not None:
        return cast(DocumentBindingStoreProtocol, configured_store)
    return _DEFAULT_DOCUMENT_BINDING_STORE


def get_exact_retrieval_repository(request: Request) -> ExactRetrievalRepository:
    """Resolve the persistent canonical retrieval boundary."""

    repository = getattr(request.app.state, "exact_retrieval_repository", None)
    if repository is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=EXACT_RETRIEVAL_UNAVAILABLE,
            message="Exact canonical retrieval is unavailable in this runtime.",
            reason="exact_retrieval_persistence_unavailable",
            details={},
        )
    return cast(ExactRetrievalRepository, repository)


def get_semantic_retrieval_repository(request: Request) -> SemanticRetrievalRepository:
    """Resolve the persistent authorized semantic-discovery boundary."""

    repository = getattr(request.app.state, "semantic_retrieval_repository", None)
    if repository is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=SEMANTIC_RETRIEVAL_UNAVAILABLE,
            message="Semantic canonical retrieval is unavailable in this runtime.",
            reason="semantic_retrieval_persistence_unavailable",
            details={},
        )
    return cast(SemanticRetrievalRepository, repository)


def get_hybrid_retrieval_repository(request: Request) -> HybridRetrievalRepository:
    """Resolve the fused hybrid retrieval boundary."""

    repository = getattr(request.app.state, "hybrid_retrieval_repository", None)
    if repository is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=HYBRID_RETRIEVAL_UNAVAILABLE,
            message="Hybrid canonical retrieval is unavailable in this runtime.",
            reason="hybrid_retrieval_persistence_unavailable",
            details={},
        )
    return cast(HybridRetrievalRepository, repository)


def get_source_artifact_store(request: Request) -> SourceArtifactStoreProtocol:
    """Resolve the sole source-artifact authority for the current runtime."""

    configured_store = getattr(request.app.state, "source_artifact_store", None)
    if configured_store is not None:
        return cast(SourceArtifactStoreProtocol, configured_store)
    return get_default_source_artifact_store()


def get_storage_adapter(request: Request) -> StorageAdapterProtocol:
    """Resolve optional test override or default storage adapter."""

    configured_adapter = getattr(request.app.state, "storage_adapter", None)
    if configured_adapter is not None:
        return cast(StorageAdapterProtocol, configured_adapter)

    if get_document_ai_runtime_mode() == "production":
        adapter = build_runtime_storage_adapter()
        request.app.state.storage_adapter = adapter
        return adapter

    configured_endpoint = get_storage_endpoint_url().strip()
    endpoint_url = configured_endpoint
    if not configured_endpoint or configured_endpoint == DEFAULT_STORAGE_ENDPOINT_URL:
        endpoint_url = str(request.base_url).rstrip("/")

    return InMemoryStorageAdapter(endpoint_url=endpoint_url)


def get_signed_access_store(request: Request) -> SignedAccessStoreProtocol:
    """Resolve optional test override or default signed access store."""

    configured_store = getattr(request.app.state, "signed_access_store", None)
    if configured_store is not None:
        return cast(SignedAccessStoreProtocol, configured_store)

    return get_default_signed_access_store()


def get_compliance_override_store(request: Request) -> ComplianceOverrideStoreProtocol:
    """Resolve optional test override or default compliance-override store."""

    configured_store = getattr(request.app.state, "compliance_override_store", None)
    if configured_store is not None:
        return cast(ComplianceOverrideStoreProtocol, configured_store)

    return get_default_compliance_override_store()


def get_metrics_emitter(request: Request) -> DocumentAIMetricsEmitter:
    """Resolve optional test override or default metrics emitter."""

    configured_emitter = getattr(request.app.state, "metrics_emitter", None)
    if configured_emitter is not None:
        return cast(DocumentAIMetricsEmitter, configured_emitter)

    return get_default_metrics_emitter()


@ROUTER.get("/health")
def document_ai_health_endpoint(request: Request) -> JSONResponse:
    """Return readiness after target durable storage is usable."""

    persistence_mode = get_document_ai_persistence_mode()
    persistence_ready = persistence_mode == "in_memory"
    if persistence_mode == "persistent":
        database_url = load_document_ai_database_url()
        persistence_ready = bool(database_url) and (
            resolve_document_ai_persistence_status(
                database_url=database_url,
                required_tables=_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES,
            )
            == "ready"
        )
    return JSONResponse(
        status_code=200 if persistence_ready else 503,
        content={
            "status": "ready" if persistence_ready else "not_ready",
            "runtime_mode": get_document_ai_runtime_mode(),
            "persistence_mode": persistence_mode,
            "durable_storage": persistence_mode == "persistent",
            "persistence_ready": persistence_ready,
        },
    )


@ROUTER.put("/upload/{object_key:path}")
async def upload_storage_object_endpoint(
    request: Request,
    object_key: str,
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
) -> Response:
    """Persist uploaded file bytes into local file-backed storage."""

    if get_document_ai_runtime_mode() == "production":
        # SR-001/SR-002: production uploads use the exact short-lived R2
        # capability. This development endpoint must not accept caller keys.
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message="Direct storage uploads are unavailable in production.",
            reason="direct_storage_upload_not_available",
            details={},
        )

    for header_name, expected_value in REQUIRED_ENCRYPTION_HEADERS.items():
        provided_value = request.headers.get(header_name, "").strip()
        if provided_value != expected_value:
            raise _create_document_ai_http_error(
                request=request,
                status_code=400,
                error_code=INVALID_UPLOAD_SESSION_REQUEST,
                message="Storage upload request is missing required encryption headers.",
                reason="missing_required_storage_encryption_header",
                details={"header": header_name},
            )

    content_type = request.headers.get("Content-Type", "").strip()
    if not is_allowed_upload_mime_type(content_type):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message="Storage upload content type is invalid.",
            reason="invalid_storage_upload_content_type",
            details={"content_type": content_type},
        )

    payload_stream = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    payload_size = 0
    async for chunk in request.stream():
        if chunk:
            payload_stream.write(chunk)
            payload_size += len(chunk)
    if payload_size <= 0:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message="Storage upload payload is empty.",
            reason="empty_storage_upload_payload",
            details={"object_key": object_key},
        )
    payload_stream.seek(0)

    storage_adapter.store_upload_object_filelike(
        object_key=object_key,
        payload_stream=payload_stream,
        content_type=content_type,
        payload_size=payload_size,
    )
    return Response(status_code=200)


@ROUTER.get("/download/{object_key:path}")
def download_storage_object_endpoint(
    request: Request,
    object_key: str,
    capability_token: Annotated[str, Query(min_length=1)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
) -> FileResponse:
    """Serve one stored file when a valid signed download capability is presented."""

    claims = _decode_direct_download_capability(
        request=request,
        object_key=object_key,
        capability_token=capability_token,
    )
    expected_object_key = build_tenant_document_download_object_key(
        claims.tenant_id, claims.document_id
    )
    if object_key != expected_object_key:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOWNLOAD_CAPABILITY_REJECTED,
            message="Signed download capability is invalid for this object.",
            reason="capability_scope_mismatch",
            details={
                "object_key": object_key,
                "expected_object_key": expected_object_key,
            },
        )

    scoped_document = document_registry_store.get_document(claims.document_id)
    if scoped_document is None or scoped_document.tenant_id != claims.tenant_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=DOCUMENT_NOT_FOUND,
            message="Document was not found.",
            reason="document_not_found_or_forbidden",
            details={"document_id": str(claims.document_id)},
        )
    if scoped_document.state != "active":
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOWNLOAD_CAPABILITY_REJECTED,
            message="Signed download capability is not available for this document state.",
            reason="document_lifecycle_blocked",
            details={
                "document_id": str(claims.document_id),
                "document_state": scoped_document.state,
            },
        )

    try:
        object_path, content_type = storage_adapter.resolve_download_object(object_key)
    except StorageAdapterPermanentError as error:
        if error.reason == "storage_object_not_found":
            raise _create_document_ai_http_error(
                request=request,
                status_code=404,
                error_code=DOCUMENT_NOT_FOUND,
                message="Stored document file was not found.",
                reason="storage_object_not_found",
                details={"object_key": object_key},
            ) from error
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOWNLOAD_ACCESS_REQUEST,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
        ) from error

    return FileResponse(
        path=object_path,
        media_type=content_type,
        filename=object_path.name,
    )


@ROUTER.post("/v1/document-bindings", response_model=DocumentBindingEnvelope, status_code=201)
def create_document_binding_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    document_binding_store: Annotated[
        DocumentBindingStoreProtocol, Depends(get_document_binding_store)
    ],
) -> DocumentBindingEnvelope:
    """Create an idempotent binding after tenant, owner, and lifecycle checks."""

    try:
        binding_request = DocumentBindingRequest.model_validate(payload)
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_BINDING_REQUEST,
            message="Document binding request is invalid.",
            reason="invalid_document_binding_request",
            details={"validation_error": str(error)},
        ) from error
    document = document_registry_store.get_document(binding_request.document_id)
    if document is None or document.tenant_id != principal.tenant_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=DOCUMENT_NOT_FOUND,
            message="Document was not found.",
            reason="document_not_found_or_forbidden",
            details={"document_id": str(binding_request.document_id)},
        )
    if document.owner_user_id != principal.user_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_BINDING_FORBIDDEN,
            message="Document binding is not permitted for this principal.",
            reason="document_owner_mismatch",
            details={"document_id": str(binding_request.document_id)},
        )
    if document.state not in {"uploaded", "processing", "validated"}:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOCUMENT_BINDING_LIFECYCLE_BLOCKED,
            message="Document lifecycle state does not permit a new binding.",
            reason="document_lifecycle_blocked",
            details={"document_id": str(binding_request.document_id)},
            current_state=document.state,
        )
    binding = document_binding_store.create(
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=binding_request,
        correlation_id=get_correlation_id(request),
    )
    emit_document_structured_log(
        event_name="document_binding_created",
        action="document_binding_create",
        status="success",
        trace_id=_build_trace_id_from_correlation(get_correlation_id(request)),
        correlation_id=get_correlation_id(request),
        document_id=binding.document_id,
        reason_code="bound",
        payload={
            "binding_id": str(binding.document_binding_id),
            "binding_role": binding.binding_role,
            "conversation_id": binding.conversation_id,
            "turn_id": binding.turn_id,
            "workflow_id": binding.workflow_id,
        },
    )
    return DocumentBindingEnvelope(binding=binding)


@ROUTER.get("/v1/document-bindings", response_model=DocumentBindingListEnvelope)
def list_document_bindings_endpoint(
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_binding_store: Annotated[
        DocumentBindingStoreProtocol, Depends(get_document_binding_store)
    ],
    conversation_id: Annotated[str | None, Query(min_length=1)] = None,
    turn_id: Annotated[str | None, Query(min_length=1)] = None,
    workflow_id: Annotated[str | None, Query(min_length=1)] = None,
) -> DocumentBindingListEnvelope:
    """Reload authorized durable bindings for one conversation, turn, or workflow."""

    if (conversation_id is None) == (workflow_id is None) or (
        turn_id is not None and conversation_id is None
    ):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_BINDING_REQUEST,
            message="Document binding target is invalid.",
            reason="invalid_document_binding_target",
            details={},
        )
    bindings = document_binding_store.list_for_target(
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
    )
    return DocumentBindingListEnvelope(bindings=bindings)


@ROUTER.post(
    "/v1/document-evidence/exact-retrievals",
    response_model=ExactRetrievalEnvelope,
)
def retrieve_exact_document_evidence_endpoint(
    request: Request,
    payload: Annotated[ExactRetrievalRequest, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    repository: Annotated[ExactRetrievalRepository, Depends(get_exact_retrieval_repository)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> ExactRetrievalEnvelope:
    """Retrieve active canonical chunks inside the authorized tenant/owner scope."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=resolved_tenant_id,
        actor_role=principal.role,
        document_owner_user_id=principal.user_id,
        document_tenant_id=resolved_tenant_id,
        action="retrieve_exact_evidence",
    )
    if decision["decision"] == "deny":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_ACCESS_DENIED,
            message="Exact evidence retrieval is not permitted for this principal.",
            reason=decision["reason"],
            details={"action": "retrieve_exact_evidence"},
        )
    try:
        evidence = repository.retrieve(
            tenant_id=resolved_tenant_id,
            owner_user_id=principal.user_id,
            request=payload,
        )
    except RuntimeError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=EXACT_RETRIEVAL_UNAVAILABLE,
            message="Exact canonical retrieval is temporarily unavailable.",
            reason=str(error),
            details={},
        ) from error
    return ExactRetrievalEnvelope(evidence=evidence)


def _retrieve_semantic_document_candidates(
    *,
    request: Request,
    payload: SemanticRetrievalRequest,
    principal: Principal,
    repository: SemanticRetrievalRepository,
    tenant_id: str | None,
    route_label: str,
) -> SemanticRetrievalEnvelope:
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=resolved_tenant_id,
        actor_role=principal.role,
        document_owner_user_id=principal.user_id,
        document_tenant_id=resolved_tenant_id,
        action="retrieve_semantic_candidates",
    )
    if decision["decision"] == "deny":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_ACCESS_DENIED,
            message="Semantic retrieval is not permitted for this principal.",
            reason=decision["reason"],
            details={"action": "retrieve_semantic_candidates"},
        )
    try:
        return repository.retrieve(
            tenant_id=resolved_tenant_id, owner_user_id=principal.user_id, request=payload
        )
    except EmbeddingProviderError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=SEMANTIC_RETRIEVAL_UNAVAILABLE,
            message=f"{route_label} is temporarily unavailable.",
            reason=error.reason,
            details={"retryable": error.retryable},
            retryable=error.retryable,
        ) from error
    except RuntimeError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=SEMANTIC_RETRIEVAL_UNAVAILABLE,
            message=f"{route_label} is temporarily unavailable.",
            reason=str(error),
            details={},
        ) from error


@ROUTER.post(
    "/v1/document-evidence/semantic-retrievals",
    response_model=SemanticRetrievalEnvelope,
)
def retrieve_semantic_document_candidates_endpoint(
    request: Request,
    payload: Annotated[SemanticRetrievalRequest, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    repository: Annotated[SemanticRetrievalRepository, Depends(get_semantic_retrieval_repository)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> SemanticRetrievalEnvelope:
    """Discover only authorized semantic candidates; callers must not treat them as evidence."""

    return _retrieve_semantic_document_candidates(
        request=request,
        payload=payload,
        principal=principal,
        repository=repository,
        tenant_id=tenant_id,
        route_label="Semantic canonical retrieval",
    )


@ROUTER.post(
    "/v1/document-evidence/hybrid-retrievals",
    response_model=HybridRetrievalEnvelope,
)
def retrieve_hybrid_document_candidates_endpoint(
    request: Request,
    payload: Annotated[HybridRetrievalRequest, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    repository: Annotated[HybridRetrievalRepository, Depends(get_hybrid_retrieval_repository)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> HybridRetrievalEnvelope:
    """Retrieve fused semantic and exact candidates without adjudicating evidence."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=resolved_tenant_id,
        actor_role=principal.role,
        document_owner_user_id=principal.user_id,
        document_tenant_id=resolved_tenant_id,
        action="retrieve_hybrid_candidates",
    )
    if decision["decision"] == "deny":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_ACCESS_DENIED,
            message="Hybrid retrieval is not permitted for this principal.",
            reason=decision["reason"],
            details={"action": "retrieve_hybrid_candidates"},
        )
    try:
        return repository.retrieve(
            tenant_id=resolved_tenant_id,
            owner_user_id=principal.user_id,
            request=payload,
        )
    except EmbeddingProviderError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=HYBRID_RETRIEVAL_UNAVAILABLE,
            message="Hybrid canonical retrieval is temporarily unavailable.",
            reason=error.reason,
            details={"retryable": error.retryable},
            retryable=error.retryable,
        ) from error
    except RuntimeError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=HYBRID_RETRIEVAL_UNAVAILABLE,
            message="Hybrid canonical retrieval is temporarily unavailable.",
            reason=str(error),
            details={},
        ) from error


@ROUTER.post("/v1/documents/upload-sessions", response_model=UploadSessionResponse, status_code=201)
def create_upload_session_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    upload_session_store: Annotated[UploadSessionStoreProtocol, Depends(get_upload_session_store)],
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
    metrics_emitter: Annotated[DocumentAIMetricsEmitter, Depends(get_metrics_emitter)],
) -> UploadSessionResponse:
    """Create one deterministic document upload session with idempotency replay support."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    upload_session_request = _parse_upload_session_request(request=request, payload=payload)
    lane_scope = upload_session_request.lane_hint or "unspecified"
    metrics_emitter.increment_counter_non_blocking(
        DOCUMENT_INGESTION_REQUESTS_TOTAL,
        dimensions={
            "action": "upload_session_create",
            "status": "attempt",
            "reason_code": "request_received",
            "lane_scope": lane_scope,
        },
    )
    if upload_session_request.owner_user_id != principal.user_id:
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            reason_code="owner_user_mismatch",
            payload={"lane_scope": lane_scope},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "rejected",
                "reason_code": "owner_user_mismatch",
                "lane_scope": lane_scope,
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=UPLOAD_SESSION_FORBIDDEN,
            message="Upload session ownership context does not match authenticated principal.",
            reason="owner_user_mismatch",
            details={
                "owner_user_id": str(upload_session_request.owner_user_id),
                "principal_user_id": str(principal.user_id),
            },
        )

    try:
        response = build_upload_session(
            upload_session_request=upload_session_request,
            principal_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            upload_session_store=upload_session_store,
            storage_adapter=storage_adapter,
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_REQUESTS_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "success",
                "reason_code": "created",
                "lane_scope": lane_scope,
            },
        )
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="success",
            trace_id=response.traceability.trace_id,
            correlation_id=response.traceability.correlation_id,
            document_id=response.document_id,
            reason_code="created",
            payload={"lane_scope": lane_scope, "session_id": str(response.session_id)},
        )
        return response
    except UploadSessionConflictError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            reason_code=error.reason,
            payload={"lane_scope": lane_scope},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": lane_scope,
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=IDEMPOTENCY_KEY_CONFLICT,
            message="Idempotency key conflicts with an existing upload-session request.",
            reason=error.reason,
            details=error.details(),
        ) from error
    except UploadSessionRequestError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            reason_code=error.reason,
            payload={"lane_scope": lane_scope},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": lane_scope,
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
        ) from error
    except StorageAdapterTransientError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="failed",
            trace_id=trace_id,
            correlation_id=correlation_id,
            reason_code=error.reason,
            payload={"lane_scope": lane_scope},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": lane_scope,
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=True,
        ) from error
    except StorageAdapterPermanentError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_session",
            action="upload_session_create",
            status="failed",
            trace_id=trace_id,
            correlation_id=correlation_id,
            reason_code=error.reason,
            payload={"lane_scope": lane_scope},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_session_create",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": lane_scope,
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_NON_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=False,
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/upload-completion",
    response_model=DocumentRecordEnvelope,
)
def register_upload_completion_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    upload_session_store: Annotated[UploadSessionStoreProtocol, Depends(get_upload_session_store)],
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    source_artifact_store: Annotated[
        SourceArtifactStoreProtocol, Depends(get_source_artifact_store)
    ],
    metrics_emitter: Annotated[DocumentAIMetricsEmitter, Depends(get_metrics_emitter)],
) -> DocumentRecordEnvelope:
    """Register upload completion and persist canonical document metadata."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    upload_completion_request = _parse_upload_completion_request(request=request, payload=payload)
    metrics_emitter.increment_counter_non_blocking(
        DOCUMENT_INGESTION_REQUESTS_TOTAL,
        dimensions={
            "action": "upload_completion_register",
            "status": "attempt",
            "reason_code": "request_received",
            "lane_scope": "unspecified",
        },
    )
    try:
        session_id = upload_completion_request.resolved_session_id()
    except ValueError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code="missing_session_id",
            payload={},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": "missing_session_id",
                "lane_scope": "unspecified",
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_COMPLETION_REQUEST,
            message="Invalid upload-completion request payload.",
            reason="missing_session_id",
            details={},
        ) from error

    session_record = _resolve_session_record(
        request=request,
        session_id=session_id,
        upload_session_store=upload_session_store,
        allow_consumed_session=document_registry_store.get_completion(idempotency_key) is not None,
    )
    expected_object_key = build_tenant_document_object_key(
        session_record.tenant_id, session_record.document_id
    )
    if upload_completion_request.object_key != expected_object_key:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_COMPLETION_REQUEST,
            message="Upload completion object key does not match the governed storage key.",
            reason="object_key_mismatch",
            details={
                "expected_object_key": expected_object_key,
                "object_key": upload_completion_request.object_key,
            },
        )
    _enforce_session_guards(
        request=request,
        document_id=document_id,
        principal=principal,
        session_record=session_record,
    )
    try:
        storage_adapter.verify_upload_object(
            tenant_id=session_record.tenant_id,
            owner_user_id=session_record.owner_user_id,
            object_key=expected_object_key,
            checksum_sha256=upload_completion_request.checksum_sha256,
            size_bytes=upload_completion_request.size_bytes,
            content_type=upload_completion_request.content_type,
        )
    except StorageAdapterTransientError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="failed",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason,
            payload={},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": "unspecified",
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=True,
        ) from error
    except StorageAdapterPermanentError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="failed",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason,
            payload={},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": "unspecified",
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_NON_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=False,
        ) from error

    try:
        response = register_durable_upload_confirmation(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_REQUESTS_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "success",
                "reason_code": "registered",
                "lane_scope": "unspecified",
            },
        )
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="success",
            trace_id=response.traceability.trace_id,
            correlation_id=response.traceability.correlation_id,
            document_id=response.document.document_id,
            reason_code="registered",
            payload={"state": response.document.state},
        )
        if not isinstance(document_registry_store, PersistentDocumentRegistryStore):
            mark_upload_session_completed(
                session_id=session_record.session_id,
                upload_session_store=upload_session_store,
            )
        return response
    except CompletionConflictError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason,
            payload={},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": "unspecified",
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=IDEMPOTENCY_KEY_CONFLICT,
            message="Upload completion request conflicts with existing deterministic state.",
            reason=error.reason,
            details=error.details(),
        ) from error
    except CompletionValidationError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason,
            payload={},
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": "unspecified",
            },
        )
        status_code = 403 if error.reason == "tenant_mismatch" else 400
        error_code = (
            UPLOAD_SESSION_FORBIDDEN
            if error.reason == "tenant_mismatch"
            else (INVALID_UPLOAD_COMPLETION_REQUEST)
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
        ) from error
    except DocumentStateTransitionError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="rejected",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason,
            payload={
                "current_state": error.current_state,
                "requested_state": error.requested_state,
            },
        )
        metrics_emitter.increment_counter_non_blocking(
            DOCUMENT_INGESTION_FAILURES_TOTAL,
            dimensions={
                "action": "upload_completion_register",
                "status": "rejected",
                "reason_code": error.reason,
                "lane_scope": "unspecified",
            },
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=INVALID_DOCUMENT_STATE_TRANSITION,
            message="Requested document state transition is not allowed.",
            reason=error.reason,
            details={},
            current_state=error.current_state,
            requested_state=error.requested_state,
        ) from error


@ROUTER.get(
    "/v1/documents",
    response_model=DocumentListEnvelope,
)
def list_documents_endpoint(
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    uploaded_from: Annotated[str | None, Query()] = None,
    uploaded_to: Annotated[str | None, Query()] = None,
    computation_id: Annotated[str | None, Query()] = None,
) -> DocumentListEnvelope:
    """List documents constrained to deterministic tenant/owner scope."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    list_access_decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=resolved_tenant_id,
        actor_role=principal.role,
        document_owner_user_id=None,
        document_tenant_id=resolved_tenant_id,
        action="list_documents",
    )
    if list_access_decision["decision"] == "deny":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_ACCESS_DENIED,
            message="Document action is not permitted for authenticated role.",
            reason=list_access_decision["reason"],
            details={"action": "list_documents"},
        )
    resolved_state = _resolve_document_state_filter(request=request, state=state)
    resolved_uploaded_from = _resolve_uploaded_datetime_filter(
        request=request,
        value=uploaded_from,
        field_name="uploaded_from",
    )
    resolved_uploaded_to = _resolve_uploaded_datetime_filter(
        request=request,
        value=uploaded_to,
        field_name="uploaded_to",
    )
    if (
        resolved_uploaded_from is not None
        and resolved_uploaded_to is not None
        and resolved_uploaded_from > resolved_uploaded_to
    ):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Uploaded date range is invalid for document retrieval.",
            reason="invalid_uploaded_range",
            details={},
        )
    resolved_computation_id = _resolve_computation_id_filter(
        request=request,
        computation_id=computation_id,
    )
    scoped_documents = document_registry_store.list_documents_for_scope(
        tenant_id=resolved_tenant_id,
        owner_user_id=principal.user_id,
        state=resolved_state,
        uploaded_from=resolved_uploaded_from,
        uploaded_to=resolved_uploaded_to,
        computation_id=resolved_computation_id,
    )
    # Ordinary library reads expose only usable documents.  Trash is an explicit
    # lifecycle-aware filter; purge-pending and purged identities are unavailable.
    if resolved_state is None:
        scoped_documents = [
            item
            for item in scoped_documents
            if item.state not in {"trashed", "purge_pending", "purged", "eligible_for_purge"}
        ]
    elif resolved_state in {"purge_pending", "purged"}:
        scoped_documents = []
    correlation_id = get_correlation_id(request)
    trace_id = _build_read_trace_id(
        correlation_id=correlation_id,
        operation="list_documents",
        tenant_id=resolved_tenant_id,
        principal_user_id=principal.user_id,
    )
    return DocumentListEnvelope(
        status="ok",
        documents=[to_document_record(record) for record in scoped_documents],
        traceability=_build_upload_traceability(trace_id=trace_id, correlation_id=correlation_id),
    )


@ROUTER.get(
    "/v1/documents/{document_id}",
    response_model=DocumentRecordEnvelope,
)
def retrieve_document_endpoint(
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Retrieve one document constrained to deterministic tenant/owner scope."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    scoped_document = _resolve_document_for_action_access_policy(
        request=request,
        document_id=document_id,
        action="get_document",
        principal=principal,
        tenant_id=resolved_tenant_id,
        document_registry_store=document_registry_store,
    )

    correlation_id = get_correlation_id(request)
    trace_id = _build_read_trace_id(
        correlation_id=correlation_id,
        operation="get_document",
        tenant_id=resolved_tenant_id,
        principal_user_id=principal.user_id,
        document_id=document_id,
    )
    return DocumentRecordEnvelope(
        status="ok",
        document=to_document_record(scoped_document),
        traceability=_build_upload_traceability(trace_id=trace_id, correlation_id=correlation_id),
    )


@ROUTER.patch("/v1/documents/{document_id}", response_model=DocumentRecordEnvelope)
def update_document_metadata_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[DocumentMetadataUpdateRequest, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Update only approved user-facing metadata with optimistic concurrency."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    document = _resolve_document_for_action_access_policy(
        request=request,
        document_id=document_id,
        action="get_document",
        principal=principal,
        tenant_id=resolved_tenant_id,
        document_registry_store=document_registry_store,
    )
    try:
        return update_document_metadata(
            document_record=document,
            request=payload,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            document_registry_store=document_registry_store,
        )
    except DocumentMetadataConflictError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=INVALID_DOCUMENT_STATE_TRANSITION,
            message="Document metadata command conflicts with current document state.",
            reason=error.reason,
            details={"document_id": str(document_id)},
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/download-capabilities",
    response_model=SignedDownloadCapabilityEnvelope,
    status_code=201,
)
def issue_document_download_capability_endpoint(
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> SignedDownloadCapabilityEnvelope:
    """Issue deterministic signed short-lived download capability for one scoped document."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    scoped_document = document_registry_store.get_document(document_id)
    if scoped_document is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=DOCUMENT_NOT_FOUND,
            message="Document was not found.",
            reason="document_not_found_or_forbidden",
            details={"document_id": str(document_id)},
        )
    if scoped_document.state != "active":
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOWNLOAD_CAPABILITY_REJECTED,
            message="Signed download capability is not available for this document state.",
            reason="document_lifecycle_blocked",
            details={
                "document_id": str(document_id),
                "document_state": scoped_document.state,
            },
        )
    access_decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=resolved_tenant_id,
        actor_role=principal.role,
        document_owner_user_id=scoped_document.owner_user_id,
        document_tenant_id=scoped_document.tenant_id,
        action="download_document",
    )
    if access_decision["decision"] == "deny":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOWNLOAD_ACCESS_DENIED,
            message="Caller is not authorized for signed document download access.",
            reason="unauthorized_download_access",
            details={
                "document_id": str(document_id),
                "policy_reason": access_decision["reason"],
            },
        )
    correlation_id = get_correlation_id(request)
    try:
        return issue_signed_download_capability(
            document_record=scoped_document,
            issued_to_user_id=principal.user_id,
            tenant_id=resolved_tenant_id,
            correlation_id=correlation_id,
            storage_adapter=storage_adapter,
            document_state=scoped_document.state,
        )
    except SignedDownloadAccessError as error:
        status_code, error_code = _map_signed_download_access_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error
    except StorageAdapterTransientError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=True,
        ) from error
    except StorageAdapterPermanentError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=STORAGE_NON_RETRYABLE_FAILURE,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            retryable=False,
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/download-capabilities/validate",
    response_model=SignedDownloadCapabilityValidationEnvelope,
)
def validate_document_download_capability_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    signed_access_store: Annotated[SignedAccessStoreProtocol, Depends(get_signed_access_store)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> SignedDownloadCapabilityValidationEnvelope:
    """Validate deterministic signed download capability usage for one scoped document."""

    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    validation_request = _parse_signed_download_validation_request(request=request, payload=payload)
    correlation_id = get_correlation_id(request)
    scoped_document = document_registry_store.get_document(document_id)
    if scoped_document is None or scoped_document.tenant_id != resolved_tenant_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=DOCUMENT_NOT_FOUND,
            message="Document was not found.",
            reason="document_not_found_or_forbidden",
            details={"document_id": str(document_id)},
        )
    try:
        return validate_signed_download_capability(
            request_document_id=document_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            actor_tenant_id=resolved_tenant_id,
            capability_token=validation_request.capability_token,
            correlation_id=correlation_id,
            signed_access_store=signed_access_store,
            document_state=scoped_document.state,
        )
    except SignedDownloadAccessError as error:
        status_code, error_code = _map_signed_download_access_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/compliance-overrides",
    response_model=ComplianceOverrideResponseEnvelope,
    status_code=201,
)
def request_document_compliance_override_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> ComplianceOverrideResponseEnvelope:
    """Create deterministic compliance-lock override request for one scoped action."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    override_request = _parse_compliance_override_request_payload(request=request, payload=payload)
    try:
        _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="compliance_override_request",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_compliance_override_audit(
            override_id=_build_override_attempt_id(
                document_id=document_id,
                requested_action=override_request.requested_action,
                correlation_id=correlation_id,
                actor_user_id=principal.user_id,
                scope="request",
            ),
            event_type="request",
            event_status="failure",
            document_id=document_id,
            requested_action=override_request.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=cast(str | None, detail.get("reason")),
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    try:
        return request_compliance_override(
            document_id=document_id,
            requested_action=override_request.requested_action,
            justification=override_request.justification,
            tenant_id=resolved_tenant_id,
            requester_user_id=principal.user_id,
            requester_role=principal.role,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
    except ComplianceOverrideError as error:
        override_id = _build_override_attempt_id(
            document_id=document_id,
            requested_action=override_request.requested_action,
            correlation_id=correlation_id,
            actor_user_id=principal.user_id,
            scope="request",
        )
        _emit_document_compliance_override_audit(
            override_id=override_id,
            event_type="request",
            event_status="failure",
            document_id=document_id,
            requested_action=override_request.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=error.reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_compliance_override_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/compliance-overrides/{override_id}/approve",
    response_model=ComplianceOverrideResponseEnvelope,
)
def approve_document_compliance_override_endpoint(
    document_id: UUID,
    override_id: str,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> ComplianceOverrideResponseEnvelope:
    """Approve deterministic compliance-lock override with independent authorized actor."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    decision_payload = _parse_compliance_override_decision_payload(request=request, payload=payload)
    try:
        _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="compliance_override_approve",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        reason = cast(str | None, detail.get("reason"))
        mapped_reason = "compliance_override_not_authorized"
        if reason == "document_not_found_or_forbidden":
            mapped_reason = reason
        _emit_document_compliance_override_audit(
            override_id=override_id,
            event_type="approve",
            event_status="failure",
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=mapped_reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if mapped_reason == "compliance_override_not_authorized":
            raise _create_document_ai_http_error(
                request=request,
                status_code=403,
                error_code=COMPLIANCE_OVERRIDE_REJECTED,
                message="Compliance override action is not authorized for actor role.",
                reason=mapped_reason,
                details={"override_id": override_id},
            ) from error
        raise
    try:
        return approve_compliance_override(
            override_id=override_id,
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            approver_user_id=principal.user_id,
            approver_role=principal.role,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
    except ComplianceOverrideError as error:
        _emit_document_compliance_override_audit(
            override_id=override_id,
            event_type="approve",
            event_status="failure",
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=error.reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_compliance_override_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/compliance-overrides/{override_id}/reject",
    response_model=ComplianceOverrideResponseEnvelope,
)
def reject_document_compliance_override_endpoint(
    document_id: UUID,
    override_id: str,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> ComplianceOverrideResponseEnvelope:
    """Reject deterministic compliance-lock override with independent authorized actor."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    decision_payload = _parse_compliance_override_decision_payload(request=request, payload=payload)
    try:
        _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="compliance_override_reject",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        reason = cast(str | None, detail.get("reason"))
        mapped_reason = "compliance_override_not_authorized"
        if reason == "document_not_found_or_forbidden":
            mapped_reason = reason
        _emit_document_compliance_override_audit(
            override_id=override_id,
            event_type="reject",
            event_status="failure",
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=mapped_reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if mapped_reason == "compliance_override_not_authorized":
            raise _create_document_ai_http_error(
                request=request,
                status_code=403,
                error_code=COMPLIANCE_OVERRIDE_REJECTED,
                message="Compliance override action is not authorized for actor role.",
                reason=mapped_reason,
                details={"override_id": override_id},
            ) from error
        raise
    try:
        return reject_compliance_override(
            override_id=override_id,
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            approver_user_id=principal.user_id,
            approver_role=principal.role,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
    except ComplianceOverrideError as error:
        _emit_document_compliance_override_audit(
            override_id=override_id,
            event_type="reject",
            event_status="failure",
            document_id=document_id,
            requested_action=decision_payload.requested_action,
            tenant_id=resolved_tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=error.reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_compliance_override_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/trash",
    response_model=DocumentRecordEnvelope,
)
def trash_document_endpoint(
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
    compliance_override_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Apply deterministic trash action for one scoped document."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    try:
        scoped_document = _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="trash",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="trash",
            action_status="failure",
            document_id=document_id,
            previous_state=None,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    previous_state = scoped_document.state
    try:
        override_granted = _consume_compliance_override_for_locked_action(
            request=request,
            principal=principal,
            scoped_document=scoped_document,
            action="trash",
            compliance_override_id=compliance_override_id,
            tenant_id=resolved_tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
        response = apply_document_retention_action(
            action="trash",
            document_record=scoped_document,
            principal_user_id=principal.user_id,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
            compliance_override_granted=override_granted,
        )
        _emit_document_lifecycle_audit(
            action="trash",
            action_status="success",
            document_id=document_id,
            previous_state=previous_state,
            new_state=response.document.state,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return response
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="trash",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    except DocumentRetentionActionError as error:
        _emit_document_lifecycle_audit(
            action="trash",
            action_status="failure",
            document_id=document_id,
            previous_state=cast(str | None, error.details.get("current_state", previous_state)),
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=error.reason,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_retention_action_error(error, action="trash")
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            current_state=cast(str | None, error.details.get("current_state")),
            requested_state=cast(str | None, error.details.get("requested_state")),
        ) from error
    except DocumentAITransactionAmbiguousResultError as error:
        emit_document_structured_log(
            event_name="document_ingestion_upload_completion",
            action="upload_completion_register",
            status="failed",
            trace_id=trace_id,
            correlation_id=correlation_id,
            document_id=document_id,
            reason_code=error.reason_code,
            payload={},
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=503,
            error_code=DOCUMENT_PERSISTENCE_AMBIGUOUS_RESULT,
            message=error.message,
            reason=error.reason_code,
            details=cast(dict[str, object], error.details),
            retryable=False,
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/restore",
    response_model=DocumentRecordEnvelope,
)
def restore_document_endpoint(
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
    compliance_override_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Apply deterministic restore action for one scoped document."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    try:
        scoped_document = _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="restore",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="restore",
            action_status="failure",
            document_id=document_id,
            previous_state=None,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    previous_state = scoped_document.state
    try:
        override_granted = _consume_compliance_override_for_locked_action(
            request=request,
            principal=principal,
            scoped_document=scoped_document,
            action="restore",
            compliance_override_id=compliance_override_id,
            tenant_id=resolved_tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
        response = apply_document_retention_action(
            action="restore",
            document_record=scoped_document,
            principal_user_id=principal.user_id,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
            compliance_override_granted=override_granted,
        )
        _emit_document_lifecycle_audit(
            action="restore",
            action_status="success",
            document_id=document_id,
            previous_state=previous_state,
            new_state=response.document.state,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return response
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="restore",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    except DocumentRetentionActionError as error:
        _emit_document_lifecycle_audit(
            action="restore",
            action_status="failure",
            document_id=document_id,
            previous_state=cast(str | None, error.details.get("current_state", previous_state)),
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=error.reason,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_retention_action_error(error, action="restore")
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
            current_state=cast(str | None, error.details.get("current_state")),
            requested_state=cast(str | None, error.details.get("requested_state")),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/purge-eligibility",
    response_model=DocumentRecordEnvelope,
)
def mark_document_eligible_for_purge_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
    compliance_override_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Mark one scoped document as eligible_for_purge under deterministic timestamp rules."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    try:
        scoped_document = _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="mark_eligible_for_purge",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="mark_eligible_for_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=None,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    previous_state = scoped_document.state
    try:
        retention_request = _parse_mark_eligible_for_purge_request(request=request, payload=payload)
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="mark_eligible_for_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    try:
        override_granted = _consume_compliance_override_for_locked_action(
            request=request,
            principal=principal,
            scoped_document=scoped_document,
            action="mark_eligible_for_purge",
            compliance_override_id=compliance_override_id,
            tenant_id=resolved_tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
        response = apply_document_retention_action(
            action="mark_eligible_for_purge",
            document_record=scoped_document,
            principal_user_id=principal.user_id,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
            compliance_override_granted=override_granted,
            purge_eligible_at=retention_request.purge_eligible_at,
        )
        _emit_document_lifecycle_audit(
            action="mark_eligible_for_purge",
            action_status="success",
            document_id=document_id,
            previous_state=previous_state,
            new_state=response.document.state,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return response
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="mark_eligible_for_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    except DocumentRetentionActionError as error:
        _emit_document_lifecycle_audit(
            action="mark_eligible_for_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=cast(str | None, error.details.get("current_state", previous_state)),
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=error.reason,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_retention_action_error(
            error,
            action="mark_eligible_for_purge",
        )
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
            current_state=cast(str | None, error.details.get("current_state")),
            requested_state=cast(str | None, error.details.get("requested_state")),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/purge",
    response_model=DocumentRecordEnvelope,
)
def execute_document_purge_endpoint(
    document_id: UUID,
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    storage_adapter: Annotated[StorageAdapterProtocol, Depends(get_storage_adapter)],
    compliance_override_store: Annotated[
        ComplianceOverrideStoreProtocol, Depends(get_compliance_override_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
    compliance_override_id: Annotated[str | None, Query()] = None,
) -> DocumentRecordEnvelope:
    """Execute deterministic purge for one eligible scoped document."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    try:
        scoped_document = _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="execute_purge",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=None,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    previous_state = scoped_document.state
    try:
        retention_request = _parse_execute_purge_request(request=request, payload=payload)
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    try:
        override_granted = _consume_compliance_override_for_locked_action(
            request=request,
            principal=principal,
            scoped_document=scoped_document,
            action="execute_purge",
            compliance_override_id=compliance_override_id,
            tenant_id=resolved_tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
        response = execute_document_purge(
            document_record=scoped_document,
            principal_user_id=principal.user_id,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
            storage_adapter=storage_adapter,
            compliance_override_granted=override_granted,
            purged_at=retention_request.purged_at,
        )
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="success",
            document_id=document_id,
            previous_state=previous_state,
            new_state=response.document.state,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return response
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=previous_state,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise
    except DocumentRetentionActionError as error:
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="failure",
            document_id=document_id,
            previous_state=cast(str | None, error.details.get("current_state", previous_state)),
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=error.reason,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_retention_action_error(error, action="execute_purge")
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
            current_state=cast(str | None, error.details.get("current_state")),
            requested_state=cast(str | None, error.details.get("requested_state")),
        ) from error


@ROUTER.post(
    "/v1/documents/{document_id}/purge-dry-run",
    response_model=DocumentPurgeSafetyDryRunEnvelope,
)
def execute_document_purge_dry_run_endpoint(
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_document_ai_principal)],
    document_registry_store: Annotated[
        DocumentRegistryStoreProtocol, Depends(get_document_registry_store)
    ],
    tenant_id: Annotated[str | None, Query()] = None,
) -> DocumentPurgeSafetyDryRunEnvelope:
    """Run deterministic non-destructive purge safety prechecks for one scoped document."""

    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    resolved_tenant_id = _resolve_tenant_scope(request=request, tenant_id=tenant_id)
    try:
        scoped_document = _resolve_document_for_action_access_policy(
            request=request,
            document_id=document_id,
            action="purge_dry_run",
            principal=principal,
            tenant_id=resolved_tenant_id,
            document_registry_store=document_registry_store,
        )
    except HTTPException as error:
        detail = cast(dict[str, object], error.detail)
        _emit_document_lifecycle_audit(
            action="execute_purge",
            action_status="checked",
            document_id=document_id,
            previous_state=None,
            new_state=None,
            tenant_id=resolved_tenant_id,
            user_id=principal.user_id,
            reason_code=cast(str | None, detail.get("reason")),
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise

    dry_run_report = evaluate_document_purge_safety(
        document_record=scoped_document,
        correlation_id=correlation_id,
        trace_id=trace_id,
    )
    _emit_document_lifecycle_audit(
        action="execute_purge",
        action_status="checked",
        document_id=document_id,
        previous_state=scoped_document.state,
        new_state=None,
        tenant_id=resolved_tenant_id,
        user_id=principal.user_id,
        reason_code=dry_run_report.blockers[0] if dry_run_report.blockers else None,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    return dry_run_report


def create_app() -> FastAPI:
    """Build the document_ai FastAPI application."""

    app = FastAPI()
    validate_document_ai_production_configuration()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    persistence_mode = get_document_ai_persistence_mode()
    app.state.document_ai_persistence_mode = persistence_mode
    document_ai_connection_pool = None
    if persistence_mode == "persistent":
        database_url = load_document_ai_database_url()
        if database_url is None or not database_url.strip():
            raise RuntimeError(
                "document_ai persistent mode requires DATABASE_URL or DB credentials; "
                "set DOCUMENT_AI_PERSISTENCE_MODE=in_memory only for explicit non-production use."
            )
        document_ai_connection_pool = get_document_ai_connection_pool(database_url=database_url)
        app.state.document_ai_connection_pool = document_ai_connection_pool
        try:
            persistence_status = resolve_document_ai_persistence_status(
                database_url=database_url,
                required_tables=_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES,
            )
            if persistence_status != "ready":
                raise RuntimeError(
                    "document_ai persistent runtime schema is unavailable or mismatched; "
                    "apply migrations before starting the service."
                )
            app.state.upload_session_store = PersistentUploadSessionStore(database_url=database_url)
            app.state.document_registry_store = PersistentDocumentRegistryStore(
                database_url=database_url
            )
            app.state.document_binding_store = PersistentDocumentBindingStore(
                database_url=database_url
            )
            app.state.exact_retrieval_repository = ExactRetrievalRepository(
                database_url=database_url
            )
            app.state.semantic_retrieval_repository = SemanticRetrievalRepository(
                database_url=database_url
            )
            app.state.hybrid_retrieval_repository = HybridRetrievalRepository(
                database_url=database_url
            )
            app.state.source_artifact_store = PersistentDocumentFoundationStore(
                database_url=database_url
            )
            app.state.signed_access_store = PersistentSignedAccessStore(database_url=database_url)
            app.state.compliance_override_store = PersistentComplianceOverrideStore(
                database_url=database_url
            )
            app.state.processing_outbox_repository = ProcessingOutboxRepository(
                database_url=database_url
            )
            app.state.processing_work_discovery_repository = ProcessingWorkDiscoveryRepository(
                database_url=database_url
            )
            app.state.processing_worker_repository = ProcessingWorkerRepository(
                database_url=database_url
            )
            app.state.processing_state_reconciler = ProcessingStateReconciler(
                database_url=database_url
            )
            if get_document_ai_runtime_mode() == "production":
                app.state.storage_adapter = build_runtime_storage_adapter()
            configure_document_audit_backend(
                PersistentDocumentAuditBackend(database_url=database_url)
            )
        except Exception:
            close_document_ai_connection_pool(connection_pool=document_ai_connection_pool)
            raise
    else:
        configure_document_audit_backend(InMemoryDocumentAuditBackend())
    app.include_router(ROUTER)
    lifecycle_app = cast(_EventHandlerApplicationProtocol, app)
    lifecycle_app.add_event_handler("startup", lambda: _recover_durable_work(app))
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: _reconcile_processing_outbox(app),
        trigger=IntervalTrigger(seconds=30),
        id="document-ai-processing-outbox-reconciliation",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        lambda: _reconcile_processing_state(app),
        trigger=IntervalTrigger(seconds=30),
        id="document-ai-processing-state-reconciliation",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    app.state.processing_outbox_scheduler = scheduler
    lifecycle_app.add_event_handler("startup", scheduler.start)
    lifecycle_app.add_event_handler("shutdown", lambda: scheduler.shutdown(wait=False))
    if persistence_mode == "persistent" and document_ai_connection_pool is not None:
        lifecycle_app.add_event_handler(
            "shutdown",
            lambda pool=document_ai_connection_pool: close_document_ai_connection_pool(
                connection_pool=pool
            ),
        )
    worker_controller = _build_processing_worker_polling_controller(app)
    if worker_controller is not None:
        app.state.processing_worker_polling_controller = worker_controller
        lifecycle_app.add_event_handler("startup", worker_controller.start)
        lifecycle_app.add_event_handler("shutdown", worker_controller.stop)
    return app


def _parse_upload_session_request(
    request: Request,
    payload: object,
) -> UploadSessionCreateRequest:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message="Invalid upload-session request payload.",
            reason="request_body_must_be_object",
            details={},
        )

    try:
        return UploadSessionCreateRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_SESSION_REQUEST,
            message="Invalid upload-session request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False, include_context=False)},
        ) from error


def _parse_upload_completion_request(
    request: Request,
    payload: object,
) -> UploadCompletionRequest:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_COMPLETION_REQUEST,
            message="Invalid upload-completion request payload.",
            reason="request_body_must_be_object",
            details={},
        )

    try:
        return UploadCompletionRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_UPLOAD_COMPLETION_REQUEST,
            message="Invalid upload-completion request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_mark_eligible_for_purge_request(
    request: Request,
    payload: object,
) -> MarkEligibleForPurgeRequest:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETENTION_REQUEST,
            message="Invalid document retention request payload.",
            reason="request_body_must_be_object",
            details={},
        )
    try:
        return MarkEligibleForPurgeRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETENTION_REQUEST,
            message="Invalid document retention request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_execute_purge_request(
    request: Request,
    payload: object,
) -> ExecutePurgeRequest:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETENTION_REQUEST,
            message="Invalid document retention request payload.",
            reason="request_body_must_be_object",
            details={},
        )
    try:
        return ExecutePurgeRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETENTION_REQUEST,
            message="Invalid document retention request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _recover_durable_work(app: FastAPI) -> None:
    """Recover stale worker and outbox claims before accepting traffic."""

    state_reconciler = getattr(app.state, "processing_state_reconciler", None)
    if isinstance(state_reconciler, ProcessingStateReconciler):
        state_reconciler.reconcile_once()
    worker_repository = getattr(app.state, "processing_worker_repository", None)
    if isinstance(worker_repository, ProcessingWorkerRepository):
        worker_repository.recover_expired_leases()
    purge_store = getattr(app.state, "document_registry_store", None)
    purge_storage_adapter = getattr(app.state, "storage_adapter", None)
    if isinstance(purge_store, PersistentDocumentRegistryStore) and purge_storage_adapter is not None:
        recover_pending_document_purges(
            database_url=purge_store.database_url,
            storage_adapter=purge_storage_adapter,
        )
    _reconcile_processing_outbox(app, recover_only=True)


def _reconcile_processing_outbox(app: FastAPI, *, recover_only: bool = False) -> int:
    """Reconcile the one durable outbox when a configured broker publisher is available."""

    repository = getattr(app.state, "processing_outbox_repository", None)
    if not isinstance(repository, ProcessingOutboxRepository):
        return 0
    repository.recover_stale_claims(stale_after=timedelta(minutes=5))
    if recover_only:
        return 0
    publisher = getattr(app.state, "processing_outbox_publisher", None)
    if publisher is None:
        return 0
    return ProcessingOutboxRelay(repository=repository, publisher=publisher).reconcile_once()


def _reconcile_processing_state(app: FastAPI) -> int:
    """Repair bounded durable processing inconsistencies without running work."""

    reconciler = getattr(app.state, "processing_state_reconciler", None)
    if not isinstance(reconciler, ProcessingStateReconciler):
        return 0
    report = reconciler.reconcile_once()
    return report.repaired_total


def _build_processing_worker_polling_controller(
    app: FastAPI,
) -> DocumentAIWorkerPollingController | None:
    """Build the worker polling controller only when a real handoff boundary exists."""

    repository = getattr(app.state, "processing_work_discovery_repository", None)
    handoff = getattr(app.state, "processing_work_candidate_handoff", None)
    if not isinstance(repository, ProcessingWorkDiscoveryRepository):
        return None
    if handoff is None or not isinstance(handoff, ProcessingWorkCandidateHandoffProtocol):
        return None
    loop = BoundedProcessingWorkPollingLoop(
        repository=repository,
        candidate_handoff=handoff,
        policy=ProcessingWorkPollingPolicy(
            batch_size=repository.max_batch_size,
            poll_interval_seconds=float(get_document_ai_worker_poll_interval_seconds()),
            empty_queue_backoff_seconds=float(get_document_ai_worker_empty_queue_backoff_seconds()),
            discovery_failure_backoff_seconds=float(
                get_document_ai_worker_discovery_failure_backoff_seconds()
            ),
        ),
    )
    return DocumentAIWorkerPollingController(loop=loop)


def _parse_signed_download_validation_request(
    request: Request,
    payload: object,
) -> SignedDownloadCapabilityValidationRequest:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOWNLOAD_ACCESS_REQUEST,
            message="Invalid signed download capability request payload.",
            reason="request_body_must_be_object",
            details={},
        )
    try:
        return SignedDownloadCapabilityValidationRequest.model_validate(
            cast(dict[str, object], payload)
        )
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOWNLOAD_ACCESS_REQUEST,
            message="Invalid signed download capability request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _decode_direct_download_capability(
    *,
    request: Request,
    object_key: str,
    capability_token: str,
):
    signing_secret = os.getenv(SIGNED_DOWNLOAD_SECRET_ENV_VAR)
    if signing_secret is None or not signing_secret.strip():
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOWNLOAD_CAPABILITY_REJECTED,
            message="Signed download capability signature is invalid.",
            reason="invalid_capability_signature",
            details={"object_key": object_key},
        )

    try:
        claims = decode_signed_download_capability_token(
            capability_token=capability_token,
            signing_secret=signing_secret,
        )
    except SignedDownloadAccessError as error:
        status_code, error_code = _map_signed_download_access_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=_coerce_error_details(error.details),
        ) from error

    expires_at = datetime.fromisoformat(claims.expires_at.replace("Z", "+00:00"))
    if expires_at <= datetime.now(UTC):
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=DOWNLOAD_CAPABILITY_REJECTED,
            message="Signed download capability has expired.",
            reason="capability_expired",
            details={"object_key": object_key},
        )

    return claims


def _parse_compliance_override_request_payload(
    request: Request,
    payload: object,
) -> ComplianceOverrideRequestPayload:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPLIANCE_OVERRIDE_REQUEST,
            message="Invalid compliance-override request payload.",
            reason="request_body_must_be_object",
            details={},
        )
    try:
        return ComplianceOverrideRequestPayload.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPLIANCE_OVERRIDE_REQUEST,
            message="Invalid compliance-override request payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_compliance_override_decision_payload(
    request: Request,
    payload: object,
) -> ComplianceOverrideDecisionPayload:
    if not isinstance(payload, dict):
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPLIANCE_OVERRIDE_REQUEST,
            message="Invalid compliance-override decision payload.",
            reason="request_body_must_be_object",
            details={},
        )
    try:
        return ComplianceOverrideDecisionPayload.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPLIANCE_OVERRIDE_REQUEST,
            message="Invalid compliance-override decision payload.",
            reason="validation_error",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _resolve_session_record(
    request: Request,
    session_id: UUID,
    upload_session_store: UploadSessionStoreProtocol,
    allow_consumed_session: bool = False,
) -> UploadSessionRecord:
    """Load a usable governed upload session for durable completion."""

    session_record = get_upload_session_record(
        session_id=session_id,
        upload_session_store=upload_session_store,
    )
    if session_record is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=UPLOAD_SESSION_NOT_FOUND,
            message="Upload session was not found.",
            reason="unknown_session",
            details={"session_id": str(session_id)},
        )
    if is_upload_session_expired(session_record):
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=UPLOAD_SESSION_EXPIRED,
            message="Upload session has expired.",
            reason="expired_session",
            details={"session_id": str(session_id)},
        )
    if session_record.session_state == "completed" and not allow_consumed_session:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=UPLOAD_SESSION_INVALID_STATE,
            message="Upload session has already been consumed.",
            reason="session_already_consumed",
            details={"session_id": str(session_id)},
        )
    return session_record


def _enforce_session_guards(
    request: Request,
    document_id: UUID,
    principal: Principal,
    session_record: UploadSessionRecord,
) -> None:
    """Ensure completion cannot cross a document, owner, or tenant boundary."""

    if session_record.document_id != document_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=409,
            error_code=UPLOAD_SESSION_DOCUMENT_MISMATCH,
            message="Upload session document reference does not match requested document.",
            reason="session_document_mismatch",
            details={
                "session_document_id": str(session_record.document_id),
                "request_document_id": str(document_id),
            },
        )
    if session_record.owner_user_id != principal.user_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=UPLOAD_SESSION_FORBIDDEN,
            message="Upload session ownership context does not match authenticated principal.",
            reason="owner_user_mismatch",
            details={
                "owner_user_id": str(session_record.owner_user_id),
                "principal_user_id": str(principal.user_id),
            },
        )


def _create_document_ai_http_error(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    details: dict[str, object],
    current_state: str | None = None,
    requested_state: str | None = None,
    retryable: bool | None = None,
) -> HTTPException:
    correlation_id = get_correlation_id(request)
    trace_id = _build_trace_id_from_correlation(correlation_id)
    redacted_details = cast(dict[str, object], redact_sensitive_fields(details))
    detail: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "details": redacted_details,
    }
    if current_state is not None:
        detail["current_state"] = current_state
    if requested_state is not None:
        detail["requested_state"] = requested_state
    if retryable is not None:
        detail["retryable"] = retryable
    return HTTPException(
        status_code=status_code,
        detail=detail,
    )


def _coerce_error_details(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _resolve_document_for_action_access_policy(
    *,
    request: Request,
    document_id: UUID,
    action: DocumentAccessAction,
    principal: Principal,
    tenant_id: str,
    document_registry_store: DocumentRegistryStoreProtocol,
) -> PersistedDocumentRecord:
    document_record = document_registry_store.get_document(document_id)
    if document_record is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=404,
            error_code=DOCUMENT_NOT_FOUND,
            message="Document was not found.",
            reason="document_not_found_or_forbidden",
            details={"document_id": str(document_id)},
        )

    access_decision = evaluate_document_access_policy(
        actor_user_id=principal.user_id,
        actor_tenant_id=tenant_id,
        actor_role=principal.role,
        document_owner_user_id=document_record.owner_user_id,
        document_tenant_id=document_record.tenant_id,
        action=action,
    )
    if access_decision["decision"] == "allow":
        return document_record

    if access_decision["reason"] == "role_not_permitted_for_action":
        raise _create_document_ai_http_error(
            request=request,
            status_code=403,
            error_code=DOCUMENT_ACCESS_DENIED,
            message="Document action is not permitted for authenticated role.",
            reason=access_decision["reason"],
            details={"document_id": str(document_id), "action": action},
        )

    raise _create_document_ai_http_error(
        request=request,
        status_code=404,
        error_code=DOCUMENT_NOT_FOUND,
        message="Document was not found.",
        reason="document_not_found_or_forbidden",
        details={
            "document_id": str(document_id),
            "action": action,
            "policy_reason": access_decision["reason"],
        },
    )


def _emit_document_lifecycle_audit(
    *,
    action: LifecycleActionName,
    action_status: Literal["success", "failure", "checked"],
    document_id: UUID,
    previous_state: str | None,
    new_state: str | None,
    tenant_id: str,
    user_id: UUID,
    reason_code: str | None,
    trace_id: str,
    correlation_id: str,
) -> None:
    emit_document_lifecycle_audit_evidence(
        action=action,
        action_status=action_status,
        document_id=document_id,
        previous_state=previous_state,
        new_state=new_state,
        tenant_id=tenant_id,
        user_id=user_id,
        reason_code=reason_code,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    emit_document_structured_log(
        event_name=build_document_lifecycle_event_name(action=action, status=action_status),
        action=action,
        status=action_status,
        trace_id=trace_id,
        correlation_id=correlation_id,
        document_id=document_id,
        reason_code=reason_code,
        payload={"previous_state": previous_state, "new_state": new_state, "tenant_id": tenant_id},
    )


def _emit_document_compliance_override_audit(
    *,
    override_id: str,
    event_type: Literal["request", "approve", "reject", "use", "expire"],
    event_status: Literal["success", "failure"],
    document_id: UUID,
    requested_action: ComplianceOverrideAction,
    tenant_id: str,
    actor_user_id: UUID,
    actor_role: str,
    reason_code: str | None,
    state_before: str | None,
    state_after: str | None,
    trace_id: str,
    correlation_id: str,
) -> None:
    emit_document_compliance_override_audit_evidence(
        override_id=override_id,
        event_type=event_type,
        event_status=event_status,
        document_id=document_id,
        requested_action=requested_action,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        reason_code=reason_code,
        state_before=state_before,
        state_after=state_after,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )


def _consume_compliance_override_for_locked_action(
    *,
    request: Request,
    principal: Principal,
    scoped_document: PersistedDocumentRecord,
    action: ComplianceOverrideAction,
    compliance_override_id: str | None,
    tenant_id: str,
    correlation_id: str,
    trace_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
) -> bool:
    lock_active = is_document_compliance_lock_active(
        compliance_lock_until=scoped_document.compliance_lock_until
    )
    if not lock_active:
        return False
    if compliance_override_id is None:
        return False
    try:
        consume_compliance_override_for_action(
            override_id=compliance_override_id,
            document_id=scoped_document.document_id,
            requested_action=action,
            actor_user_id=principal.user_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            compliance_override_store=compliance_override_store,
        )
        return True
    except ComplianceOverrideError as error:
        _emit_document_compliance_override_audit(
            override_id=compliance_override_id,
            event_type="use",
            event_status="failure",
            document_id=scoped_document.document_id,
            requested_action=action,
            tenant_id=tenant_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            reason_code=error.reason,
            state_before=None,
            state_after=None,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        status_code, error_code = _map_compliance_override_error(reason=error.reason)
        raise _create_document_ai_http_error(
            request=request,
            status_code=status_code,
            error_code=error_code,
            message=error.message,
            reason=error.reason,
            details=cast(dict[str, object], error.details),
        ) from error


def _build_override_attempt_id(
    *,
    document_id: UUID,
    requested_action: ComplianceOverrideAction,
    correlation_id: str,
    actor_user_id: UUID,
    scope: str,
) -> str:
    return sha256(
        (
            f"document_ai:compliance_override_attempt:{scope}:{document_id}:{requested_action}:"
            f"{actor_user_id}:{correlation_id}"
        ).encode()
    ).hexdigest()


def _build_trace_id_from_correlation(correlation_id: str) -> str:
    return sha256(correlation_id.encode("utf-8")).hexdigest()


def _build_upload_traceability(*, trace_id: str, correlation_id: str) -> UploadSessionTraceability:
    return UploadSessionTraceability(
        trace_id=trace_id,
        correlation_id=correlation_id,
    )


def _resolve_tenant_scope(
    *,
    request: Request,
    tenant_id: str | None,
) -> str:
    if tenant_id is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Tenant context is required for document retrieval.",
            reason="missing_tenant_id",
            details={},
        )
    normalized_tenant_id = tenant_id.strip()
    if not normalized_tenant_id:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Tenant context is required for document retrieval.",
            reason="invalid_tenant_id",
            details={},
        )
    return normalized_tenant_id


def _build_read_trace_id(
    *,
    correlation_id: str,
    operation: str,
    tenant_id: str,
    principal_user_id: UUID,
    document_id: UUID | None = None,
) -> str:
    trace_basis = f"{correlation_id}:{operation}:{tenant_id}:{principal_user_id}"
    if document_id is not None:
        trace_basis = f"{trace_basis}:{document_id}"
    return sha256(trace_basis.encode("utf-8")).hexdigest()


def _map_retention_action_error(
    error: DocumentRetentionActionError,
    *,
    action: str,
) -> tuple[int, str]:
    if error.reason == "compliance_lock_active":
        return 409, DOCUMENT_RETENTION_LOCK_ACTIVE
    if error.reason in {
        "already_trashed",
        "already_purged",
        "already_eligible_for_purge",
        "invalid_trash_state_transition",
        "invalid_restore_state_transition",
        "invalid_mark_eligible_state_transition",
        "invalid_execute_purge_state_transition",
    }:
        return 409, INVALID_DOCUMENT_STATE_TRANSITION
    if error.reason in {
        "missing_purge_eligible_at",
        "invalid_purge_eligible_at",
        "invalid_purged_at",
        "invalid_uploaded_at",
        "purge_eligible_at_in_future",
        "purge_eligible_at_before_uploaded_at",
        "purge_before_eligibility",
    }:
        return 400, INVALID_DOCUMENT_RETENTION_REQUEST
    if error.reason == "owner_user_mismatch":
        return 403, DOCUMENT_RETENTION_ACTION_FORBIDDEN
    return 400, INVALID_DOCUMENT_RETRIEVAL_REQUEST


def _map_signed_download_access_error(reason: str) -> tuple[int, str]:
    if reason == "unauthorized_download_access":
        return 403, DOWNLOAD_ACCESS_DENIED
    if reason in {
        "capability_expired",
        "capability_already_consumed",
        "capability_scope_mismatch",
        "invalid_capability_signature",
        "document_lifecycle_blocked",
    }:
        return 409, DOWNLOAD_CAPABILITY_REJECTED
    return 400, INVALID_DOWNLOAD_ACCESS_REQUEST


def _map_compliance_override_error(reason: str) -> tuple[int, str]:
    if reason in {
        "compliance_override_not_authorized",
        "compliance_override_self_approval_forbidden",
    }:
        return 403, COMPLIANCE_OVERRIDE_REJECTED
    if reason in {
        "compliance_override_invalid_state",
        "compliance_override_expired",
        "compliance_override_scope_mismatch",
    }:
        return 409, COMPLIANCE_OVERRIDE_REJECTED
    return 400, INVALID_COMPLIANCE_OVERRIDE_REQUEST


def _resolve_document_state_filter(
    *,
    request: Request,
    state: str | None,
) -> DocumentLifecycleState | None:
    if state is None:
        return None
    normalized_state = state.strip()
    if normalized_state not in _ALLOWED_DOCUMENT_STATES:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Document retrieval filter state is invalid.",
            reason="invalid_state_filter",
            details={"state": state},
        )
    return cast(DocumentLifecycleState, normalized_state)


def _resolve_uploaded_datetime_filter(
    *,
    request: Request,
    value: str | None,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Document retrieval uploaded date filter is invalid.",
            reason=f"invalid_{field_name}_filter",
            details={field_name: value},
        )
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Document retrieval uploaded date filter is invalid.",
            reason=f"invalid_{field_name}_filter",
            details={field_name: value},
        ) from error
    if parsed.tzinfo is None:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Document retrieval uploaded date filter is invalid.",
            reason=f"invalid_{field_name}_timezone",
            details={field_name: value},
        )
    return parsed.astimezone(UTC)


def _resolve_computation_id_filter(
    *,
    request: Request,
    computation_id: str | None,
) -> str | None:
    if computation_id is None:
        return None
    normalized = computation_id.strip()
    if not normalized:
        raise _create_document_ai_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_DOCUMENT_RETRIEVAL_REQUEST,
            message="Document retrieval computation linkage filter is invalid.",
            reason="invalid_computation_id_filter",
            details={},
        )
    return normalized


app = create_app()
