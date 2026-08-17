"""Shared test-only runtime configuration."""

from __future__ import annotations

import os
from collections.abc import Callable

from fastapi.testclient import TestClient

from services.orchestration.app import main as orchestration_main
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.conversation_turn_test_support import DeterministicTestConversationTurnResolver

os.environ.setdefault("DOCUMENT_AI_RUNTIME_MODE", "test")
os.environ.setdefault("DOCUMENT_AI_PERSISTENCE_MODE", "in_memory")
os.environ.setdefault("ORCHESTRATION_CONVERSATION_STATE_PERSISTENCE_MODE", "in_memory")


_original_testclient_request: Callable[..., object] = TestClient.request
_original_build_default_turn_resolver = orchestration_main.build_default_turn_resolver
_LIVE_OPENAI_ENV_VAR = "KODI_LIVE_OPENAI_TEST"


def _inject_default_orchestration_auth(
    self: TestClient,
    method: str,
    url: str,
    *args: object,
    **kwargs: object,
) -> object:
    if os.getenv(_LIVE_OPENAI_ENV_VAR) == "1":
        return _original_testclient_request(self, method, url, *args, **kwargs)
    app = getattr(self, "app", None)
    app_title = getattr(app, "title", None)
    headers = dict(kwargs.get("headers") or {})
    header_names = {str(key).lower() for key in headers}
    if (
        app_title == "orchestration"
        and str(url).startswith("/v1/orchestration/prompt/execute")
        and "x-test-allow-client-user-id" not in header_names
        and isinstance(kwargs.get("json"), dict)
    ):
        body = dict(kwargs["json"])
        body.pop("user_id", None)
        kwargs["json"] = body
    if (
        app_title == "orchestration"
        and "x-auth-context" not in header_names
        and "x-test-anonymous" not in header_names
    ):
        headers = {**orchestration_auth_headers(), **headers}
        kwargs["headers"] = headers
    return _original_testclient_request(self, method, url, *args, **kwargs)


TestClient.request = _inject_default_orchestration_auth  # type: ignore[assignment]


def _build_deterministic_test_turn_resolver(
    *args: object,
    **kwargs: object,
) -> DeterministicTestConversationTurnResolver:
    if os.getenv(_LIVE_OPENAI_ENV_VAR) == "1":
        return _original_build_default_turn_resolver(*args, **kwargs)
    return DeterministicTestConversationTurnResolver()


orchestration_main.build_default_turn_resolver = _build_deterministic_test_turn_resolver  # type: ignore[assignment]
