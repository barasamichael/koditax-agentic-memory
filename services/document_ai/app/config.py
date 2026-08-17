"""Deterministic configuration for document_ai ingestion policy controls."""

from __future__ import annotations

import os
import re
from typing import cast
from typing import Literal
from dataclasses import dataclass
from urllib.parse import urlparse

from services.document_ai.app.document_formats import supported_media_types
from services.document_ai.app.document_formats import is_supported_media_type

# One registry governs all active ingestion formats.  Do not add MIME lists to
# handlers: source inspection confirms the actual bytes after direct R2 upload.
ALLOWED_UPLOAD_MIME_TYPES = supported_media_types()
ARCHITECTURE_DEFINED_UNSUPPORTED_MIME_TYPES: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Preprocessing policy — B.3.4
#
# For application/pdf:
#   Stage 1: MIME-type allowlist check (this module)
#   Stage 2: File-size bounds check (this module)
#   Stage 3: SHA-256 checksum format validation (this module)
#   Stage 4: Tenant-scoped object-key validation (config.is_tenant_scoped_object_key)
#   Stage 5: Storage-layer upload verification (StorageAdapterProtocol.verify_upload_object)
#
# No OCR, image normalization, HEIC conversion, or structured-file schema
# parsing stages exist in the current runtime. If those stages are added,
# their governing constants must be declared here.
# ---------------------------------------------------------------------------

MAX_UPLOAD_SIZE_BYTES = 200 * 1024 * 1024
SOURCE_INSPECTION_POLICY_VERSION = "v1"
MAX_SOURCE_INSPECTION_PAGES = 1_000
MAX_SOURCE_INSPECTION_IMAGE_PIXELS = 40_000_000
SOURCE_INSPECTION_SCOPE_SIZE = 50
UPLOAD_SESSION_TTL_MINUTES = 15
UPLOAD_SESSION_TTL_SECONDS = UPLOAD_SESSION_TTL_MINUTES * 60
S3_MULTIPART_MIN_PART_SIZE_BYTES = 5 * 1024 * 1024
S3_MULTIPART_MAX_PART_SIZE_BYTES = 5 * 1024 * 1024 * 1024
S3_MULTIPART_MAX_PARTS = 10_000
S3_MULTIPART_UPLOAD_THRESHOLD_BYTES = 32 * 1024 * 1024
S3_MULTIPART_UPLOAD_PART_SIZE_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# Source inspection and OpenAI understanding determine governed target processing.
# ---------------------------------------------------------------------------

CLASSIFICATION_AUTO_ACCEPT_THRESHOLD = 0.95
CLASSIFICATION_REVIEW_THRESHOLD = 0.80
CHECKSUM_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
DocumentDuplicatePolicy = Literal["reuse_existing_document"]
DOCUMENT_DUPLICATE_POLICY: DocumentDuplicatePolicy = "reuse_existing_document"

