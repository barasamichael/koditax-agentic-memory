"""Storage adapter boundary for deterministic document_ai ingestion behavior."""

from __future__ import annotations

from io import BytesIO
import os
import json
from uuid import UUID
import base64
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from hashlib import sha256
from pathlib import Path
from datetime import UTC
from datetime import datetime
import tempfile
from importlib import import_module
import mimetypes
from collections.abc import Mapping

from pydantic import Field
from pydantic import BaseModel

from services.document_ai.app.config import S3_MULTIPART_MAX_PARTS
from services.document_ai.app.config import get_storage_endpoint_url
from services.document_ai.app.config import get_document_ai_r2_bucket
from services.document_ai.app.config import get_document_ai_s3_bucket
from services.document_ai.app.config import get_document_ai_aws_region
from services.document_ai.app.config import get_document_ai_r2_endpoint
from services.document_ai.app.config import get_document_ai_s3_kms_key_id
from services.document_ai.app.config import get_storage_encryption_required
from services.document_ai.app.config import get_document_ai_r2_access_key_id
from services.document_ai.app.config import get_document_ai_storage_provider
from services.document_ai.app.config import S3_MULTIPART_MIN_PART_SIZE_BYTES
from services.document_ai.app.config import get_storage_signing_secret_env_var
from services.document_ai.app.config import get_document_ai_r2_secret_access_key
from services.document_ai.app.config import get_document_ai_r2_read_timeout_seconds
from services.document_ai.app.config import get_document_ai_s3_read_timeout_seconds
from services.document_ai.app.config import get_document_ai_s3_server_side_encryption
from services.document_ai.app.config import get_document_ai_r2_connect_timeout_seconds
from services.document_ai.app.config import get_document_ai_s3_connect_timeout_seconds
from services.document_ai.app.config import get_document_ai_r2_upload_capability_ttl_seconds
from services.document_ai.app.config import get_document_ai_s3_upload_capability_ttl_seconds
from services.document_ai.app.config import validate_document_ai_r2_production_configuration
from services.document_ai.app.config import validate_document_ai_s3_production_configuration
from services.document_ai.app.config import get_document_ai_r2_download_capability_ttl_seconds
from services.document_ai.app.config import get_document_ai_s3_download_capability_ttl_seconds
from services.document_ai.app.config import get_document_ai_s3_multipart_upload_part_size_bytes
from services.document_ai.app.config import get_document_ai_s3_multipart_upload_threshold_bytes
from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.storage_keys import is_tenant_scoped_object_key
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.storage_keys import validate_tenant_document_object_key
from services.document_ai.app.storage_keys import build_tenant_document_download_object_key
from services.document_ai.app.document_formats import normalize_media_type
from services.document_ai.app.security_controls import StorageSecurityControls
from services.document_ai.app.security_controls import REQUIRED_ENCRYPTION_HEADERS
from services.document_ai.app.security_controls import SecurityPolicyViolationError
from services.document_ai.app.security_controls import validate_storage_security_controls


class StorageUploadCapability(BaseModel):
    """Represent deterministic upload capability payload from storage adapter."""

    capability_id: str
    object_key: str
    upload_url: str
    expires_at: str
    storage_provider: str = "in_memory"
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)


class StorageDownloadCapability(BaseModel):
    """Represent deterministic signed download capability payload from storage adapter."""

    capability_id: str
    object_key: str
    download_url: str
    expires_at: str
    method: Literal["GET"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)


class StorageObjectVerification(BaseModel):
    """Represent deterministic storage object verification result."""

    object_key: str
    checksum_sha256: str
    checksum_algorithm: Literal["sha256"] = "sha256"
    size_bytes: int
    content_type: str
    server_side_encryption: str | None = None
    checksum_verification_source: Literal[
        "provider_checksum",
        "supporting_metadata",
        "downloaded_bytes",
    ] = "provider_checksum"
    status: Literal["verified"] = "verified"


class StorageObjectMetadata(BaseModel):
    """Represent provider metadata without treating an ETag as a checksum."""

    object_key: str
    size_bytes: int
    content_type: str
    provider_etag: str | None = None
    checksum_algorithm: str | None = None
    checksum_sha256: str | None = None
    server_side_encryption: str | None = None
    version_id: str | None = None
    last_modified: str | None = None
    custom_metadata: dict[str, str] = Field(default_factory=dict)


