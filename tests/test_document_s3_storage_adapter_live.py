"""Live Amazon S3 storage adapter coverage for Document AI."""

from __future__ import annotations

from uuid import uuid4
from pathlib import Path
import hashlib
import tempfile

from dotenv import load_dotenv
import pytest

from services.document_ai.app.config import get_document_ai_s3_bucket
from services.document_ai.app.config import get_document_ai_runtime_mode
from services.document_ai.app.config import get_document_ai_storage_provider
from services.document_ai.app.storage_adapter import S3StorageAdapter
from services.document_ai.app.storage_adapter import StorageAdapterPermanentError
from services.document_ai.app.storage_adapter import build_runtime_storage_adapter

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def test_document_ai_s3_storage_adapter_live_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_AI_RUNTIME_MODE", "production")
    monkeypatch.setenv("DOCUMENT_AI_STORAGE_PROVIDER", "s3")
    if not _live_s3_enabled():
        pytest.skip("Real Document AI S3 configuration is not available.")

    adapter = build_runtime_storage_adapter()
    assert isinstance(adapter, S3StorageAdapter)

    object_key = f"tenant-a/docs/{uuid4()}"
    payload = b"document-ai-s3-live-test"
    content_type = "text/plain"

    try:
        adapter.store_upload_object(object_key, payload, content_type)

        metadata = adapter.get_object_metadata(object_key)
        assert metadata.object_key == object_key
        assert metadata.size_bytes == len(payload)
        assert metadata.content_type == content_type
        assert metadata.provider_etag
        assert metadata.checksum_algorithm == "sha256"
        assert metadata.checksum_sha256
        assert metadata.server_side_encryption == "AES256"

        resolved_path, resolved_content_type = adapter.resolve_download_object(object_key)
        try:
            assert resolved_content_type == content_type
            assert resolved_path.read_bytes() == payload
        finally:
            resolved_path.unlink(missing_ok=True)

        assert adapter.object_exists(object_key) is True
        assert adapter.verify_object_absent(object_key) is False

        adapter.delete_object(object_key)
        assert adapter.verify_object_absent(object_key) is True
        with pytest.raises(StorageAdapterPermanentError, match="storage_object_not_found"):
            adapter.get_object_metadata(object_key)
    finally:
        adapter.delete_object(object_key)


def test_document_ai_s3_storage_adapter_live_200_mib_multipart_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_AI_RUNTIME_MODE", "production")
    monkeypatch.setenv("DOCUMENT_AI_STORAGE_PROVIDER", "s3")
    if not _live_s3_enabled():
        pytest.skip("Real Document AI S3 configuration is not available.")

    adapter = build_runtime_storage_adapter()
    assert isinstance(adapter, S3StorageAdapter)

    object_key = f"tenant-a/docs/{uuid4()}"
    chunk = b"0123456789abcdef" * (1024 * 64)
    payload_size = 200 * 1024 * 1024
    checksum = hashlib.sha256()

    try:
        with tempfile.NamedTemporaryFile(prefix="document-ai-200m-", suffix=".bin") as handle:
            remaining = payload_size
            while remaining > 0:
                part = chunk if remaining >= len(chunk) else chunk[:remaining]
                handle.write(part)
                checksum.update(part)
                remaining -= len(part)
            handle.flush()
            handle.seek(0)
            adapter.store_upload_object_filelike(
                object_key,
                handle,
                "application/octet-stream",
                payload_size=payload_size,
            )

        metadata = adapter.get_object_metadata(object_key)
        verified = adapter.verify_upload_object(
            "tenant-a",
            uuid4(),
            object_key,
            checksum.hexdigest(),
            payload_size,
            "application/octet-stream",
        )
        assert metadata.object_key == object_key
        assert metadata.size_bytes == payload_size
        assert metadata.checksum_sha256 == checksum.hexdigest()
        assert verified.status == "verified"
    finally:
        adapter.delete_object(object_key)


def _live_s3_enabled() -> bool:
    bucket = get_document_ai_s3_bucket()
    return (
        get_document_ai_runtime_mode() == "production"
        and get_document_ai_storage_provider() == "s3"
        and isinstance(bucket, str)
        and bucket.strip() != ""
        and not bucket.strip().startswith("<")
    )