DEFAULT_STORAGE_ENDPOINT_URL = "https://storage.local"
DEFAULT_STORAGE_ENCRYPTION_REQUIRED = True
DEFAULT_STORAGE_SIGNING_SECRET_ENV_VAR = "DOCUMENT_AI_STORAGE_SIGNING_SECRET"
DOCUMENT_AI_RUNTIME_MODE_ENV_VAR = "DOCUMENT_AI_RUNTIME_MODE"
DOCUMENT_AI_STORAGE_PROVIDER_ENV_VAR = "DOCUMENT_AI_STORAGE_PROVIDER"
DOCUMENT_AI_AWS_REGION_ENV_VAR = "DOCUMENT_AI_AWS_REGION"
DOCUMENT_AI_S3_BUCKET_ENV_VAR = "DOCUMENT_AI_S3_BUCKET"
DOCUMENT_AI_S3_SERVER_SIDE_ENCRYPTION_ENV_VAR = "DOCUMENT_AI_S3_SERVER_SIDE_ENCRYPTION"
DOCUMENT_AI_S3_KMS_KEY_ID_ENV_VAR = "DOCUMENT_AI_S3_KMS_KEY_ID"
DOCUMENT_AI_S3_CONNECT_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_S3_CONNECT_TIMEOUT_SECONDS"
DOCUMENT_AI_S3_READ_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_S3_READ_TIMEOUT_SECONDS"
DOCUMENT_AI_S3_UPLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_S3_UPLOAD_CAPABILITY_TTL_SECONDS"
)
DOCUMENT_AI_S3_DOWNLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_S3_DOWNLOAD_CAPABILITY_TTL_SECONDS"
)
DOCUMENT_AI_R2_ENDPOINT_ENV_VAR = "DOCUMENT_AI_R2_ENDPOINT"
DOCUMENT_AI_R2_BUCKET_ENV_VAR = "DOCUMENT_AI_R2_BUCKET"
DOCUMENT_AI_R2_ACCESS_KEY_ID_ENV_VAR = "DOCUMENT_AI_R2_ACCESS_KEY_ID"
DOCUMENT_AI_R2_SECRET_ACCESS_KEY_ENV_VAR = "DOCUMENT_AI_R2_SECRET_ACCESS_KEY"
DOCUMENT_AI_R2_CONNECT_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_R2_CONNECT_TIMEOUT_SECONDS"
DOCUMENT_AI_R2_READ_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_R2_READ_TIMEOUT_SECONDS"
DOCUMENT_AI_R2_UPLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_R2_UPLOAD_CAPABILITY_TTL_SECONDS"
)
DOCUMENT_AI_R2_DOWNLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_R2_DOWNLOAD_CAPABILITY_TTL_SECONDS"
)
DEFAULT_R2_TIMEOUT_SECONDS = 10
DEFAULT_R2_CAPABILITY_TTL_SECONDS = 15 * 60
DEFAULT_S3_TIMEOUT_SECONDS = 10
DEFAULT_S3_CAPABILITY_TTL_SECONDS = 15 * 60
DEFAULT_S3_SERVER_SIDE_ENCRYPTION = "AES256"
DOCUMENT_AI_WORKER_LEASE_SECONDS_ENV_VAR = "DOCUMENT_AI_WORKER_LEASE_SECONDS"
DEFAULT_DOCUMENT_AI_WORKER_LEASE_SECONDS = 60
DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS_ENV_VAR = "DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS"
DOCUMENT_AI_PROCESSING_MAX_RETRY_ELAPSED_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_PROCESSING_MAX_RETRY_ELAPSED_SECONDS"
)
DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE_ENV_VAR = "DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE"
DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS_ENV_VAR = "DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS"
DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS"
)
DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS"
)
DEFAULT_DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS = 3
DEFAULT_DOCUMENT_AI_PROCESSING_MAX_RETRY_ELAPSED_SECONDS = 900
DEFAULT_DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE = 25
DEFAULT_DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS = 5
DEFAULT_DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS = 5
DEFAULT_DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS = 15
DOCUMENT_AI_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
DOCUMENT_AI_OPENAI_MODEL_ENV_VAR = "DOCUMENT_AI_OPENAI_MODEL"
DOCUMENT_AI_OPENAI_EMBEDDING_MODEL_ENV_VAR = "DOCUMENT_AI_OPENAI_EMBEDDING_MODEL"
DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS"
DEFAULT_DOCUMENT_AI_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
APPROVED_DOCUMENT_AI_OPENAI_MODELS: frozenset[str] = frozenset({DEFAULT_DOCUMENT_AI_OPENAI_MODEL})
APPROVED_DOCUMENT_AI_OPENAI_EMBEDDING_MODELS: frozenset[str] = frozenset(
    {DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL}
)
DEFAULT_DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS = 60
DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR = (
    "DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS"
)
DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR = (
    "DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS"
)
DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR = (
    "DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS"
)
DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS = 5
DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS = 100
DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS = 2_000


@dataclass(frozen=True)
class DocumentAIDatabaseTransactionRetryConfig:
    """Represent bounded CockroachDB retry settings for Document AI transactions."""

    max_attempts: int
    backoff_base_ms: int
    backoff_max_ms: int


def get_document_ai_runtime_mode() -> Literal["development", "test", "production"]:
    """Return the explicit runtime mode used to select document providers."""

    value = os.getenv(DOCUMENT_AI_RUNTIME_MODE_ENV_VAR, "development").strip().lower()
    if value not in {"development", "test", "production"}:
        raise RuntimeError(
            f"{DOCUMENT_AI_RUNTIME_MODE_ENV_VAR} must be development, test, or production."
        )
    return cast(Literal["development", "test", "production"], value)


def get_document_ai_storage_provider() -> Literal["r2", "s3"]:
    """Return the only approved production source-artifact storage provider."""

    value = os.getenv(DOCUMENT_AI_STORAGE_PROVIDER_ENV_VAR, "s3").strip().lower()
    if value not in {"r2", "s3"}:
        raise RuntimeError(
            "Production document storage requires DOCUMENT_AI_STORAGE_PROVIDER=r2 or s3."
        )
    return cast(Literal["r2", "s3"], value)


