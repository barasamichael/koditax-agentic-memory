"""Phase 15 orchestration scope-guard regressions."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app


@pytest.mark.parametrize("method", ("get", "post", "patch", "delete"))
def test_orchestration_scope_guard_fails_closed_consistently_across_methods(
    method: str,
) -> None:
    client = TestClient(create_app())
    request = getattr(client, method)
    first = request(
        "/v1/orchestration/unsupported-scope/private/path",
        headers={"X-Correlation-ID": "corr-orch-scope-guard-001"},
    )
    second = request(
        "/v1/orchestration/unsupported-scope/private/path",
        headers={"X-Correlation-ID": "corr-orch-scope-guard-001"},
    )

    assert first.status_code == 404
    assert second.status_code == 404
    first_detail = cast(dict[str, object], first.json()["detail"])
    second_detail = cast(dict[str, object], second.json()["detail"])
    assert first_detail["error_code"] == "unsupported_orchestration_scope"
    assert first_detail["reason"] == "unsupported_orchestration_scope"
    assert first_detail["reason_code"] == "unsupported_orchestration_scope"
    assert first_detail["correlation_id"] == "corr-orch-scope-guard-001"
    assert first_detail["trace_id"] == "corr-orch-scope-guard-001"
    assert first_detail == second_detail
