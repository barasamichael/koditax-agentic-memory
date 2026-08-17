"""Focused S3 storage-adapter regression coverage."""

from __future__ import annotations

from io import BytesIO
from uuid import UUID
import base64
import services.document_ai.app.storage_adapter as storage_adapter_module
from hashlib import sha256

import pytest

from services.document_ai.app.storage_adapter import S3StorageAdapter
from services.document_ai.app.storage_adapter import StorageAdapterPermanentError
from services.document_ai.app.storage_adapter import StorageAdapterTransientError

DOCUMENT_ID = UUID("4cb1a057-2fab-44aa-a700-a10d4d2f0a91")
OWNER_ID = UUID("0db7ad5e-dd9a-4a1e-bce0-e3182d3a34a9")
SESSION_ID = UUID("53f3d5c7-dd98-4f56-b0b7-a78fb503cc13")
EXPIRY = "2030-01-01T00:00:00Z"
PAYLOAD = b"document-ai-s3-object"
CHECKSUM_SHA256 = "1" * 64


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.put_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.create_multipart_calls: list[dict[str, object]] = []
        self.upload_part_calls: list[dict[str, object]] = []
        self.complete_multipart_calls: list[dict[str, object]] = []
        self.abort_multipart_calls: list[dict[str, object]] = []
        self.multipart_uploads: dict[str, dict[str, object]] = {}
        self.fail_part_number: int | None = None

    def generate_presigned_url(self, operation: str, **kwargs: object) -> str:
        return f"https://s3.example.invalid/{operation}"

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(dict(kwargs))
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, (bytes, bytearray))
        metadata = dict(kwargs.get("Metadata", {}) or {})
        self.objects[key] = {
            "body": bytes(body),
            "content_type": str(kwargs.get("ContentType") or "application/octet-stream"),
            "checksum_sha256": metadata.get("kodi-checksum-sha256", CHECKSUM_SHA256),
            "server_side_encryption": kwargs.get("ServerSideEncryption"),
            "metadata": metadata,
        }

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_calls.append(dict(kwargs))
        key = str(kwargs["Key"])
        stored = self.objects[key]
        return {
            "ContentLength": len(stored["body"]),
            "ContentType": stored["content_type"],
            "ChecksumSHA256": _sha256_hex_to_base64(str(stored["checksum_sha256"])),
            "ServerSideEncryption": stored["server_side_encryption"],
            "ETag": '"fake-etag"',
            "Metadata": stored["metadata"],
            "LastModified": None,
            "VersionId": "1",
        }

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects[key]
        return {"Body": BytesIO(bytes(stored["body"])), "ContentType": stored["content_type"]}

    def delete_object(self, **kwargs: object) -> None:
        self.objects.pop(str(kwargs["Key"]), None)

    def create_multipart_upload(self, **kwargs: object) -> dict[str, object]:
        self.create_multipart_calls.append(dict(kwargs))
        upload_id = f"upload-{len(self.create_multipart_calls)}"
        self.multipart_uploads[upload_id] = {
            "content_type": str(kwargs.get("ContentType") or "application/octet-stream"),
            "metadata": dict(kwargs.get("Metadata", {}) or {}),
            "encryption": kwargs.get("ServerSideEncryption"),
            "parts": {},
        }
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs: object) -> dict[str, object]:
        self.upload_part_calls.append(dict(kwargs))
        part_number = int(kwargs["PartNumber"])
        if self.fail_part_number is not None and part_number == self.fail_part_number:
            raise TimeoutError("simulated multipart failure")
        upload_id = str(kwargs["UploadId"])
        body = kwargs["Body"]
        assert isinstance(body, (bytes, bytearray))
        session = self.multipart_uploads[upload_id]
        parts: dict[int, bytes] = session["parts"]  # type: ignore[assignment]
        parts[part_number] = bytes(body)
        return {"ETag": f'"part-{part_number}"'}

    def complete_multipart_upload(self, **kwargs: object) -> dict[str, object]:
        self.complete_multipart_calls.append(dict(kwargs))
        upload_id = str(kwargs["UploadId"])
        session = self.multipart_uploads.pop(upload_id)
        parts: dict[int, bytes] = session["parts"]  # type: ignore[assignment]
        assembled = b"".join(parts[index] for index in sorted(parts))
        checksum_sha256 = sha256(assembled).hexdigest()
        key = str(kwargs["Key"])
        self.objects[key] = {
            "body": assembled,
            "content_type": session["content_type"],
            "checksum_sha256": checksum_sha256,
            "server_side_encryption": session["encryption"],
            "metadata": {
                "kodi-checksum-algorithm": "sha256",
                "kodi-checksum-sha256": checksum_sha256,
                "kodi-content-type": session["content_type"],
            },
        }
        return {"ETag": '"multipart-etag"'}

    def abort_multipart_upload(self, **kwargs: object) -> None:
        self.abort_multipart_calls.append(dict(kwargs))
        self.multipart_uploads.pop(str(kwargs["UploadId"]), None)