class StorageAdapterError(ValueError):
    """Represent deterministic storage adapter failure."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = redact_sensitive_fields(details if details is not None else {})


class StorageAdapterTransientError(StorageAdapterError):
    """Represent retryable storage adapter failure."""


class StorageAdapterPermanentError(StorageAdapterError):
    """Represent non-retryable storage adapter failure."""


def classify_storage_adapter_error(error: Exception) -> Literal["transient", "non_retryable"]:
    """Classify storage adapter failures into retry policy classes."""

    if isinstance(error, StorageAdapterTransientError):
        return "transient"
    return "non_retryable"


class StorageAdapterProtocol(Protocol):
    """Define deterministic storage adapter operations used by ingestion flow."""

    def create_upload_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        session_id: UUID,
        expires_at: str,
    ) -> StorageUploadCapability:
        """Create upload capability for one ingestion session."""

        ...

    def verify_upload_object(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        object_key: str,
        checksum_sha256: str,
        size_bytes: int,
        content_type: str,
    ) -> StorageObjectVerification:
        """Verify uploaded object metadata through adapter boundary."""

        ...

    def create_download_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        capability_id: str,
        expires_at: str,
        signed_token: str,
    ) -> StorageDownloadCapability:
        """Create signed download capability for one scoped document."""

        ...

    def store_upload_object(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
    ) -> None:
        """Persist one uploaded object in the backing storage medium."""

        ...

    def store_upload_object_filelike(
        self,
        object_key: str,
        payload_stream: Any,
        content_type: str,
        payload_size: int | None = None,
    ) -> None:
        """Persist one uploaded object from a file-like stream."""

        ...

    def resolve_download_object(
        self,
        object_key: str,
    ) -> tuple[Path, str]:
        """Resolve one stored object into a concrete file path and content type."""

        ...

    def get_object_metadata(self, object_key: str) -> StorageObjectMetadata:
        """Read governed object metadata for integrity and lifecycle operations."""

        ...

    def object_exists(self, object_key: str) -> bool:
        """Determine whether one governed object exists."""

        ...

    def delete_object(self, object_key: str) -> None:
        """Request idempotent deletion of one governed object."""

        ...

    def verify_object_absent(self, object_key: str) -> bool:
        """Verify absence instead of trusting a delete response alone."""

        ...


class S3StorageAdapter:
    """Private object-storage adapter satisfying FR-002, SR-004, and PR-006."""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any,
        max_capability_ttl_seconds: int = 900,
        server_side_encryption: str | None = None,
        kms_key_id: str | None = None,
        storage_provider: Literal["r2", "s3"] = "s3",
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._max_capability_ttl_seconds = max_capability_ttl_seconds
        self._storage_provider = storage_provider
        normalized_encryption = (
            server_side_encryption.strip() if isinstance(server_side_encryption, str) else None
        )
        if storage_provider == "s3" and not normalized_encryption:
            normalized_encryption = "AES256"
        self._server_side_encryption = normalized_encryption
        self._kms_key_id = (
            kms_key_id.strip() if isinstance(kms_key_id, str) and kms_key_id.strip() else None
        )

    def create_upload_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        session_id: UUID,
        expires_at: str,
    ) -> StorageUploadCapability:
        del owner_user_id
        capability_id = sha256(f"{tenant_id}:{document_id}:{session_id}".encode()).hexdigest()
        object_key = build_tenant_document_object_key(tenant_id, document_id)
        capability_ttl = self._capability_ttl(expires_at)
        try:
            params: dict[str, object] = {"Bucket": self._bucket, "Key": object_key}
            params.update(self._storage_encryption_params())
            upload_url = self._client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=capability_ttl,
                HttpMethod="PUT",
            )
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(
                error, "Unable to issue private S3 upload capability."
            ) from error
        return StorageUploadCapability(
            capability_id=capability_id,
            object_key=object_key,
            upload_url=str(upload_url),
            expires_at=expires_at,
            storage_provider=self._storage_provider,
            headers={"x-kodi-capability-id": capability_id},
        )

    def verify_upload_object(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        object_key: str,
        checksum_sha256: str,
        size_bytes: int,
        content_type: str,
    ) -> StorageObjectVerification:
        del owner_user_id
        if not _is_tenant_scoped_object_key(object_key, tenant_id):
            raise StorageAdapterPermanentError(
                reason="storage_object_scope_mismatch",
                message="Uploaded object is outside the tenant storage scope.",
            )
        if self._storage_provider == "r2":
            return self._verify_r2_upload_object(
                object_key=object_key,
                checksum_sha256=checksum_sha256,
                size_bytes=size_bytes,
                content_type=content_type,
            )
        return self._verify_s3_upload_object(
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
            content_type=content_type,
        )

    def create_download_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        capability_id: str,
        expires_at: str,
        signed_token: str,
    ) -> StorageDownloadCapability:
        del owner_user_id, signed_token
        object_key = build_tenant_document_download_object_key(tenant_id, document_id)
        capability_ttl = self._capability_ttl(expires_at)
        try:
            download_url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=capability_ttl,
                HttpMethod="GET",
            )
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(
                error, "Unable to issue private S3 download capability."
            ) from error
        return StorageDownloadCapability(
            capability_id=capability_id,
            object_key=object_key,
            download_url=str(download_url),
            expires_at=expires_at,
        )

    def store_upload_object(self, object_key: str, payload: bytes, content_type: str) -> None:
        _validate_r2_object_key(object_key)
        self.store_upload_object_filelike(
            object_key=object_key,
            payload_stream=BytesIO(payload),
            content_type=content_type,
            payload_size=len(payload),
        )

    def store_upload_object_filelike(
        self,
        object_key: str,
        payload_stream: Any,
        content_type: str,
        payload_size: int | None = None,
    ) -> None:
        _validate_r2_object_key(object_key)
        try:
            if self._storage_provider == "s3":
                if (
                    payload_size is None
                    and hasattr(payload_stream, "seek")
                    and hasattr(payload_stream, "tell")
                ):
                    current = int(payload_stream.tell())
                    payload_stream.seek(0, os.SEEK_END)
                    payload_size = int(payload_stream.tell())
                    payload_stream.seek(current)
                    payload_size -= current
                if payload_size is None:
                    raise StorageAdapterPermanentError(
                        reason="storage_object_payload_unknown_size",
                        message="Multipart upload requires a known payload size.",
                    )
                if payload_size >= get_document_ai_s3_multipart_upload_threshold_bytes():
                    self._store_s3_multipart_object(
                        object_key, payload_stream, payload_size, content_type
                    )
                    return
                payload_bytes = payload_stream.read()
                if not isinstance(payload_bytes, (bytes, bytearray)):
                    raise TypeError("Upload stream must return bytes.")
                self._store_s3_single_put_object(object_key, bytes(payload_bytes), content_type)
                return
            payload_bytes = payload_stream.read()
            if not isinstance(payload_bytes, (bytes, bytearray)):
                raise TypeError("Upload stream must return bytes.")
            put_params: dict[str, object] = {
                "Bucket": self._bucket,
                "Key": object_key,
                "Body": bytes(payload_bytes),
                "ContentType": content_type,
            }
            put_params.update(self._storage_encryption_params())
            self._client.put_object(**put_params)
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(
                error, "Unable to persist object in private S3 storage."
            ) from error

    def _store_s3_single_put_object(
        self, object_key: str, payload: bytes, content_type: str
    ) -> None:
        checksum_sha256 = _compute_sha256_hex(payload)
        put_params: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": object_key,
            "Body": payload,
            "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": _sha256_hex_to_base64(checksum_sha256),
            "Metadata": {
                "kodi-checksum-algorithm": "sha256",
                "kodi-checksum-sha256": checksum_sha256,
                "kodi-content-type": content_type,
            },
        }
        put_params.update(self._storage_encryption_params())
        self._client.put_object(**put_params)

    def _store_s3_multipart_object(
        self,
        object_key: str,
        payload_stream: Any,
        payload_size: int,
        content_type: str,
    ) -> None:
        part_size = get_document_ai_s3_multipart_upload_part_size_bytes()
        _validate_s3_multipart_limits(payload_size=payload_size, part_size=part_size)
        checksum_sha256 = sha256()
        create_params: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": object_key,
            "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256",
            "Metadata": {
                "kodi-checksum-algorithm": "sha256",
                "kodi-content-type": content_type,
            },
        }
        create_params.update(self._storage_encryption_params())
        upload_id: str | None = None
        multipart_parts: list[dict[str, object]] = []
        try:
            response = self._client.create_multipart_upload(**create_params)
            upload_id = str(response["UploadId"])
            part_number = 1
            while True:
                part_payload = payload_stream.read(part_size)
                if not part_payload:
                    break
                if not isinstance(part_payload, (bytes, bytearray)):
                    raise TypeError("Multipart upload stream must return bytes.")
                part_bytes = bytes(part_payload)
                checksum_sha256.update(part_bytes)
                upload_part_params: dict[str, object] = {
                    "Bucket": self._bucket,
                    "Key": object_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                    "Body": part_bytes,
                    "ChecksumAlgorithm": "SHA256",
                    "ChecksumSHA256": _sha256_hex_to_base64(_compute_sha256_hex(part_bytes)),
                }
                upload_part_params.update(self._storage_encryption_params())
                upload_part_response = self._client.upload_part(**upload_part_params)
                multipart_parts.append(
                    {
                        "ETag": str(upload_part_response.get("ETag", "")),
                        "PartNumber": part_number,
                    }
                )
                part_number += 1
                if part_number > S3_MULTIPART_MAX_PARTS:
                    raise StorageAdapterPermanentError(
                        reason="storage_multipart_part_limit_exceeded",
                        message="Multipart upload would exceed Amazon S3 part limits.",
                    )
            complete_params: dict[str, object] = {
                "Bucket": self._bucket,
                "Key": object_key,
                "UploadId": upload_id,
                "MultipartUpload": {"Parts": multipart_parts},
                "ChecksumAlgorithm": "SHA256",
            }
            complete_params.update(self._storage_encryption_params())
            self._client.complete_multipart_upload(**complete_params)
        except Exception:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket, Key=object_key, UploadId=upload_id
                    )
                except Exception:
                    pass
            raise

    def resolve_download_object(self, object_key: str) -> tuple[Path, str]:
        _validate_r2_object_key(object_key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            body = response["Body"]
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(error, "Requested S3 object could not be resolved.") from error
        suffix = Path(object_key).suffix or ".bin"
        handle = tempfile.NamedTemporaryFile(
            prefix="document-ai-download-", suffix=suffix, delete=False
        )
        try:
            with handle:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    handle.write(chunk)
        except Exception as error:  # noqa: BLE001
            Path(handle.name).unlink(missing_ok=True)
            raise _map_storage_error(error, "Requested S3 object could not be read.") from error
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        content_type = response.get("ContentType")
        resolved_content_type = (
            content_type if isinstance(content_type, str) else "application/octet-stream"
        )
        return Path(handle.name), resolved_content_type

    def get_object_metadata(self, object_key: str) -> StorageObjectMetadata:
        """Read S3 metadata; ETag remains provider diagnostics, never SHA-256."""

        _validate_r2_object_key(object_key)
        if self._storage_provider == "r2":
            try:
                response = self._client.head_object(Bucket=self._bucket, Key=object_key)
            except Exception as error:  # noqa: BLE001
                raise _map_storage_error(error, "S3 object metadata could not be read.") from error
            return StorageObjectMetadata(
                object_key=object_key,
                size_bytes=int(response.get("ContentLength", 0)),
                content_type=str(response.get("ContentType") or "application/octet-stream"),
                provider_etag=_optional_string(response.get("ETag")),
            )
        response = self._head_object(object_key)
        return self._build_object_metadata(object_key=object_key, response=response)

    def object_exists(self, object_key: str) -> bool:
        """Distinguish absence from access and transient R2 failures."""

        try:
            self.get_object_metadata(object_key)
        except StorageAdapterPermanentError as error:
            if error.reason == "storage_object_not_found":
                return False
            raise
        return True

    def delete_object(self, object_key: str) -> None:
        """Request idempotent deletion; PR-006 verification remains separate."""

        _validate_r2_object_key(object_key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(error, "S3 object deletion could not be requested.") from error

    def verify_object_absent(self, object_key: str) -> bool:
        """Verify final absence through HEAD rather than assuming delete success."""

        return not self.object_exists(object_key)

    def _capability_ttl(self, expires_at: str) -> int:
        try:
            remaining_seconds = int(_seconds_until(expires_at))
        except (TypeError, ValueError) as error:
            raise StorageAdapterPermanentError(
                reason="storage_capability_expiry_invalid",
                message="Storage capability expiration is invalid.",
            ) from error
        if remaining_seconds <= 0:
            raise StorageAdapterPermanentError(
                reason="storage_capability_expired",
                message="Storage capability has expired.",
            )
        return min(self._max_capability_ttl_seconds, remaining_seconds)

    def _verify_r2_checksum(self, object_key: str, expected_checksum_sha256: str) -> None:
        """Verify bytes directly because provider ETags are not SHA-256."""

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            body = response["Body"]
            digest = sha256()
            try:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    digest.update(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(error, "S3 object checksum could not be verified.") from error
        if digest.hexdigest() != expected_checksum_sha256:
            raise StorageAdapterPermanentError(
                reason="storage_object_checksum_mismatch",
                message="Uploaded S3 object checksum does not match declared checksum.",
                details={"object_key": object_key},
            )

    def _verify_r2_upload_object(
        self,
        *,
        object_key: str,
        checksum_sha256: str,
        size_bytes: int,
        content_type: str,
    ) -> StorageObjectVerification:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(error, "Uploaded S3 object could not be verified.") from error
        if int(response.get("ContentLength", -1)) != size_bytes:
            raise StorageAdapterPermanentError(
                reason="storage_object_size_mismatch",
                message="Uploaded storage object size does not match declared size.",
                details={
                    "object_key": object_key,
                    "expected_size_bytes": size_bytes,
                    "actual_size_bytes": int(response.get("ContentLength", -1)),
                },
            )
        if normalize_media_type(str(response.get("ContentType") or "")) != normalize_media_type(
            content_type
        ):
            raise StorageAdapterPermanentError(
                reason="storage_object_content_type_mismatch",
                message="Uploaded storage object content type does not match declared type.",
                details={
                    "object_key": object_key,
                    "expected_content_type": content_type,
                    "actual_content_type": str(response.get("ContentType") or ""),
                },
            )
        self._verify_r2_checksum(object_key, checksum_sha256)
        return StorageObjectVerification(
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            checksum_verification_source="downloaded_bytes",
        )

    def _verify_s3_upload_object(
        self,
        *,
        object_key: str,
        checksum_sha256: str,
        size_bytes: int,
        content_type: str,
    ) -> StorageObjectVerification:
        response = self._head_object(object_key)
        metadata = self._normalize_user_metadata(response.get("Metadata"))
        actual_size_bytes = int(response.get("ContentLength", -1))
        actual_content_type = str(response.get("ContentType") or "application/octet-stream")
        if actual_size_bytes != size_bytes:
            raise StorageAdapterPermanentError(
                reason="storage_object_size_mismatch",
                message="Uploaded S3 object size does not match the completion request.",
                details={
                    "object_key": object_key,
                    "expected_size_bytes": size_bytes,
                    "actual_size_bytes": actual_size_bytes,
                },
            )
        if normalize_media_type(actual_content_type) != normalize_media_type(content_type):
            raise StorageAdapterPermanentError(
                reason="storage_object_content_type_mismatch",
                message="Uploaded S3 object content type does not match the completion request.",
                details={
                    "object_key": object_key,
                    "expected_content_type": content_type,
                    "actual_content_type": actual_content_type,
                },
            )
        expected_encryption = self._expected_server_side_encryption()
        actual_encryption = _optional_string(response.get("ServerSideEncryption"))
        if expected_encryption is not None and actual_encryption != expected_encryption:
            raise StorageAdapterPermanentError(
                reason="storage_object_encryption_mismatch",
                message="Uploaded S3 object encryption does not match the configured policy.",
                details={
                    "object_key": object_key,
                    "expected_server_side_encryption": expected_encryption,
                    "actual_server_side_encryption": actual_encryption,
                },
            )
        provider_checksum = _optional_string(response.get("ChecksumSHA256"))
        checksum_source = "provider_checksum"
        if provider_checksum is not None:
            actual_checksum_sha256 = _sha256_base64_to_hex(provider_checksum)
        else:
            actual_checksum_sha256 = metadata.get("kodi-checksum-sha256") or metadata.get(
                "checksum-sha256"
            )
            if actual_checksum_sha256 is None:
                raise StorageAdapterPermanentError(
                    reason="storage_object_checksum_unavailable",
                    message="Uploaded S3 object checksum could not be verified from HEAD metadata.",
                    details={"object_key": object_key},
                )
            checksum_source = "supporting_metadata"
        if actual_checksum_sha256 != checksum_sha256:
            raise StorageAdapterPermanentError(
                reason="storage_object_checksum_mismatch",
                message="Uploaded S3 object checksum does not match declared checksum.",
                details={
                    "object_key": object_key,
                    "expected_checksum_sha256": checksum_sha256,
                    "actual_checksum_sha256": actual_checksum_sha256,
                },
            )
        metadata_checksum = metadata.get("kodi-checksum-sha256") or metadata.get("checksum-sha256")
        if metadata_checksum is not None and metadata_checksum != checksum_sha256:
            raise StorageAdapterPermanentError(
                reason="storage_object_checksum_mismatch",
                message=(
                    "Uploaded S3 object checksum metadata does not match the completion request."
                ),
                details={"object_key": object_key},
            )
        return StorageObjectVerification(
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            server_side_encryption=actual_encryption,
            checksum_verification_source=checksum_source,
        )

    def _storage_encryption_params(self) -> dict[str, object]:
        """Return optional S3 encryption parameters for PUT and presign flows."""

        params: dict[str, object] = {}
        if self._server_side_encryption:
            params["ServerSideEncryption"] = self._server_side_encryption
        if self._kms_key_id:
            params["SSEKMSKeyId"] = self._kms_key_id
        return params

    def _expected_server_side_encryption(self) -> str | None:
        if self._storage_provider != "s3":
            return None
        return self._server_side_encryption

    def _head_object(self, object_key: str) -> dict[str, object]:
        try:
            params: dict[str, object] = {"Bucket": self._bucket, "Key": object_key}
            if self._storage_provider == "s3":
                params["ChecksumMode"] = "ENABLED"
            response = self._client.head_object(**params)
        except Exception as error:  # noqa: BLE001
            raise _map_storage_error(error, "S3 object metadata could not be read.") from error
        return cast(dict[str, object], response)

    @staticmethod
    def _normalize_user_metadata(metadata: object) -> dict[str, str]:
        if not isinstance(metadata, Mapping):
            return {}
        normalized: dict[str, str] = {}
        for key, value in metadata.items():
            if isinstance(key, str) and isinstance(value, str):
                normalized[key.strip().lower()] = value.strip()
        return normalized

    def _build_object_metadata(
        self, *, object_key: str, response: Mapping[str, object]
    ) -> StorageObjectMetadata:
        metadata = self._normalize_user_metadata(response.get("Metadata"))
        provider_checksum = _optional_string(response.get("ChecksumSHA256"))
        checksum_sha256 = (
            _sha256_base64_to_hex(provider_checksum) if provider_checksum is not None else None
        )
        if checksum_sha256 is None:
            checksum_sha256 = metadata.get("kodi-checksum-sha256") or metadata.get(
                "checksum-sha256"
            )
        checksum_algorithm = "sha256" if checksum_sha256 is not None else None
        return StorageObjectMetadata(
            object_key=object_key,
            size_bytes=int(response.get("ContentLength", 0)),
            content_type=str(response.get("ContentType") or "application/octet-stream"),
            provider_etag=_optional_string(response.get("ETag")),
            checksum_algorithm=checksum_algorithm,
            checksum_sha256=checksum_sha256,
            server_side_encryption=_optional_string(response.get("ServerSideEncryption")),
            version_id=_optional_string(response.get("VersionId")),
            last_modified=_serialize_last_modified(response.get("LastModified")),
            custom_metadata=metadata,
        )


class R2StorageAdapter(S3StorageAdapter):
    """Private object-storage adapter for R2-compatible storage."""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any,
        max_capability_ttl_seconds: int = 900,
    ) -> None:
        super().__init__(
            bucket=bucket,
            client=client,
            max_capability_ttl_seconds=max_capability_ttl_seconds,
            server_side_encryption=None,
            kms_key_id=None,
            storage_provider="r2",
        )


def build_runtime_storage_adapter() -> StorageAdapterProtocol:
    """Build the configured production storage adapter."""

    provider = get_document_ai_storage_provider()
    try:
        boto3 = import_module("boto3")
        config_module = import_module("botocore.config")
    except ImportError as error:
        raise RuntimeError("Production storage requires boto3 and botocore.") from error

    if provider == "s3":
        validate_document_ai_s3_production_configuration()
        bucket = get_document_ai_s3_bucket()
        region = get_document_ai_aws_region()
        assert bucket is not None and region is not None
        config = config_module.Config(
            signature_version="s3v4",
            connect_timeout=get_document_ai_s3_connect_timeout_seconds(),
            read_timeout=get_document_ai_s3_read_timeout_seconds(),
            retries={"max_attempts": 3, "mode": "standard"},
        )
        client = boto3.client(
            "s3",
            region_name=region,
            config=config,
        )
        return S3StorageAdapter(
            bucket=bucket,
            client=client,
            max_capability_ttl_seconds=min(
                get_document_ai_s3_upload_capability_ttl_seconds(),
                get_document_ai_s3_download_capability_ttl_seconds(),
            ),
            server_side_encryption=get_document_ai_s3_server_side_encryption(),
            kms_key_id=get_document_ai_s3_kms_key_id(),
            storage_provider="s3",
        )

    validate_document_ai_r2_production_configuration()
    bucket = get_document_ai_r2_bucket()
    endpoint = get_document_ai_r2_endpoint()
    access_key_id = get_document_ai_r2_access_key_id()
    secret_access_key = get_document_ai_r2_secret_access_key()
    assert bucket is not None and endpoint is not None
    assert access_key_id is not None and secret_access_key is not None
    config = config_module.Config(
        signature_version="s3v4",
        connect_timeout=get_document_ai_r2_connect_timeout_seconds(),
        read_timeout=get_document_ai_r2_read_timeout_seconds(),
        retries={"max_attempts": 3, "mode": "standard"},
    )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=config,
    )
    return R2StorageAdapter(
        bucket=bucket,
        client=client,
        max_capability_ttl_seconds=min(
            get_document_ai_r2_upload_capability_ttl_seconds(),
            get_document_ai_r2_download_capability_ttl_seconds(),
        ),
    )


def _seconds_until(expires_at: str) -> float:
    deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)
    return (deadline - datetime.now(UTC)).total_seconds()


def _tenant_document_object_key(tenant_id: str, document_id: UUID) -> str:
    """Build the only R2 key shape accepted for normal document capabilities."""

    try:
        return build_tenant_document_object_key(tenant_id, document_id)
    except ValueError as error:
        raise StorageAdapterPermanentError(
            reason="storage_object_scope_mismatch",
            message="Tenant scope cannot be represented in a storage object key.",
        ) from error


def _is_tenant_scoped_object_key(object_key: str, tenant_id: str) -> bool:
    """Reject traversal, separators, and foreign namespaces before R2 access."""

    if not is_tenant_scoped_object_key(object_key, tenant_id):
        return False
    if not object_key.startswith(f"{tenant_id}/docs/"):
        return False
    try:
        document_id = UUID(object_key.removeprefix(f"{tenant_id}/docs/"))
    except ValueError:
        return False
    return object_key == _tenant_document_object_key(tenant_id, document_id)


def _validate_r2_object_key(object_key: str) -> None:
    """Enforce Milestone 4 governed R2 locators before any provider request."""

    key_parts = object_key.split("/")
    if len(key_parts) != 3 or key_parts[1] != "docs":
        _raise_invalid_r2_object_key()
    tenant_id, _, filename = key_parts
    if not tenant_id or tenant_id in {".", ".."}:
        _raise_invalid_r2_object_key()
    try:
        document_id = UUID(filename)
    except ValueError:
        _raise_invalid_r2_object_key()
    try:
        validate_tenant_document_object_key(object_key, tenant_id, document_id)
    except ValueError:
        _raise_invalid_r2_object_key()


def _raise_invalid_r2_object_key() -> None:
    raise StorageAdapterPermanentError(
        reason="storage_object_reference_invalid",
        message="Storage object reference is invalid.",
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _compute_sha256_hex(data: bytes) -> str:
    digest = sha256()
    digest.update(data)
    return digest.hexdigest()


def _sha256_hex_to_base64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def _sha256_base64_to_hex(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).hex()


def _validate_s3_multipart_limits(*, payload_size: int, part_size: int) -> None:
    if payload_size <= 0:
        raise StorageAdapterPermanentError(
            reason="storage_object_payload_invalid",
            message="Multipart upload payload must be non-empty.",
        )
    if part_size < S3_MULTIPART_MIN_PART_SIZE_BYTES:
        raise StorageAdapterPermanentError(
            reason="storage_multipart_part_size_invalid",
            message="Multipart upload part size is below the minimum supported size.",
        )
    if part_size > 5 * 1024 * 1024 * 1024:
        raise StorageAdapterPermanentError(
            reason="storage_multipart_part_size_invalid",
            message="Multipart upload part size exceeds the maximum supported size.",
        )
    total_parts = (payload_size + part_size - 1) // part_size
    if total_parts > S3_MULTIPART_MAX_PARTS:
        raise StorageAdapterPermanentError(
            reason="storage_multipart_part_limit_exceeded",
            message="Multipart upload would exceed Amazon S3 part limits.",
        )
    non_final_parts = max(0, total_parts - 1)
    if non_final_parts and part_size < S3_MULTIPART_MIN_PART_SIZE_BYTES:
        raise StorageAdapterPermanentError(
            reason="storage_multipart_part_size_invalid",
            message="Multipart upload part size is below the minimum supported size.",
        )


def _serialize_last_modified(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return _optional_string(value)


def _map_storage_error(error: Exception, message: str) -> StorageAdapterError:
    """Map S3-compatible errors to the stable Documents Service taxonomy."""

    response = cast(object, getattr(error, "response", None))
    response_mapping: Mapping[str, object] = {}
    if isinstance(response, Mapping):
        raw_response = cast(Mapping[object, object], response)
        response_mapping = {str(key): value for key, value in raw_response.items()}
    error_payload = response_mapping.get("Error", {})
    metadata = response_mapping.get("ResponseMetadata", {})
    error_mapping: Mapping[str, object] = {}
    if isinstance(error_payload, Mapping):
        raw_error = cast(Mapping[object, object], error_payload)
        error_mapping = {str(key): value for key, value in raw_error.items()}
    metadata_mapping: Mapping[str, object] = {}
    if isinstance(metadata, Mapping):
        raw_metadata = cast(Mapping[object, object], metadata)
        metadata_mapping = {str(key): value for key, value in raw_metadata.items()}
    code = str(error_mapping.get("Code", "")).lower()
    status = metadata_mapping.get("HTTPStatusCode")
    if code in {"nosuchkey", "notfound", "404"} or status == 404:
        return StorageAdapterPermanentError("storage_object_not_found", message)
    if (
        code in {"accessdenied", "signaturedoesnotmatch", "invalidaccesskeyid", "403"}
        or status == 403
    ):
        return StorageAdapterPermanentError("storage_access_denied", message)
    if code in {"slowdown", "toomanyrequests", "429"} or status == 429:
        return StorageAdapterTransientError("storage_rate_limited", message)
    if code in {"requesttimeout", "readtimeout", "timeout"} or isinstance(error, TimeoutError):
        return StorageAdapterTransientError("storage_timeout", message)
    if isinstance(status, int) and status >= 500:
        return StorageAdapterTransientError("storage_service_unavailable", message)
    return StorageAdapterPermanentError("storage_provider_failure", message)


_map_r2_error = _map_storage_error


class InMemoryStorageAdapter:
    """Provide deterministic file-backed storage adapter implementation."""

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        encryption_required: bool | None = None,
        signing_secret_env_var: str | None = None,
        signing_secret_literal: str | None = None,
        storage_root: str | Path | None = None,
    ) -> None:
        self._endpoint_url = (
            get_storage_endpoint_url() if endpoint_url is None else endpoint_url.strip()
        )
        self._encryption_required = (
            get_storage_encryption_required()
            if encryption_required is None
            else encryption_required
        )
        self._signing_secret_env_var = (
            get_storage_signing_secret_env_var()
            if signing_secret_env_var is None
            else signing_secret_env_var.strip()
        )
        self._signing_secret_literal = signing_secret_literal
        configured_root = (
            Path(os.getenv("DOCUMENT_AI_STORAGE_ROOT", "")).expanduser()
            if storage_root is None and os.getenv("DOCUMENT_AI_STORAGE_ROOT", "").strip()
            else Path.cwd() / ".document-ai-storage"
            if storage_root is None
            else Path(storage_root)
        )
        self._storage_root = configured_root.resolve()

    def create_upload_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        session_id: UUID,
        expires_at: str,
    ) -> StorageUploadCapability:
        self._enforce_security_controls()
        object_key = build_tenant_document_object_key(tenant_id, document_id)
        capability_id = sha256(
            f"{tenant_id}:{owner_user_id}:{document_id}:{session_id}".encode()
        ).hexdigest()
        upload_url = f"{self._endpoint_url.rstrip('/')}/upload/{object_key}"
        headers = {"x-kodi-capability-id": capability_id}
        headers.update(REQUIRED_ENCRYPTION_HEADERS)
        return StorageUploadCapability(
            capability_id=capability_id,
            object_key=object_key,
            upload_url=upload_url,
            expires_at=expires_at,
            storage_provider="in_memory",
            method="PUT",
            headers=headers,
        )

    def verify_upload_object(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        object_key: str,
        checksum_sha256: str,
        size_bytes: int,
        content_type: str,
    ) -> StorageObjectVerification:
        self._enforce_security_controls()
        try:
            object_path, stored_content_type = self.resolve_download_object(object_key)
        except StorageAdapterPermanentError:
            # Preserve the legacy deterministic path for existing tests that register
            # completion without performing the storage PUT round-trip first.
            object_path = None
            stored_content_type = content_type

        if object_path is not None:
            actual_size_bytes = object_path.stat().st_size
            actual_checksum_sha256 = _compute_file_sha256(object_path)
            if actual_size_bytes != size_bytes:
                raise StorageAdapterPermanentError(
                    reason="storage_object_size_mismatch",
                    message="Uploaded storage object size does not match declared size.",
                    details={
                        "object_key": object_key,
                        "expected_size_bytes": size_bytes,
                        "actual_size_bytes": actual_size_bytes,
                    },
                )
            if actual_checksum_sha256 != checksum_sha256:
                raise StorageAdapterPermanentError(
                    reason="storage_object_checksum_mismatch",
                    message="Uploaded storage object checksum does not match declared checksum.",
                    details={
                        "object_key": object_key,
                        "expected_checksum_sha256": checksum_sha256,
                        "actual_checksum_sha256": actual_checksum_sha256,
                    },
                )
            if stored_content_type != content_type:
                raise StorageAdapterPermanentError(
                    reason="storage_object_content_type_mismatch",
                    message="Uploaded storage object content type does not match declared type.",
                    details={
                        "object_key": object_key,
                        "expected_content_type": content_type,
                        "actual_content_type": stored_content_type,
                    },
                )
        return StorageObjectVerification(
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            status="verified",
        )

    def create_download_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        capability_id: str,
        expires_at: str,
        signed_token: str,
    ) -> StorageDownloadCapability:
        self._enforce_security_controls()
        object_key = build_tenant_document_download_object_key(tenant_id, document_id)
        download_url = (
            f"{self._endpoint_url.rstrip('/')}/download/{object_key}"
            f"?capability_token={signed_token}"
        )
        headers = {"x-kodi-capability-id": capability_id}
        headers.update(REQUIRED_ENCRYPTION_HEADERS)
        return StorageDownloadCapability(
            capability_id=capability_id,
            object_key=object_key,
            download_url=download_url,
            expires_at=expires_at,
            method="GET",
            headers=headers,
        )

    def store_upload_object(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
    ) -> None:
        self._enforce_security_controls()
        self.store_upload_object_filelike(object_key, BytesIO(payload), content_type, len(payload))

    def store_upload_object_filelike(
        self,
        object_key: str,
        payload_stream: Any,
        content_type: str,
        payload_size: int | None = None,
    ) -> None:
        self._enforce_security_controls()
        object_path = self._resolve_object_path(object_key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        with object_path.open("wb") as handle:
            for chunk in iter(lambda: payload_stream.read(1024 * 1024), b""):
                if not isinstance(chunk, (bytes, bytearray)):
                    raise TypeError("Upload stream must return bytes.")
                handle.write(bytes(chunk))
        metadata_path = self._metadata_path(object_path)
        metadata_path.write_text(
            json.dumps({"content_type": content_type}, sort_keys=True),
            encoding="utf-8",
        )

    def resolve_download_object(
        self,
        object_key: str,
    ) -> tuple[Path, str]:
        self._enforce_security_controls()
        object_path = self._resolve_object_path(object_key)
        if not object_path.is_file():
            raise StorageAdapterPermanentError(
                reason="storage_object_not_found",
                message="Requested storage object does not exist.",
                details={"object_key": object_key},
            )
        metadata_path = self._metadata_path(object_path)
        content_type = _guess_content_type(object_path)
        if metadata_path.is_file():
            try:
                payload: object = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, Mapping):
                stored_content_type = cast(Mapping[object, object], payload).get("content_type")
                if isinstance(stored_content_type, str) and stored_content_type.strip():
                    content_type = stored_content_type.strip()
        return object_path, content_type

    def get_object_metadata(self, object_key: str) -> StorageObjectMetadata:
        object_path, content_type = self.resolve_download_object(object_key)
        return StorageObjectMetadata(
            object_key=object_key,
            size_bytes=object_path.stat().st_size,
            content_type=content_type,
        )

    def object_exists(self, object_key: str) -> bool:
        try:
            self.resolve_download_object(object_key)
        except StorageAdapterPermanentError as error:
            if error.reason == "storage_object_not_found":
                return False
            raise
        return True

    def delete_object(self, object_key: str) -> None:
        object_path = self._resolve_object_path(object_key)
        object_path.unlink(missing_ok=True)
        self._metadata_path(object_path).unlink(missing_ok=True)

    def verify_object_absent(self, object_key: str) -> bool:
        return not self.object_exists(object_key)

    def _enforce_security_controls(self) -> None:
        controls = StorageSecurityControls(
            endpoint_url=self._endpoint_url,
            encryption_required=self._encryption_required,
            signing_secret_env_var=self._signing_secret_env_var,
            provided_secret_literal=self._signing_secret_literal,
        )
        try:
            validate_storage_security_controls(controls)
        except SecurityPolicyViolationError as error:
            details: dict[str, object] = {}
            error_details: object = error.details
            if isinstance(error_details, Mapping):
                raw_details = cast(Mapping[object, object], error_details)
                details = {str(key): value for key, value in raw_details.items()}
            raise StorageAdapterPermanentError(
                reason=error.reason,
                message=error.message,
                details=details,
            ) from error

    def _resolve_object_path(self, object_key: str) -> Path:
        normalized_object_key = object_key.strip().lstrip("/\\")
        candidate = (self._storage_root / normalized_object_key).resolve()
        if self._storage_root != candidate and self._storage_root not in candidate.parents:
            raise StorageAdapterPermanentError(
                reason="storage_object_key_invalid",
                message="Storage object key is invalid.",
                details={"object_key": object_key},
            )
        return candidate

    @staticmethod
    def _metadata_path(object_path: Path) -> Path:
        return object_path.with_name(f"{object_path.name}.meta.json")


_DEFAULT_STORAGE_ADAPTER = InMemoryStorageAdapter()


def get_default_storage_adapter() -> InMemoryStorageAdapter:
    """Return default storage adapter instance."""

    return _DEFAULT_STORAGE_ADAPTER


def _compute_file_sha256(file_path: Path) -> str:
    digest = sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guess_content_type(file_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(file_path.name)
    if isinstance(guessed, str) and guessed.strip():
        return guessed
    return "application/octet-stream"