def get_document_ai_aws_region() -> str | None:
    """Return the configured AWS region for the S3-backed adapter."""

    value = os.getenv(DOCUMENT_AI_AWS_REGION_ENV_VAR, "").strip()
    if value:
        return value
    value = os.getenv("AWS_REGION", "").strip()
    if value:
        return value
    value = os.getenv("AWS_DEFAULT_REGION", "").strip()
    return value or None


def get_document_ai_s3_bucket() -> str | None:
    """Return the configured Amazon S3 bucket."""

    value = os.getenv(DOCUMENT_AI_S3_BUCKET_ENV_VAR, "").strip()
    return value or None


def get_document_ai_s3_server_side_encryption() -> str | None:
    """Return the configured S3 server-side encryption mode, if any."""

    value = os.getenv(DOCUMENT_AI_S3_SERVER_SIDE_ENCRYPTION_ENV_VAR, "").strip()
    return value or None


def get_document_ai_s3_kms_key_id() -> str | None:
    """Return the configured KMS key id for S3, if any."""

    value = os.getenv(DOCUMENT_AI_S3_KMS_KEY_ID_ENV_VAR, "").strip()
    return value or None


def get_document_ai_s3_connect_timeout_seconds() -> int:
    """Return bounded S3 connection timeout."""

    return _get_positive_int_env(
        DOCUMENT_AI_S3_CONNECT_TIMEOUT_SECONDS_ENV_VAR, DEFAULT_S3_TIMEOUT_SECONDS
    )


def get_document_ai_s3_read_timeout_seconds() -> int:
    """Return bounded S3 operation timeout."""

    return _get_positive_int_env(
        DOCUMENT_AI_S3_READ_TIMEOUT_SECONDS_ENV_VAR, DEFAULT_S3_TIMEOUT_SECONDS
    )


def get_document_ai_s3_upload_capability_ttl_seconds() -> int:
    """Return the approved maximum S3 upload-capability lifetime."""

    return _get_positive_int_env(
        DOCUMENT_AI_S3_UPLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR, DEFAULT_S3_CAPABILITY_TTL_SECONDS
    )


def get_document_ai_s3_download_capability_ttl_seconds() -> int:
    """Return the approved maximum S3 download-capability lifetime."""

    return _get_positive_int_env(
        DOCUMENT_AI_S3_DOWNLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR,
        DEFAULT_S3_CAPABILITY_TTL_SECONDS,
    )


def validate_document_ai_s3_production_configuration() -> None:
    """Validate Milestone 17 Amazon S3 configuration for production."""

    if get_document_ai_runtime_mode() != "production":
        return
    if get_document_ai_storage_provider() != "s3":
        raise RuntimeError("Production document storage requires DOCUMENT_AI_STORAGE_PROVIDER=s3.")
    bucket = get_document_ai_s3_bucket()
    region = get_document_ai_aws_region()
    parsed_bucket = bucket or ""
    if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed_bucket):
        raise RuntimeError("Production S3 storage requires a valid DOCUMENT_AI_S3_BUCKET.")
    if not region:
        raise RuntimeError("Production S3 storage requires a valid AWS region.")
    get_document_ai_s3_connect_timeout_seconds()
    get_document_ai_s3_read_timeout_seconds()
    get_document_ai_s3_upload_capability_ttl_seconds()
    get_document_ai_s3_download_capability_ttl_seconds()
    _validate_document_ai_s3_multipart_configuration()


def get_document_ai_r2_endpoint() -> str | None:
    """Return the configured private R2 S3-compatible endpoint."""

    value = os.getenv(DOCUMENT_AI_R2_ENDPOINT_ENV_VAR, "").strip()
    return value or None


def get_document_ai_r2_bucket() -> str | None:
    """Return the configured private R2 bucket."""

    value = os.getenv(DOCUMENT_AI_R2_BUCKET_ENV_VAR, "").strip()
    return value or None


def get_document_ai_r2_access_key_id() -> str | None:
    """Return the R2 access key from the server-side configuration boundary."""

    value = os.getenv(DOCUMENT_AI_R2_ACCESS_KEY_ID_ENV_VAR, "").strip()
    return value or None


def get_document_ai_r2_secret_access_key() -> str | None:
    """Return the R2 secret key from the server-side configuration boundary."""

    value = os.getenv(DOCUMENT_AI_R2_SECRET_ACCESS_KEY_ENV_VAR, "").strip()
    return value or None