def _sha256_hex_to_base64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def _payload_checksum_sha256() -> str:
    return sha256(PAYLOAD).hexdigest()


def _adapter() -> tuple[S3StorageAdapter, _FakeS3Client]:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(
        bucket="private-documents",
        client=client,
        max_capability_ttl_seconds=900,
        server_side_encryption="AES256",
        storage_provider="s3",
    )
    return adapter, client


def test_s3_upload_object_stores_checksum_metadata_and_enforces_sse() -> None:
    adapter, client = _adapter()

    adapter.store_upload_object(f"tenant-a/docs/{DOCUMENT_ID}", PAYLOAD, "application/pdf")

    assert client.put_calls, "expected upload to be recorded"
    put_call = client.put_calls[0]
    assert put_call["ChecksumAlgorithm"] == "SHA256"
    assert put_call["ChecksumSHA256"] == _sha256_hex_to_base64(_payload_checksum_sha256())
    assert put_call["ServerSideEncryption"] == "AES256"
    assert put_call["Metadata"]["kodi-checksum-algorithm"] == "sha256"
    assert put_call["Metadata"]["kodi-content-type"] == "application/pdf"


def test_s3_upload_object_uses_multipart_for_large_payloads_and_preserves_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _adapter()
    payload = b"a" * (11 * 1024 * 1024)
    monkeypatch.setattr(
        storage_adapter_module,
        "get_document_ai_s3_multipart_upload_threshold_bytes",
        lambda: 4 * 1024 * 1024,
    )
    monkeypatch.setattr(
        storage_adapter_module,
        "get_document_ai_s3_multipart_upload_part_size_bytes",
        lambda: 5 * 1024 * 1024,
    )

    adapter.store_upload_object_filelike(
        f"tenant-a/docs/{DOCUMENT_ID}",
        BytesIO(payload),
        "application/pdf",
        payload_size=len(payload),
    )

    assert client.create_multipart_calls
    assert len(client.upload_part_calls) == 3
    assert client.complete_multipart_calls
    assert not client.abort_multipart_calls
    uploaded = client.objects[f"tenant-a/docs/{DOCUMENT_ID}"]
    assert uploaded["body"] == payload
    assert uploaded["metadata"]["kodi-checksum-sha256"] == sha256(payload).hexdigest()


def test_s3_upload_object_aborts_multipart_upload_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _adapter()
    payload = b"b" * (11 * 1024 * 1024)
    client.fail_part_number = 2
    monkeypatch.setattr(
        storage_adapter_module,
        "get_document_ai_s3_multipart_upload_threshold_bytes",
        lambda: 4 * 1024 * 1024,
    )
    monkeypatch.setattr(
        storage_adapter_module,
        "get_document_ai_s3_multipart_upload_part_size_bytes",
        lambda: 5 * 1024 * 1024,
    )

    with pytest.raises(StorageAdapterTransientError):
        adapter.store_upload_object_filelike(
            f"tenant-a/docs/{DOCUMENT_ID}",
            BytesIO(payload),
            "application/pdf",
            payload_size=len(payload),
        )

    assert client.create_multipart_calls
    assert client.abort_multipart_calls
    assert f"tenant-a/docs/{DOCUMENT_ID}" not in client.objects


