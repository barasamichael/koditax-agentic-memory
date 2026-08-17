"""Deterministic upload-session construction and session lookup behavior."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Protocol
from typing import TypedDict
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import psycopg
from pydantic import Field
from pydantic import BaseModel

from shared.determinism.input_hash import canonical_json_dumps
from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.config import MAX_UPLOAD_SIZE_BYTES
from services.document_ai.app.config import is_valid_checksum_sha256
from services.document_ai.app.config import ALLOWED_UPLOAD_MIME_TYPES
from services.document_ai.app.config import UPLOAD_SESSION_TTL_MINUTES
from services.document_ai.app.config import is_allowed_upload_mime_type
from services.document_ai.app.config import is_within_upload_size_limit
from services.document_ai.app.config import ARCHITECTURE_DEFINED_UNSUPPORTED_MIME_TYPES
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction


class UploadSessionCreateRequest(BaseModel):
    """Represent upload-session creation request payload."""

    model_config = {"extra": "forbid"}

    tenant_id: str = Field(min_length=1, max_length=120)
    owner_user_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=127)
    expected_size_bytes: int = Field(ge=1)
    checksum_sha256: str = Field(min_length=1, max_length=128)
    lane_hint: str | None = Field(default=None, min_length=1)


class UploadSessionTraceability(BaseModel):
    """Represent deterministic traceability metadata for one response."""

    trace_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    correlation_id: str


class UploadSessionResponse(BaseModel):
    """Represent upload-session response payload."""

    status: str = "upload_session_created"
    session_id: UUID
    upload_session_id: UUID
    session_state: str
    document_id: UUID
    upload_url: str
    expires_at: str
    traceability: UploadSessionTraceability


class UploadSessionRecord(BaseModel):
    """Represent one registered upload-session record."""

    session_id: UUID
    document_id: UUID
    session_state: str
    created_at: str
    tenant_id: str
    owner_user_id: UUID
    content_type: str
    expected_size_bytes: int
    checksum_sha256: str
    expires_at: str
    original_filename: str | None = None
    storage_provider: str | None = None
    storage_key: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    completed_at: str | None = None


class UploadSessionRequestError(ValueError):
    """Represent deterministic upload-session request validation failure."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details if details is not None else {}