def _get_positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer.") from error
    if parsed <= 0 or parsed > 86_400:
        raise RuntimeError(f"{name} must be between 1 and 86400 seconds.")
    return parsed


def _get_non_negative_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a non-negative integer.") from error
    if parsed < 0 or parsed > 86_400_000:
        raise RuntimeError(f"{name} must be between 0 and 86400000 milliseconds.")
    return parsed


def get_document_ai_worker_lease_seconds() -> int:
    """Return a bounded durable worker lease; queue visibility is never a lease."""

    value = _get_positive_int_env(
        DOCUMENT_AI_WORKER_LEASE_SECONDS_ENV_VAR, DEFAULT_DOCUMENT_AI_WORKER_LEASE_SECONDS
    )
    if value > 3600:
        raise RuntimeError(
            f"{DOCUMENT_AI_WORKER_LEASE_SECONDS_ENV_VAR} must be at most 3600 seconds."
        )
    return value


def get_document_ai_openai_model() -> str:
    """Return the sole approved document-understanding model, never caller input."""

    model = os.getenv(DOCUMENT_AI_OPENAI_MODEL_ENV_VAR, DEFAULT_DOCUMENT_AI_OPENAI_MODEL).strip()
    if model not in APPROVED_DOCUMENT_AI_OPENAI_MODELS:
        raise RuntimeError("document_ai_openai_model_not_approved")
    return model


def get_document_ai_embedding_model() -> str:
    """Return the approved OpenAI embedding model, never caller input."""

    model = os.getenv(
        DOCUMENT_AI_OPENAI_EMBEDDING_MODEL_ENV_VAR, DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL
    ).strip()
    if model not in APPROVED_DOCUMENT_AI_OPENAI_EMBEDDING_MODELS:
        raise RuntimeError("document_ai_openai_embedding_model_not_approved")
    return model


def get_document_ai_openai_timeout_seconds() -> int:
    """Return a bounded provider timeout suitable for one leased worker attempt."""

    timeout = _get_positive_int_env(
        DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS,
    )
    if timeout > 600:
        raise RuntimeError(
            f"{DOCUMENT_AI_OPENAI_TIMEOUT_SECONDS_ENV_VAR} must be at most 600 seconds."
        )
    return timeout


def get_document_ai_openai_api_key() -> str | None:
    """Read the server-side provider credential without ever exposing it in models."""

    value = os.getenv(DOCUMENT_AI_OPENAI_API_KEY_ENV_VAR, "").strip()
    return value or None


def get_document_ai_embedding_api_key() -> str | None:
    """Embeddings use the same private OpenAI credential boundary."""

    return get_document_ai_openai_api_key()


def get_document_ai_processing_max_attempts() -> int:
    """Return the bounded per-work-item processing attempt budget."""

    value = _get_positive_int_env(
        DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS,
    )
    if value > 20:
        raise RuntimeError(f"{DOCUMENT_AI_PROCESSING_MAX_ATTEMPTS_ENV_VAR} must be at most 20.")
    return value


def get_document_ai_processing_max_retry_elapsed_seconds() -> int:
    """Return the bounded wall-clock retry budget for a work item."""

    return _get_positive_int_env(
        DOCUMENT_AI_PROCESSING_MAX_RETRY_ELAPSED_SECONDS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_PROCESSING_MAX_RETRY_ELAPSED_SECONDS,
    )


def get_document_ai_work_discovery_max_batch_size() -> int:
    """Return the bounded candidate-discovery batch size."""

    value = _get_positive_int_env(
        DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE_ENV_VAR,
        DEFAULT_DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE,
    )
    if value > 100:
        raise RuntimeError(
            f"{DOCUMENT_AI_WORK_DISCOVERY_MAX_BATCH_SIZE_ENV_VAR} must be at most 100."
        )
    return value


def get_document_ai_worker_poll_interval_seconds() -> int:
    """Return the bounded idle cadence between successful worker polls."""

    value = _get_positive_int_env(
        DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS,
    )
    if value > 3600:
        raise RuntimeError(
            f"{DOCUMENT_AI_WORKER_POLL_INTERVAL_SECONDS_ENV_VAR} must be at most 3600."
        )
    return value