def test_s3_verify_upload_object_uses_head_checksum_size_and_encryption() -> None:
    adapter, client = _adapter()
    object_key = f"tenant-a/docs/{DOCUMENT_ID}"
    client.objects[object_key] = {
        "body": PAYLOAD,
        "content_type": "application/pdf",
        "checksum_sha256": CHECKSUM_SHA256,
        "server_side_encryption": "AES256",
        "metadata": {"kodi-checksum-sha256": CHECKSUM_SHA256},
    }

    verified = adapter.verify_upload_object(
        "tenant-a",
        OWNER_ID,
        object_key,
        CHECKSUM_SHA256,
        len(PAYLOAD),
        "application/pdf",
    )

    assert verified.status == "verified"
    assert verified.server_side_encryption == "AES256"
    assert verified.checksum_verification_source == "provider_checksum"
    assert client.head_calls[0]["ChecksumMode"] == "ENABLED"


def test_s3_verify_upload_object_rejects_checksum_size_and_encryption_mismatches() -> None:
    adapter, client = _adapter()
    object_key = f"tenant-a/docs/{DOCUMENT_ID}"
    client.objects[object_key] = {
        "body": PAYLOAD,
        "content_type": "application/pdf",
        "checksum_sha256": CHECKSUM_SHA256,
        "server_side_encryption": "AES256",
        "metadata": {"kodi-checksum-sha256": CHECKSUM_SHA256},
    }

    with pytest.raises(StorageAdapterPermanentError, match="storage_object_size_mismatch"):
        adapter.verify_upload_object(
            "tenant-a",
            OWNER_ID,
            object_key,
            CHECKSUM_SHA256,
            len(PAYLOAD) + 1,
            "application/pdf",
        )

    with pytest.raises(StorageAdapterPermanentError, match="storage_object_checksum_mismatch"):
        adapter.verify_upload_object(
            "tenant-a",
            OWNER_ID,
            object_key,
            "2" * 64,
            len(PAYLOAD),
            "application/pdf",
        )

    client.objects[object_key]["server_side_encryption"] = None
    with pytest.raises(StorageAdapterPermanentError, match="storage_object_encryption_mismatch"):
        adapter.verify_upload_object(
            "tenant-a",
            OWNER_ID,
            object_key,
            CHECKSUM_SHA256,
            len(PAYLOAD),
            "application/pdf",
        )


def test_s3_get_object_metadata_exposes_head_metadata_without_trusting_etag() -> None:
    adapter, client = _adapter()
    object_key = f"tenant-a/docs/{DOCUMENT_ID}"
    client.objects[object_key] = {
        "body": PAYLOAD,
        "content_type": "application/pdf",
        "checksum_sha256": CHECKSUM_SHA256,
        "server_side_encryption": "AES256",
        "metadata": {"kodi-checksum-sha256": CHECKSUM_SHA256},
    }

    metadata = adapter.get_object_metadata(object_key)

    assert metadata.object_key == object_key
    assert metadata.size_bytes == len(PAYLOAD)
    assert metadata.content_type == "application/pdf"
    assert metadata.provider_etag == '"fake-etag"'
    assert metadata.checksum_sha256 == CHECKSUM_SHA256
    assert metadata.checksum_algorithm == "sha256"
    assert metadata.server_side_encryption == "AES256"
    assert metadata.version_id == "1"
    assert metadata.custom_metadata["kodi-checksum-sha256"] == CHECKSUM_SHA256
