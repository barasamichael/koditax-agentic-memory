"""Focused CockroachDB persistence support tests for auth."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime

import pytest
import psycopg
from fastapi.testclient import TestClient

from services.auth.app import config as auth_config
from services.auth.app import oauth_flow as oauth_flow_module
from services.auth.app import registration as registration_module
from services.auth.app import oauth_linking as oauth_linking_module
from services.auth.app import session_issuance as session_module
from services.auth.app import persistence_support as persistence_support
from services.auth.app.main import create_app


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = list(rows)
        self.executed_sql: list[str] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def execute(self, sql: str, parameters: object = None) -> None:
        del parameters
        self.executed_sql.append(sql)

    def fetchone(self) -> tuple[object, ...] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)


class _FakeConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(list(self._rows))

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("runtime_mode", ["production", "hackathon"])
def test_auth_runtime_requires_persistence_in_hackathon_and_production(
    monkeypatch: pytest.MonkeyPatch,
    runtime_mode: str,
) -> None:
    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", runtime_mode)
    assert persistence_support.auth_runtime_requires_persistence() is True


def test_validate_auth_database_connection_accepts_kodi_dev_cockroach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ("CockroachDB CCL v26.2.5",),
        ("kodi_dev",),
        ("auth_user",),
        (datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),),
    ]
    fake_connection = _FakeConnection(rows=rows)
    monkeypatch.setattr(
        persistence_support,
        "open_auth_database_connection",
        lambda database_url: fake_connection,
    )

    result = persistence_support.validate_auth_database_connection(
        "postgresql://example.invalid/kodi_dev",
    )

    assert result.ready is True
    assert result.reason == "ready"
    assert result.current_database == "kodi_dev"
    assert result.engine and "CockroachDB" in result.engine
    assert result.current_user == "auth_user"
    assert result.current_timestamp == datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
    assert "example.invalid" not in repr(result)


@pytest.mark.parametrize(
    ("rows", "expected_reason"),
    [
        (
            [
                ("CockroachDB CCL v26.2.5",),
                ("other_db",),
                ("auth_user",),
                (datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),),
            ],
            "wrong_database",
        ),
        (
            [
                ("PostgreSQL 16.3",),
                ("kodi_dev",),
                ("auth_user",),
                (datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),),
            ],
            "wrong_database_engine",
        ),
        (
            [
                ("CockroachDB CCL v26.2.5",),
                ("kodi_dev",),
                ("",),
                (datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),),
            ],
            "database_validation_failed",
        ),
        (
            [
                ("CockroachDB CCL v26.2.5",),
                ("kodi_dev",),
                ("auth_user",),
                (None,),
            ],
            "database_validation_failed",
        ),
    ],
)
def test_validate_auth_database_connection_rejects_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
    expected_reason: str,
) -> None:
    fake_connection = _FakeConnection(rows=rows)
    monkeypatch.setattr(
        persistence_support,
        "open_auth_database_connection",
        lambda database_url: fake_connection,
    )

    result = persistence_support.validate_auth_database_connection(
        "postgresql://example.invalid/kodi_dev",
    )

    assert result.ready is False
    assert result.reason == expected_reason
    assert "postgresql://example.invalid/kodi_dev" not in repr(result)


def test_validate_auth_database_connection_sanitizes_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked_url = "postgresql://auth_user:super-secret@db.example.invalid/kodi_dev"

    def _raise(_: str) -> _FakeConnection:
        raise psycopg.OperationalError(
            f"could not connect using {leaked_url}"
        )

    monkeypatch.setattr(
        persistence_support,
        "open_auth_database_connection",
        _raise,
    )

    result = persistence_support.validate_auth_database_connection(leaked_url)

    assert result.ready is False
    assert result.reason == "database_unreachable"
    assert leaked_url not in repr(result)
    assert "super-secret" not in repr(result)


def test_auth_database_transaction_commits_rolls_back_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_connection = _FakeConnection(rows=[])
    monkeypatch.setattr(
        persistence_support,
        "open_auth_database_connection",
        lambda database_url: fake_connection,
    )

    with persistence_support.auth_database_transaction(
        "postgresql://example.invalid/kodi_dev",
    ) as connection:
        assert connection is fake_connection

    assert fake_connection.commit_calls == 1
    assert fake_connection.rollback_calls == 0
    assert fake_connection.close_calls == 1

    fake_failure_connection = _FakeConnection(rows=[])
    monkeypatch.setattr(
        persistence_support,
        "open_auth_database_connection",
        lambda database_url: fake_failure_connection,
    )

    with pytest.raises(RuntimeError):
        with persistence_support.auth_database_transaction(
            "postgresql://example.invalid/kodi_dev",
        ):
            raise RuntimeError("boom")

    assert fake_failure_connection.commit_calls == 0
    assert fake_failure_connection.rollback_calls == 1
    assert fake_failure_connection.close_calls == 1


@pytest.mark.parametrize(
    ("module", "builder_name", "expected_type_name"),
    [
        (
            registration_module,
            "build_default_registration_store",
            "InMemoryRegistrationStore",
        ),
        (
            session_module,
            "build_default_session_issuance_store",
            "InMemorySessionIssuanceStore",
        ),
    ],
)
def test_explicit_in_memory_mode_remains_available(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    builder_name: str,
    expected_type_name: str,
) -> None:
    monkeypatch.setattr(module, "auth_runtime_requires_persistence", lambda: False)

    store = getattr(module, builder_name)()

    assert type(store).__name__ == expected_type_name


@pytest.mark.parametrize(
    ("module", "builder_name", "expected_unavailable_type"),
    [
        (
            registration_module,
            "build_default_registration_store",
            "UnavailableRegistrationStore",
        ),
        (
            session_module,
            "build_default_session_issuance_store",
            "UnavailableSessionIssuanceStore",
        ),
    ],
)
@pytest.mark.parametrize("database_url", [None, ""])
def test_persistent_mode_requires_non_blank_database_url(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    builder_name: str,
    expected_unavailable_type: str,
    database_url: str | None,
) -> None:
    monkeypatch.setattr(module, "auth_runtime_requires_persistence", lambda: True)
    monkeypatch.setattr(module, "load_auth_database_url", lambda: database_url)

    store = getattr(module, builder_name)()

    assert type(store).__name__ == expected_unavailable_type
    assert "memory" not in type(store).__name__.lower()


@pytest.mark.parametrize(
    ("module", "builder_name", "expected_unavailable_type"),
    [
        (
            registration_module,
            "build_default_registration_store",
            "UnavailableRegistrationStore",
        ),
        (
            session_module,
            "build_default_session_issuance_store",
            "UnavailableSessionIssuanceStore",
        ),
    ],
)
def test_persistent_mode_does_not_fall_back_to_memory_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    builder_name: str,
    expected_unavailable_type: str,
) -> None:
    monkeypatch.setattr(module, "auth_runtime_requires_persistence", lambda: True)
    monkeypatch.setattr(module, "load_auth_database_url", lambda: "postgresql://example.invalid/kodi_dev")
    monkeypatch.setattr(
        module,
        "validate_auth_database_connection",
        lambda database_url: persistence_support.AuthDatabaseValidationResult(
            ready=False,
            reason="database_unreachable",
        ),
    )

    store = getattr(module, builder_name)()

    assert type(store).__name__ == expected_unavailable_type
    assert "memory" not in type(store).__name__.lower()


def test_oauth_default_stores_follow_runtime_persistence_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
        oauth_flow_module.reset_default_oauth_state_store()
        oauth_linking_module.reset_default_oauth_identity_linking_store()
        assert type(oauth_flow_module.get_default_oauth_state_store()).__name__ == (
            "InMemoryOAuthStateStore"
        )
        assert type(oauth_linking_module.get_default_oauth_identity_linking_store()).__name__ == (
            "InMemoryOAuthIdentityLinkingStore"
        )

        monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "production")
        oauth_flow_module.reset_default_oauth_state_store()
        oauth_linking_module.reset_default_oauth_identity_linking_store()
        assert type(oauth_flow_module.get_default_oauth_state_store()).__name__ == (
            "UnavailableOAuthStateStore"
        )
        assert type(oauth_linking_module.get_default_oauth_identity_linking_store()).__name__ == (
            "UnavailableOAuthIdentityLinkingStore"
        )
    finally:
        monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
        oauth_flow_module.reset_default_oauth_state_store()
        oauth_linking_module.reset_default_oauth_identity_linking_store()


def test_oauth_start_route_fails_closed_when_default_persistence_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "production")
        monkeypatch.setenv(
            auth_config.AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR,
            "https://accounts.google.com",
        )
        monkeypatch.setenv(
            auth_config.AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR,
            "https://kodi.example.com/v1/auth/oauth/google/callback",
        )
        monkeypatch.setenv(
            auth_config.AUTH_OAUTH_REQUIRED_SCOPES_ENV_VAR,
            "openid,email",
        )
        monkeypatch.setenv(
            auth_config.AUTH_OAUTH_STATE_TTL_SECONDS_ENV_VAR,
            "300",
        )
        monkeypatch.setenv(
            auth_config.AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR,
            json.dumps(
                [
                    {
                        "provider_id": "google",
                        "issuer": "https://accounts.google.com",
                        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                        "token_endpoint": "https://oauth2.googleapis.com/token",
                        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                        "client_id": "google-client-id",
                        "client_secret_ref": "env:AUTH_OAUTH_SECRET_GOOGLE",
                        "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                        "scopes": ["openid", "email", "profile"],
                        "enabled": True,
                    }
                ]
            ),
        )
        oauth_flow_module.reset_default_oauth_state_store()
        oauth_linking_module.reset_default_oauth_identity_linking_store()

        app = create_app()
        with TestClient(app) as client:
            response = client.post(
                "/v1/auth/oauth/google/start",
                headers={"X-Correlation-ID": "oauth-persistence-unavailable-corr"},
                json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback"},
            )

        error = response.json()["detail"]
        assert response.status_code == 503
        assert error["error_code"] == "oauth_state_persistence_unavailable"
        assert error["reason"] == "oauth_state_persistence_unavailable"
    finally:
        monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
        oauth_flow_module.reset_default_oauth_state_store()
        oauth_linking_module.reset_default_oauth_identity_linking_store()