def get_document_ai_worker_empty_queue_backoff_seconds() -> int:
    """Return the bounded sleep applied when discovery returns no work."""

    value = _get_positive_int_env(
        DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS,
    )
    if value > 3600:
        raise RuntimeError(
            f"{DOCUMENT_AI_WORKER_EMPTY_QUEUE_BACKOFF_SECONDS_ENV_VAR} must be at most 3600."
        )
    return value


def get_document_ai_worker_discovery_failure_backoff_seconds() -> int:
    """Return the bounded sleep applied after a discovery failure."""

    value = _get_positive_int_env(
        DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS,
    )
    if value > 3600:
        raise RuntimeError(
            f"{DOCUMENT_AI_WORKER_DISCOVERY_FAILURE_BACKOFF_SECONDS_ENV_VAR} must be at most 3600."
        )
    return value


def get_document_ai_database_transaction_max_attempts() -> int:
    """Return the bounded CockroachDB transaction retry attempt count."""

    value = _get_positive_int_env(
        DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS,
    )
    if value > 20:
        raise RuntimeError(
            f"{DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR} must be at most 20."
        )
    return value


def get_document_ai_database_transaction_backoff_base_ms() -> int:
    """Return the bounded initial backoff delay for a retryable transaction."""

    return _get_non_negative_int_env(
        DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS,
    )


def get_document_ai_database_transaction_backoff_max_ms() -> int:
    """Return the bounded maximum backoff delay for a retryable transaction."""

    value = _get_non_negative_int_env(
        DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR,
        DEFAULT_DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS,
    )
    base_value = get_document_ai_database_transaction_backoff_base_ms()
    if value < base_value:
        raise RuntimeError(
            f"{DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR} must be greater than or "
            f"equal to {DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR}."
        )
    return value


def get_document_ai_r2_connect_timeout_seconds() -> int:
    """Return bounded R2 connection timeout."""

    return _get_positive_int_env(
        DOCUMENT_AI_R2_CONNECT_TIMEOUT_SECONDS_ENV_VAR, DEFAULT_R2_TIMEOUT_SECONDS
    )


def get_document_ai_r2_read_timeout_seconds() -> int:
    """Return bounded R2 operation timeout."""

    return _get_positive_int_env(
        DOCUMENT_AI_R2_READ_TIMEOUT_SECONDS_ENV_VAR, DEFAULT_R2_TIMEOUT_SECONDS
    )


def get_document_ai_r2_upload_capability_ttl_seconds() -> int:
    """Return the approved maximum R2 upload-capability lifetime."""

    return _get_positive_int_env(
        DOCUMENT_AI_R2_UPLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR,
        DEFAULT_R2_CAPABILITY_TTL_SECONDS,
    )


def get_document_ai_r2_download_capability_ttl_seconds() -> int:
    """Return the approved maximum R2 download-capability lifetime."""

    return _get_positive_int_env(
        DOCUMENT_AI_R2_DOWNLOAD_CAPABILITY_TTL_SECONDS_ENV_VAR,
        DEFAULT_R2_CAPABILITY_TTL_SECONDS,
    )