class UploadSessionConflictError(ValueError):
    """Represent deterministic idempotency conflict during upload-session creation."""

    def __init__(self, reason: str, details: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self._details = details

    def details(self) -> dict[str, object]:
        """Return deterministic conflict details payload."""

        return self._details


class UploadSessionIdempotencyRecord(TypedDict):
    """Represent one stored idempotency record for upload-session replay."""

    idempotency_key: str
    request_fingerprint: str
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    session_record: dict[str, object]


class UploadSessionStoreProtocol(Protocol):
    """Define deterministic upload-session idempotency store contract."""

    def get(self, idempotency_key: str) -> UploadSessionIdempotencyRecord | None:
        """Lookup existing record for one idempotency key."""

        ...

    def set(self, record: UploadSessionIdempotencyRecord) -> None:
        """Persist one idempotency record."""

        ...

    def get_session(self, session_id: UUID) -> UploadSessionRecord | None:
        """Lookup session record by session ID."""

        ...

    def set_session(self, session_record: UploadSessionRecord) -> None:
        """Persist session record by session ID."""

        ...

    def clear(self) -> None:
        """Clear all records."""

        ...


class InMemoryUploadSessionStore:
    """Provide deterministic in-memory upload-session storage."""

    def __init__(self) -> None:
        self._records: dict[str, UploadSessionIdempotencyRecord] = {}
        self._sessions: dict[str, UploadSessionRecord] = {}

    def get(self, idempotency_key: str) -> UploadSessionIdempotencyRecord | None:
        return self._records.get(idempotency_key)

    def set(self, record: UploadSessionIdempotencyRecord) -> None:
        self._records[record["idempotency_key"]] = record

    def get_session(self, session_id: UUID) -> UploadSessionRecord | None:
        return self._sessions.get(str(session_id))

    def set_session(self, session_record: UploadSessionRecord) -> None:
        self._sessions[str(session_record.session_id)] = session_record

    def clear(self) -> None:
        self._records.clear()
        self._sessions.clear()


class PersistentUploadSessionStore:
    """Persist deterministic upload-session state to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get(self, idempotency_key: str) -> UploadSessionIdempotencyRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            idempotency_key,
                            request_fingerprint,
                            request_payload,
                            response_payload,
                            session_record
                        FROM document_ai_upload_sessions
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error
        if row is None:
            return None
        return UploadSessionIdempotencyRecord(
            idempotency_key=str(row[0]),
            request_fingerprint=str(row[1]),
            request_payload=dict(row[2]),
            response_payload=dict(row[3]),
            session_record=dict(row[4]),
        )

    def set(self, record: UploadSessionIdempotencyRecord) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.upload_sessions.set",
                transaction_callback=lambda cursor: self._set_transaction(
                    cursor=cursor, record=record
                ),
                reconcile_ambiguous_result=lambda connection: self._reconcile_set_result(
                    connection=connection, record=record
                ),
            )
        except UploadSessionConflictError:
            raise
        except psycopg.Error as error:
            if _is_unique_violation(error):
                if self._resolve_unique_violation(record) is not None:
                    return
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error

    def _set_transaction(self, *, cursor: object, record: UploadSessionIdempotencyRecord) -> str:
        session_record = UploadSessionRecord.model_validate(record["session_record"])
        cursor.execute(
            """
            INSERT INTO document_ai_upload_sessions (
                session_id,
                document_id,
                session_state,
                created_at,
                tenant_id,
                owner_user_id,
                content_type,
                expected_size_bytes,
                checksum_sha256,
                expires_at,
                completed_at,
                idempotency_key,
                request_fingerprint,
                request_payload,
                response_payload,
                session_record
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (session_id) DO NOTHING
            """,
            (
                session_record.session_id,
                session_record.document_id,
                session_record.session_state,
                _parse_iso_datetime(session_record.created_at),
                session_record.tenant_id,
                session_record.owner_user_id,
                session_record.content_type,
                session_record.expected_size_bytes,
                session_record.checksum_sha256,
                _parse_iso_datetime(session_record.expires_at),
                _parse_iso_datetime_or_none(session_record.completed_at),
                record["idempotency_key"],
                record["request_fingerprint"],
                json.dumps(record["request_payload"], sort_keys=True),
                json.dumps(record["response_payload"], sort_keys=True),
                json.dumps(record["session_record"], sort_keys=True),
            ),
        )
        return record["idempotency_key"]

    def _resolve_unique_violation(self, record: UploadSessionIdempotencyRecord) -> str | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT idempotency_key, request_fingerprint
                        FROM document_ai_upload_sessions
                        WHERE idempotency_key = %s
                        """,
                        (record["idempotency_key"],),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error
        if row is None:
            return None
        if str(row[1]) != record["request_fingerprint"]:
            raise UploadSessionConflictError(
                reason="idempotency_key_payload_mismatch",
                details={
                    "idempotency_key": record["idempotency_key"],
                    "conflict_field": "request_fingerprint",
                },
            )
        return str(row[0])

    def _reconcile_set_result(
        self, *, connection: object, record: UploadSessionIdempotencyRecord
    ) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT request_fingerprint, request_payload, response_payload, session_record
                FROM document_ai_upload_sessions
                WHERE idempotency_key = %s
                """,
                (record["idempotency_key"],),
            )
            row = cursor.fetchone()
        if row is None or str(row[0]) != record["request_fingerprint"]:
            return None
        return record["idempotency_key"]

    def get_session(self, session_id: UUID) -> UploadSessionRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT session_record
                        FROM document_ai_upload_sessions
                        WHERE session_id = %s
                        """,
                        (session_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error
        if row is None:
            return None
        return UploadSessionRecord.model_validate(dict(row[0]))

    def set_session(self, session_record: UploadSessionRecord) -> None:
        existing_record = self._get_by_session_id(session_record.session_id)
        if existing_record is None:
            self.set(
                UploadSessionIdempotencyRecord(
                    idempotency_key=f"session:{session_record.session_id}",
                    request_fingerprint=sha256(
                        canonical_json_dumps(session_record.model_dump(mode="json")).encode()
                    ).hexdigest(),
                    request_payload={},
                    response_payload={},
                    session_record=session_record.model_dump(mode="json"),
                )
            )
            return
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.upload_sessions.update_session",
                transaction_callback=lambda cursor: self._update_session_transaction(
                    cursor=cursor, session_record=session_record
                ),
                reconcile_ambiguous_result=lambda connection: self._reconcile_update_session_result(
                    connection=connection, session_record=session_record
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error

    def _update_session_transaction(
        self, *, cursor: object, session_record: UploadSessionRecord
    ) -> str:
        cursor.execute(
            """
            UPDATE document_ai_upload_sessions
            SET session_state = %s,
                created_at = %s,
                tenant_id = %s,
                owner_user_id = %s,
                content_type = %s,
                expected_size_bytes = %s,
                checksum_sha256 = %s,
                expires_at = %s,
                completed_at = %s,
                session_record = %s::jsonb
            WHERE session_id = %s
            """,
            (
                session_record.session_state,
                _parse_iso_datetime(session_record.created_at),
                session_record.tenant_id,
                session_record.owner_user_id,
                session_record.content_type,
                session_record.expected_size_bytes,
                session_record.checksum_sha256,
                _parse_iso_datetime(session_record.expires_at),
                _parse_iso_datetime_or_none(session_record.completed_at),
                json.dumps(session_record.model_dump(mode="json"), sort_keys=True),
                session_record.session_id,
            ),
        )
        return str(session_record.session_id)

    def _reconcile_update_session_result(
        self, *, connection: object, session_record: UploadSessionRecord
    ) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT session_record
                FROM document_ai_upload_sessions
                WHERE session_id = %s
                """,
                (session_record.session_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        current = UploadSessionRecord.model_validate(dict(row[0]))
        if current.model_dump(mode="json") != session_record.model_dump(mode="json"):
            return None
        return str(session_record.session_id)

    def clear(self) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.upload_sessions.clear",
                transaction_callback=lambda cursor: self._clear_transaction(cursor=cursor),
                reconcile_ambiguous_result=lambda connection: self._reconcile_clear_result(
                    connection=connection
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error

    def _clear_transaction(self, *, cursor: object) -> bool:
        cursor.execute("DELETE FROM document_ai_upload_sessions")
        return True

    def _reconcile_clear_result(self, *, connection: object) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM document_ai_upload_sessions LIMIT 1")
            row = cursor.fetchone()
        return True if row is None else None

    def _get_by_session_id(self, session_id: UUID) -> UploadSessionIdempotencyRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            idempotency_key,
                            request_fingerprint,
                            request_payload,
                            response_payload,
                            session_record
                        FROM document_ai_upload_sessions
                        WHERE session_id = %s
                        """,
                        (session_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_upload_session_persistence_unavailable") from error
        if row is None:
            return None
        return UploadSessionIdempotencyRecord(
            idempotency_key=str(row[0]),
            request_fingerprint=str(row[1]),
            request_payload=dict(row[2]),
            response_payload=dict(row[3]),
            session_record=dict(row[4]),
        )


_DEFAULT_UPLOAD_SESSION_STORE = InMemoryUploadSessionStore()


def get_default_upload_session_store() -> InMemoryUploadSessionStore:
    """Return default upload-session idempotency store."""

    return _DEFAULT_UPLOAD_SESSION_STORE


def reset_default_upload_session_store() -> None:
    """Reset default upload-session idempotency store for tests."""

    _DEFAULT_UPLOAD_SESSION_STORE.clear()


def build_upload_session(
    upload_session_request: UploadSessionCreateRequest,
    principal_user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    upload_session_store: UploadSessionStoreProtocol,
    storage_adapter: StorageAdapterProtocol,
) -> UploadSessionResponse:
    """Build deterministic upload-session response with idempotency replay support."""

    _validate_upload_session_request_policy(upload_session_request)
    _validate_upload_filename(upload_session_request.file_name)
    request_fingerprint = _build_upload_session_request_fingerprint(
        upload_session_request=upload_session_request,
        principal_user_id=principal_user_id,
    )
    existing_record = upload_session_store.get(idempotency_key)
    if existing_record is not None:
        if existing_record["request_fingerprint"] != request_fingerprint:
            raise UploadSessionConflictError(
                reason="idempotency_key_payload_mismatch",
                details={
                    "idempotency_key": idempotency_key,
                    "conflict_field": "request_fingerprint",
                },
            )
        replayed_session = UploadSessionRecord.model_validate(existing_record["session_record"])
        if is_upload_session_expired(replayed_session):
            raise UploadSessionRequestError(
                reason="expired_session",
                message="Upload session has expired.",
                details={"session_id": str(replayed_session.session_id)},
            )

        response = UploadSessionResponse.model_validate(existing_record["response_payload"])
        return response

    session_id = uuid5(NAMESPACE_URL, f"document_ai:upload_session:{request_fingerprint}")
    document_id = uuid5(NAMESPACE_URL, f"document_ai:document:{request_fingerprint}")
    expires_at = _utc_now_plus_ttl_iso()
    upload_capability = storage_adapter.create_upload_capability(
        tenant_id=upload_session_request.tenant_id,
        owner_user_id=upload_session_request.owner_user_id,
        document_id=document_id,
        session_id=session_id,
        expires_at=expires_at,
    )
    trace_id = sha256(f"{correlation_id}:{request_fingerprint}".encode()).hexdigest()

    response_payload = UploadSessionResponse(
        status="upload_session_created",
        session_id=session_id,
        upload_session_id=session_id,
        session_state="active",
        document_id=document_id,
        upload_url=upload_capability.upload_url,
        expires_at=upload_capability.expires_at,
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    ).model_dump(mode="json")
    session_record = UploadSessionRecord(
        session_id=session_id,
        document_id=document_id,
        session_state="active",
        created_at=_utc_now_iso(),
        tenant_id=upload_session_request.tenant_id,
        owner_user_id=upload_session_request.owner_user_id,
        content_type=upload_session_request.content_type,
        expected_size_bytes=upload_session_request.expected_size_bytes,
        checksum_sha256=upload_session_request.checksum_sha256,
        expires_at=expires_at,
        original_filename=upload_session_request.file_name,
        storage_provider=upload_capability.storage_provider,
        storage_key=upload_capability.object_key,
        upload_headers=upload_capability.headers,
        completed_at=None,
    )
    upload_session_store.set(
        UploadSessionIdempotencyRecord(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            request_payload=upload_session_request.model_dump(mode="json"),
            response_payload=response_payload,
            session_record=session_record.model_dump(mode="json"),
        )
    )
    persisted_record = upload_session_store.get(idempotency_key)
    if persisted_record is not None:
        return UploadSessionResponse.model_validate(persisted_record["response_payload"])
    return UploadSessionResponse.model_validate(response_payload)


def get_upload_session_record(
    session_id: UUID,
    upload_session_store: UploadSessionStoreProtocol,
) -> UploadSessionRecord | None:
    """Lookup upload session by session ID."""

    return upload_session_store.get_session(session_id)


def mark_upload_session_completed(
    session_id: UUID,
    upload_session_store: UploadSessionStoreProtocol,
) -> UploadSessionRecord | None:
    """Transition upload session to completed state deterministically."""

    existing = upload_session_store.get_session(session_id)
    if existing is None:
        return None
    if existing.session_state == "completed":
        return existing
    if existing.session_state != "active":
        return existing
    updated = existing.model_copy(
        update={"session_state": "completed", "completed_at": _utc_now_iso()}
    )
    upload_session_store.set_session(updated)
    return updated


def is_upload_session_expired(
    session_record: UploadSessionRecord,
    now_utc: datetime | None = None,
) -> bool:
    """Return whether upload session has expired."""

    reference_now = datetime.now(UTC) if now_utc is None else now_utc
    expiry_time = datetime.fromisoformat(session_record.expires_at.replace("Z", "+00:00"))
    return expiry_time <= reference_now


def _build_upload_session_request_fingerprint(
    upload_session_request: UploadSessionCreateRequest,
    principal_user_id: UUID,
) -> str:
    envelope = {
        "principal_user_id": str(principal_user_id),
        "upload_session_request": upload_session_request.model_dump(mode="json"),
    }
    return compute_canonical_hash(envelope).sha256_hex


def _utc_now_plus_ttl_iso() -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=UPLOAD_SESSION_TTL_MINUTES))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _parse_iso_datetime_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_iso_datetime(value)


def canonical_upload_session_json(response: UploadSessionResponse) -> str:
    """Serialize upload-session response into canonical deterministic JSON."""

    return canonical_json_dumps(response.model_dump(mode="json"))


def _validate_upload_session_request_policy(
    upload_session_request: UploadSessionCreateRequest,
) -> None:
    if not is_allowed_upload_mime_type(upload_session_request.content_type):
        # Distinguish architecture-defined formats (explicitly not yet supported
        # in production) from completely unknown types so operators can tell
        # the difference between "not yet governed" and "unrecognized input".
        if upload_session_request.content_type in ARCHITECTURE_DEFINED_UNSUPPORTED_MIME_TYPES:
            reason = "format_not_supported_in_production"
        else:
            reason = "unsupported_mime_type"
        raise UploadSessionRequestError(
            reason=reason,
            message="Upload session content type is not supported for production ingestion.",
            details={
                "content_type": upload_session_request.content_type,
                "allowed_mime_types": list(ALLOWED_UPLOAD_MIME_TYPES),
            },
        )
    if not is_within_upload_size_limit(upload_session_request.expected_size_bytes):
        raise UploadSessionRequestError(
            reason="upload_size_exceeds_limit",
            message="Upload session expected size exceeds allowed limit.",
            details={
                "expected_size_bytes": upload_session_request.expected_size_bytes,
                "max_size_bytes": MAX_UPLOAD_SIZE_BYTES,
            },
        )
    if not is_valid_checksum_sha256(upload_session_request.checksum_sha256):
        raise UploadSessionRequestError(
            reason="invalid_checksum_format",
            message="Upload session checksum must be lowercase SHA-256 hex.",
            details={"checksum_sha256": upload_session_request.checksum_sha256},
        )


def _validate_upload_filename(file_name: str) -> None:
    normalized = file_name.strip()
    if not normalized:
        raise UploadSessionRequestError(
            reason="invalid_file_name",
            message="Upload session filename must not be blank.",
            details={"file_name": file_name},
        )
    if any(separator in file_name for separator in ("/", "\\", "\x00")):
        raise UploadSessionRequestError(
            reason="invalid_file_name",
            message="Upload session filename must not contain path separators.",
            details={"file_name": file_name},
        )
    if file_name in {".", ".."}:
        raise UploadSessionRequestError(
            reason="invalid_file_name",
            message="Upload session filename must be a safe original filename.",
            details={"file_name": file_name},
        )


def _is_unique_violation(error: psycopg.Error) -> bool:
    return getattr(error, "sqlstate", None) == "23505"
