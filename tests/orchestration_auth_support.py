"""Trusted auth-context fixtures for orchestration route tests."""

from __future__ import annotations

import json
from uuid import uuid4
from uuid import uuid5
from uuid import NAMESPACE_URL


def orchestration_auth_headers(
    *,
    user_reference: str = "orchestration-test-user",
    tenant_id: str = "pilot_tenant_alpha",
) -> dict[str, str]:
    """Return a valid IndividualTaxpayer context for protected prompt routes."""

    return {
        "X-Auth-Context": json.dumps(
            {
                "schema_version": "1.0.0",
                "user_id": str(uuid5(NAMESPACE_URL, user_reference)),
                "tenant_id": tenant_id,
                "role": "IndividualTaxpayer",
                "session_id": str(uuid4()),
                "delegation_context": {
                    "is_delegated": False,
                    "principal_user_id": None,
                    "delegate_user_id": None,
                    "delegation_id": None,
                    "granted_at": None,
                    "revoked_at": None,
                },
            },
            sort_keys=True,
        )
    }


def orchestration_test_user_id(user_reference: str) -> str:
    """Return the deterministic UUID encoded by ``orchestration_auth_headers``."""

    return str(uuid5(NAMESPACE_URL, user_reference))
