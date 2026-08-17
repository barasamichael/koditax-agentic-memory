"""Deterministic reports service runtime configuration."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime

REPORTS_SERVICE_NAME = "reports"
REPORTS_SERVICE_VERSION_ENV_VAR = "REPORTS_SERVICE_VERSION"
DEFAULT_REPORTS_SERVICE_VERSION = "1.0.0"
DEFAULT_REPORT_DOWNLOAD_TTL_SECONDS = 900
DEFAULT_REPORT_REFERENCE_TIME = "2026-01-01T00:00:00+00:00"


def get_reports_service_version() -> str:
    """Return deterministic reports service version string."""

    raw_value = os.getenv(REPORTS_SERVICE_VERSION_ENV_VAR, DEFAULT_REPORTS_SERVICE_VERSION)
    normalized_value = raw_value.strip()
    if normalized_value:
        return normalized_value
    return DEFAULT_REPORTS_SERVICE_VERSION


def get_report_download_ttl_seconds() -> int:
    """Return report download capability TTL policy seconds."""

    raw_value = os.getenv("REPORTS_DOWNLOAD_CAPABILITY_TTL_SECONDS", "").strip()
    if raw_value == "":
        return DEFAULT_REPORT_DOWNLOAD_TTL_SECONDS
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_REPORT_DOWNLOAD_TTL_SECONDS
    return parsed if parsed > 0 else DEFAULT_REPORT_DOWNLOAD_TTL_SECONDS


def get_report_reference_time() -> datetime:
    """Return deterministic report reference time for expiry policy checks."""

    raw_value = os.getenv("REPORTS_REFERENCE_TIME", DEFAULT_REPORT_REFERENCE_TIME).strip()
    parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
