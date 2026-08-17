"""Deterministic reports access-context and authorization helpers."""

from __future__ import annotations

import re
from uuid import UUID
from dataclasses import dataclass

from fastapi import Request

REPORT_OWNER_HEADER = "X-User-ID"
REPORT_TENANT_HEADER = "X-Tenant-ID"
DEFAULT_REPORT_OWNER_USER_ID = "anonymous_user"
DEFAULT_REPORT_TENANT_ID = "default_tenant"
REPORT_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class ReportAccessContext:
    """Represent caller access identity for report ownership boundaries."""

    owner_user_id: str
    tenant_id: str


def resolve_report_access_context(*, request: Request) -> ReportAccessContext:
    """Resolve deterministic owner/tenant access context from request headers."""

    owner_user_id = request.headers.get(REPORT_OWNER_HEADER, "").strip()
    tenant_id = request.headers.get(REPORT_TENANT_HEADER, "").strip()
    return ReportAccessContext(
        owner_user_id=owner_user_id or DEFAULT_REPORT_OWNER_USER_ID,
        tenant_id=tenant_id or DEFAULT_REPORT_TENANT_ID,
    )


def is_valid_report_id(report_id: str) -> bool:
    """Return whether provided report_id matches deterministic internal format."""

    normalized = report_id.strip().lower()
    if REPORT_ID_UUID_PATTERN.fullmatch(normalized) is None:
        return False
    try:
        UUID(normalized)
    except ValueError:
        return False
    return True
