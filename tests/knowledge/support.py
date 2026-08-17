from __future__ import annotations

import json
from typing import Any
from typing import cast


def require_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def require_object_list(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    raw_items = cast(list[object], value)
    return [require_object(item) for item in raw_items]


def stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
    }


def admin_auth_headers(seed: str = "knowledge-admin") -> dict[str, str]:
    return role_auth_headers(role="Administrator", seed=seed)


def role_auth_headers(*, role: str, seed: str = "knowledge-role") -> dict[str, str]:
    headers = stable_headers(seed)
    headers["X-Auth-Context"] = json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": "123e4567-e89b-12d3-a456-426614174999",
            "tenant_id": "default_tenant",
            "role": role,
            "session_id": "11111111-2222-3333-4444-555555555555",
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
    return headers


def response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
