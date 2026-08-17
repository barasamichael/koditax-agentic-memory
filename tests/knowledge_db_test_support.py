"""Shared DB-backed support for governed knowledge service tests."""

from __future__ import annotations

import os
import json
from typing import cast
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import FastAPI
import psycopg
from psycopg.abc import Query

from services.knowledge.app.main import create_app
from services.knowledge.app.embeddings import KnowledgeEmbeddingProvider
from services.knowledge.app.repository import KnowledgeRepository

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"
KNOWLEDGE_MIGRATION_FILES = (
    Path("database/migrations/0017_knowledge_persistent_catalog_baseline.sql"),
    Path("database/migrations/0018_knowledge_hybrid_retrieval_embeddings.sql"),
)


def _string_list() -> list[str]:
    return []


@dataclass
class KnowledgeRuntimeHarness:
    """Typed runtime harness for DB-backed knowledge service tests."""

    app: FastAPI
    connection: psycopg.Connection
    database_url: str
    job_ids: list[str] = field(default_factory=_string_list)
    document_ids: list[str] = field(default_factory=_string_list)
    user_ids: list[str] = field(default_factory=_string_list)


def load_database_url() -> str | None:
    """Load the DB URL from the environment or local .env file."""

    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    env_values = _read_env_values()
    direct_value = env_values.get(DATABASE_URL_ENV_VAR)
    if direct_value:
        return direct_value

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def ensure_knowledge_migration_applied(*, database_url: str) -> None:
    """Ensure the governed knowledge baseline migration exists for DB-backed tests."""

    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.knowledge_chunk_embeddings')")
                row = cursor.fetchone()
                if row is not None and row[0] is not None:
                    return
                for migration_file in KNOWLEDGE_MIGRATION_FILES:
                    sql_text = migration_file.read_text(encoding="utf-8")
                    cursor.execute(cast(Query, sql_text))
    except OSError as error:
        pytest.skip(f"Knowledge migration file is unreadable: {error}")
    except psycopg.Error:
        pytest.skip("Knowledge migration could not be applied for DB-backed tests.")


def create_runtime_harness(
    *,
    connection: psycopg.Connection,
    embedding_provider: KnowledgeEmbeddingProvider | None = None,
) -> KnowledgeRuntimeHarness:
    """Build a typed runtime harness around the governed knowledge app."""

    database_url = load_database_url()
    assert database_url is not None
    repository = KnowledgeRepository(
        database_url=database_url,
        embedding_provider=embedding_provider,
    )
    return KnowledgeRuntimeHarness(
        app=create_app(repository=repository),
        connection=connection,
        database_url=database_url,
    )


def require_object_dict(value: object) -> dict[str, object]:
    """Return a strongly typed object mapping from an arbitrary runtime value."""

    assert isinstance(value, dict)
    normalized: dict[str, object] = {}
    raw_mapping = cast(dict[object, object], value)
    for key, item in raw_mapping.items():
        assert isinstance(key, str)
        normalized[key] = item
    return normalized


def require_int(value: object) -> int:
    """Return an `int` from an arbitrary runtime value."""

    assert isinstance(value, int)
    assert not isinstance(value, bool)
    return value


def build_admin_auth_headers(*, user_id: str | UUID | None = None) -> dict[str, str]:
    """Build deterministic internal admin auth headers for knowledge management tests."""

    normalized_user_id = str(user_id or uuid4())
    return {
        "X-Auth-Context": json.dumps(
            {
                "schema_version": "1.0.0",
                "user_id": normalized_user_id,
                "tenant_id": "default_tenant",
                "role": "Administrator",
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
    }


def _read_env_values() -> dict[str, str]:
    env_file = Path(".env")
    if not env_file.exists():
        return {}
    try:
        raw_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")
    return values
