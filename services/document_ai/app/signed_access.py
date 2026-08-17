"""Deterministic signed access controls for document download capabilities."""

from __future__ import annotations

import os
import hmac
import json
from uuid import UUID
import base64
from typing import Literal
from typing import Protocol
import hashlib
import binascii
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import psycopg
from pydantic import Field
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from shared.determinism.input_hash import canonical_json_dumps
from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.upload_sessions import UploadSessionTraceability
from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.document_access_policy import evaluate_document_access_policy

SIGNED_DOWNLOAD_ACCESS_TTL_MINUTES = 15
SIGNED_DOWNLOAD_SECRET_ENV_VAR = "DOCUMENT_AI_SIGNED_DOWNLOAD_SECRET"


class SignedDownloadCapabilityClaims(BaseModel):
    """Represent signed download capability claims payload."""

    capability_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: UUID
    issued_to_user_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120)
    expires_at: str
    allowed_action: Literal["download"] = "download"


class SignedDownloadCapability(BaseModel):
    """Represent deterministic signed download capability response payload."""

    capability_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: UUID
    issued_to_user_id: UUID
    tenant_id: str
    expires_at: str
    allowed_action: Literal["download"] = "download"
    capability_token: str
    download_url: str
    method: Literal["GET"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)


class SignedDownloadCapabilityEnvelope(BaseModel):
    """Represent signed download capability issuance envelope."""

    status: Literal["download_capability_issued"] = "download_capability_issued"
    capability: SignedDownloadCapability
    traceability: UploadSessionTraceability


class SignedDownloadCapabilityValidationRequest(BaseModel):
    """Represent signed download capability validation request payload."""

    capability_token: str = Field(min_length=1, max_length=8192)


class SignedDownloadCapabilityValidationEnvelope(BaseModel):
    """Represent signed download capability validation success payload."""

    status: Literal["download_capability_valid"] = "download_capability_valid"
    capability_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: UUID
    expires_at: str
    allowed_action: Literal["download"] = "download"
    validated_at: str
    traceability: UploadSessionTraceability


class SignedDownloadAccessError(ValueError):
    """Represent deterministic signed-access validation failure."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = redact_sensitive_fields(details if details is not None else {})


class SignedAccessStoreProtocol(Protocol):
    """Define deterministic storage for used download capabilities."""

    def is_consumed(self, capability_id: str) -> bool:
        """Return whether capability token was already consumed."""

        ...

    def mark_consumed(self, capability_id: str) -> None:
        """Mark one capability token as consumed."""

        ...

    def clear(self) -> None:
        """Clear all in-memory capability usage records."""

        ...


class InMemorySignedAccessStore:
    """Provide deterministic in-memory signed capability usage storage."""

    def __init__(self) -> None:
        self._consumed_capability_ids: set[str] = set()

    def is_consumed(self, capability_id: str) -> bool:
        return capability_id in self._consumed_capability_ids

    def mark_consumed(self, capability_id: str) -> None:
        self._consumed_capability_ids.add(capability_id)

    def clear(self) -> None:
        self._consumed_capability_ids.clear()


class PersistentSignedAccessStore:
    """Persist signed capability consumption to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def is_consumed(self, capability_id: str) -> bool:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1
                        FROM document_ai_signed_access_usage
                        WHERE capability_id = %s
                        """,
                        (capability_id,),
                    )
                    return cursor.fetchone() is not None
        except psycopg.Error as error:
            raise RuntimeError("document_ai_signed_access_persistence_unavailable") from error

    def mark_consumed(self, capability_id: str) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.signed_access.mark_consumed",
                transaction_callback=lambda cursor: self._mark_consumed_transaction(
                    cursor=cursor, capability_id=capability_id
                ),
                reconcile_ambiguous_result=lambda connection: self._reconcile_mark_consumed_result(
                    connection=connection, capability_id=capability_id
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_signed_access_persistence_unavailable") from error

    def _mark_consumed_transaction(self, *, cursor: object, capability_id: str) -> str:
        cursor.execute(
            """
            INSERT INTO document_ai_signed_access_usage (capability_id)
            VALUES (%s)
            ON CONFLICT (capability_id) DO NOTHING
            """,
            (capability_id,),
        )
        return capability_id

    def _reconcile_mark_consumed_result(
        self, *, connection: object, capability_id: str
    ) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM document_ai_signed_access_usage
                WHERE capability_id = %s
                """,
                (capability_id,),
            )
            row = cursor.fetchone()
        return None if row is None else capability_id

    def clear(self) -> None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM document_ai_signed_access_usage")
                    connection.commit()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_signed_access_persistence_unavailable") from error


