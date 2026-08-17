"""Storage service runtime configuration."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime

STORAGE_SERVICE_NAME = "storage"
DEFAULT_STORAGE_SERVICE_VERSION = "1.0.0"
DEFAULT_UPLOAD_CAPABILITY_TTL_SECONDS = 900
DEFAULT_DOWNLOAD_CAPABILITY_TTL_SECONDS = 900
DEFAULT_METADATA_CAPABILITY_TTL_SECONDS = 900
DEFAULT_CAPABILITY_BASE_TIME = "2026-01-01T00:00:00+00:00"


def get_storage_service_version() -> str:
    """Return deterministic storage service version string."""

    candidate = os.getenv("STORAGE_SERVICE_VERSION", "").strip()
    return candidate or DEFAULT_STORAGE_SERVICE_VERSION


def get_upload_capability_ttl_seconds() -> int:
    """Return upload capability TTL seconds."""

    return _int_from_env(
        "STORAGE_UPLOAD_CAPABILITY_TTL_SECONDS", DEFAULT_UPLOAD_CAPABILITY_TTL_SECONDS
    )


def get_download_capability_ttl_seconds() -> int:
    """Return download capability TTL seconds."""

    return _int_from_env(
        "STORAGE_DOWNLOAD_CAPABILITY_TTL_SECONDS",
        DEFAULT_DOWNLOAD_CAPABILITY_TTL_SECONDS,
    )


def get_metadata_capability_ttl_seconds() -> int:
    """Return metadata capability TTL seconds."""

    return _int_from_env(
        "STORAGE_METADATA_CAPABILITY_TTL_SECONDS",
        DEFAULT_METADATA_CAPABILITY_TTL_SECONDS,
    )


def get_capability_base_time() -> datetime:
    """Return deterministic base timestamp used for capability expiry."""

    raw_value = os.getenv("STORAGE_CAPABILITY_BASE_TIME", DEFAULT_CAPABILITY_BASE_TIME).strip()
    parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if raw_value == "":
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