def validate_document_ai_r2_production_configuration() -> None:
    """Validate Milestone 4 private-R2 configuration without AWS-region coupling."""

    if get_document_ai_runtime_mode() != "production":
        return
    get_document_ai_storage_provider()
    endpoint = get_document_ai_r2_endpoint()
    bucket = get_document_ai_r2_bucket()
    access_key_id = get_document_ai_r2_access_key_id()
    secret_access_key = get_document_ai_r2_secret_access_key()
    parsed_endpoint = urlparse(endpoint or "")
    if (
        not endpoint
        or parsed_endpoint.scheme != "https"
        or not parsed_endpoint.hostname
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise RuntimeError("Production R2 storage requires a valid HTTPS DOCUMENT_AI_R2_ENDPOINT.")
    if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("Production R2 storage requires a valid DOCUMENT_AI_R2_BUCKET.")
    if not access_key_id or not secret_access_key:
        raise RuntimeError(
            "Production R2 storage requires DOCUMENT_AI_R2_ACCESS_KEY_ID and secret."
        )
    get_document_ai_r2_connect_timeout_seconds()
    get_document_ai_r2_read_timeout_seconds()
    get_document_ai_r2_upload_capability_ttl_seconds()
    get_document_ai_r2_download_capability_ttl_seconds()


def validate_document_ai_production_configuration() -> None:
    """Validate every active production dependency without legacy provider coupling."""

    if get_document_ai_runtime_mode() != "production":
        return
    provider = get_document_ai_storage_provider()
    if provider == "s3":
        validate_document_ai_s3_production_configuration()
    else:
        validate_document_ai_r2_production_configuration()
    if not get_document_ai_openai_api_key():
        raise RuntimeError("Production Document AI requires an OpenAI API key.")
    get_document_ai_openai_model()
    get_document_ai_embedding_model()
    get_document_ai_openai_timeout_seconds()
    get_document_ai_worker_lease_seconds()
    get_document_ai_processing_max_attempts()
    get_document_ai_processing_max_retry_elapsed_seconds()
    get_document_ai_work_discovery_max_batch_size()
    get_document_ai_worker_poll_interval_seconds()
    get_document_ai_worker_empty_queue_backoff_seconds()
    get_document_ai_worker_discovery_failure_backoff_seconds()


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def is_allowed_upload_mime_type(content_type: str) -> bool:
    """Return whether MIME type is explicitly allowlisted for ingestion."""

    return is_supported_media_type(content_type)


def is_within_upload_size_limit(size_bytes: int) -> bool:
    """Return whether payload size is within deterministic configured limit."""

    return 0 < size_bytes <= MAX_UPLOAD_SIZE_BYTES


def is_valid_checksum_sha256(checksum_sha256: str) -> bool:
    """Return whether checksum string matches canonical lowercase SHA-256 hex."""

    return CHECKSUM_SHA256_PATTERN.fullmatch(checksum_sha256) is not None


def get_document_ai_s3_multipart_upload_threshold_bytes() -> int:
    """Return the deterministic boundary for multipart uploads."""

    return S3_MULTIPART_UPLOAD_THRESHOLD_BYTES


def get_document_ai_s3_multipart_upload_part_size_bytes() -> int:
    """Return the deterministic S3 multipart part size."""

    return S3_MULTIPART_UPLOAD_PART_SIZE_BYTES


def _validate_document_ai_s3_multipart_configuration() -> None:
    """Validate the multipart policy against current Amazon S3 limits."""

    part_size = get_document_ai_s3_multipart_upload_part_size_bytes()
    threshold = get_document_ai_s3_multipart_upload_threshold_bytes()
    if part_size < S3_MULTIPART_MIN_PART_SIZE_BYTES:
        raise RuntimeError("S3 multipart part size must be at least 5 MiB.")
    if part_size > S3_MULTIPART_MAX_PART_SIZE_BYTES:
        raise RuntimeError("S3 multipart part size must not exceed 5 GiB.")
    if threshold <= 0:
        raise RuntimeError("S3 multipart threshold must be positive.")
    if threshold > MAX_UPLOAD_SIZE_BYTES:
        raise RuntimeError("S3 multipart threshold must not exceed the upload limit.")
    if part_size > MAX_UPLOAD_SIZE_BYTES:
        raise RuntimeError("S3 multipart part size must not exceed the upload limit.")
    if MAX_UPLOAD_SIZE_BYTES > S3_MULTIPART_MAX_PART_SIZE_BYTES * S3_MULTIPART_MAX_PARTS:
        raise RuntimeError("S3 multipart policy cannot support the configured upload limit.")


def is_tenant_scoped_object_key(object_key: str, tenant_id: str) -> bool:
    """Return whether object key remains tenant-scoped by deterministic prefix."""

    return object_key.startswith(f"{tenant_id}/")


def get_storage_endpoint_url() -> str:
    """Return configured storage endpoint URL used for capability URLs."""

    configured = os.getenv("DOCUMENT_AI_STORAGE_ENDPOINT_URL")
    if configured is None:
        return DEFAULT_STORAGE_ENDPOINT_URL
    normalized = configured.strip()
    return normalized if normalized else DEFAULT_STORAGE_ENDPOINT_URL


def get_storage_encryption_required() -> bool:
    """Return whether encryption-at-rest intent must be enforced for capabilities."""

    return _read_bool_env(
        "DOCUMENT_AI_STORAGE_ENCRYPTION_REQUIRED",
        default=DEFAULT_STORAGE_ENCRYPTION_REQUIRED,
    )


def get_storage_signing_secret_env_var() -> str:
    """Return configured env-var name used to source storage signing secrets."""

    configured = os.getenv("DOCUMENT_AI_STORAGE_SIGNING_SECRET_ENV_VAR")
    if configured is None:
        return DEFAULT_STORAGE_SIGNING_SECRET_ENV_VAR
    normalized = configured.strip()
    return normalized if normalized else DEFAULT_STORAGE_SIGNING_SECRET_ENV_VAR