_DEFAULT_SIGNED_ACCESS_STORE = InMemorySignedAccessStore()


def get_default_signed_access_store() -> InMemorySignedAccessStore:
    """Return default signed access store."""

    return _DEFAULT_SIGNED_ACCESS_STORE


def reset_default_signed_access_store() -> None:
    """Reset default signed access store for deterministic tests."""

    _DEFAULT_SIGNED_ACCESS_STORE.clear()


def issue_signed_download_capability(
    *,
    document_record: PersistedDocumentRecord,
    issued_to_user_id: UUID,
    tenant_id: str,
    correlation_id: str,
    storage_adapter: StorageAdapterProtocol,
    document_state: str | None = None,
    now_utc: datetime | None = None,
) -> SignedDownloadCapabilityEnvelope:
    """Issue deterministic short-lived signed download capability."""

    if tenant_id != document_record.tenant_id:
        raise SignedDownloadAccessError(
            reason="capability_scope_mismatch",
            message="Download capability scope is invalid.",
            details={
                "tenant_id": tenant_id,
                "document_tenant_id": document_record.tenant_id,
            },
        )

    if document_state is not None and document_state != "active":
        raise SignedDownloadAccessError(
            reason="document_lifecycle_blocked",
            message="Signed download capability is not available for this document state.",
            details={
                "document_id": str(document_record.document_id),
                "document_state": document_state,
            },
        )

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    expires_at = (
        (
            reference_now.replace(microsecond=0)
            + timedelta(minutes=SIGNED_DOWNLOAD_ACCESS_TTL_MINUTES)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    capability_id = hashlib.sha256(
        f"{document_record.document_id}:{issued_to_user_id}:{tenant_id}:{expires_at}:download".encode()
    ).hexdigest()
    claims = SignedDownloadCapabilityClaims(
        capability_id=capability_id,
        document_id=document_record.document_id,
        issued_to_user_id=issued_to_user_id,
        tenant_id=tenant_id,
        expires_at=expires_at,
        allowed_action="download",
    )
    signing_secret = _resolve_signing_secret()
    capability_token = encode_signed_download_capability_token(
        claims=claims,
        signing_secret=signing_secret,
    )
    storage_capability = storage_adapter.create_download_capability(
        tenant_id=tenant_id,
        owner_user_id=document_record.owner_user_id,
        document_id=document_record.document_id,
        capability_id=capability_id,
        expires_at=expires_at,
        signed_token=capability_token,
    )
    trace_id = hashlib.sha256(f"{correlation_id}:{capability_id}".encode()).hexdigest()
    return SignedDownloadCapabilityEnvelope(
        status="download_capability_issued",
        capability=SignedDownloadCapability(
            capability_id=capability_id,
            document_id=document_record.document_id,
            issued_to_user_id=issued_to_user_id,
            tenant_id=tenant_id,
            expires_at=expires_at,
            allowed_action="download",
            capability_token=capability_token,
            download_url=storage_capability.download_url,
            method=storage_capability.method,
            headers=storage_capability.headers,
        ),
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    )


def validate_signed_download_capability(
    *,
    request_document_id: UUID,
    actor_user_id: UUID,
    actor_role: str,
    actor_tenant_id: str,
    capability_token: str,
    correlation_id: str,
    signed_access_store: SignedAccessStoreProtocol,
    document_state: str | None = None,
    now_utc: datetime | None = None,
) -> SignedDownloadCapabilityValidationEnvelope:
    """Validate deterministic signed download capability and enforce usage safety."""

    signing_secret = _resolve_signing_secret()
    claims = decode_signed_download_capability_token(
        capability_token=capability_token,
        signing_secret=signing_secret,
    )
    access_decision = evaluate_document_access_policy(
        actor_user_id=actor_user_id,
        actor_tenant_id=actor_tenant_id,
        actor_role=actor_role,
        document_owner_user_id=claims.issued_to_user_id,
        document_tenant_id=claims.tenant_id,
        action="download_document",
    )
    if access_decision["decision"] != "allow":
        raise SignedDownloadAccessError(
            reason="unauthorized_download_access",
            message="Caller is not authorized for signed document download access.",
            details={
                "policy_reason": access_decision["reason"],
                "actor_user_id": str(actor_user_id),
                "issued_to_user_id": str(claims.issued_to_user_id),
            },
        )

    if claims.document_id != request_document_id or claims.tenant_id != actor_tenant_id:
        raise SignedDownloadAccessError(
            reason="capability_scope_mismatch",
            message="Download capability scope is invalid.",
            details={
                "request_document_id": str(request_document_id),
                "capability_document_id": str(claims.document_id),
                "actor_tenant_id": actor_tenant_id,
                "capability_tenant_id": claims.tenant_id,
            },
        )

    if document_state is not None and document_state != "active":
        raise SignedDownloadAccessError(
            reason="document_lifecycle_blocked",
            message="Signed download capability is not available for this document state.",
            details={
                "document_id": str(request_document_id),
                "document_state": document_state,
            },
        )

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    expires_at = _parse_utc_datetime(claims.expires_at, field_name="expires_at")
    if expires_at < reference_now:
        raise SignedDownloadAccessError(
            reason="capability_expired",
            message="Signed download capability has expired.",
            details={
                "capability_id": claims.capability_id,
                "expires_at": claims.expires_at,
            },
        )

    if signed_access_store.is_consumed(claims.capability_id):
        raise SignedDownloadAccessError(
            reason="capability_already_consumed",
            message="Signed download capability has already been used.",
            details={"capability_id": claims.capability_id},
        )
    signed_access_store.mark_consumed(claims.capability_id)

    validated_at = reference_now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    trace_id = hashlib.sha256(
        f"{correlation_id}:{claims.capability_id}:{request_document_id}".encode()
    ).hexdigest()
    return SignedDownloadCapabilityValidationEnvelope(
        status="download_capability_valid",
        capability_id=claims.capability_id,
        document_id=request_document_id,
        expires_at=claims.expires_at,
        allowed_action="download",
        validated_at=validated_at,
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    )


def encode_signed_download_capability_token(
    *,
    claims: SignedDownloadCapabilityClaims,
    signing_secret: str,
) -> str:
    """Encode deterministic signed download capability token."""

    payload_json = canonical_json_dumps(claims.model_dump(mode="json"))
    payload_bytes = payload_json.encode("utf-8")
    payload_segment = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
    signature_segment = _compute_signature(
        payload_bytes=payload_bytes,
        signing_secret=signing_secret,
    )
    return f"{payload_segment}.{signature_segment}"


def decode_signed_download_capability_token(
    *,
    capability_token: str,
    signing_secret: str,
) -> SignedDownloadCapabilityClaims:
    """Decode and verify deterministic signed download capability token."""

    payload_segment, signature_segment = _split_capability_token(capability_token=capability_token)
    payload_bytes = _decode_payload_segment(payload_segment=payload_segment)
    expected_signature = _compute_signature(
        payload_bytes=payload_bytes,
        signing_secret=signing_secret,
    )
    if not hmac.compare_digest(expected_signature, signature_segment):
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"capability_token": capability_token},
        )
    try:
        payload_object = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"capability_token": capability_token},
        ) from error
    try:
        return SignedDownloadCapabilityClaims.model_validate(payload_object)
    except PydanticValidationError as error:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={
                "capability_token": capability_token,
                "validation_errors": error.errors(include_url=False),
            },
        ) from error


def _resolve_signing_secret() -> str:
    secret = os.getenv(SIGNED_DOWNLOAD_SECRET_ENV_VAR)
    if secret is None or not secret.strip():
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"secret_env_var": SIGNED_DOWNLOAD_SECRET_ENV_VAR},
        )
    return secret


def _split_capability_token(capability_token: str) -> tuple[str, str]:
    token = capability_token.strip()
    if not token:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"capability_token": capability_token},
        )
    segments = token.split(".")
    if len(segments) != 2 or not segments[0] or not segments[1]:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"capability_token": capability_token},
        )
    return segments[0], segments[1]


def _decode_payload_segment(payload_segment: str) -> bytes:
    padding = "=" * ((4 - (len(payload_segment) % 4)) % 4)
    try:
        return base64.urlsafe_b64decode(f"{payload_segment}{padding}")
    except (ValueError, binascii.Error) as error:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={"payload_segment": payload_segment},
        ) from error


def _compute_signature(*, payload_bytes: bytes, signing_secret: str) -> str:
    return hmac.new(signing_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def _parse_utc_datetime(value: str, *, field_name: str) -> datetime:
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={field_name: value},
        ) from error
    if parsed.tzinfo is None:
        raise SignedDownloadAccessError(
            reason="invalid_capability_signature",
            message="Signed download capability signature is invalid.",
            details={field_name: value},
        )
    return parsed.astimezone(UTC)
