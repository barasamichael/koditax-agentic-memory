"""Configuration utilities for persistent event-store runtime."""

from __future__ import annotations

import os
from pathlib import Path

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
EVENT_RETENTION_DAYS_ENV_VAR = "EVENT_RETENTION_DAYS"
EVENT_RETENTION_POLICY_CODE_ENV_VAR = "EVENT_RETENTION_POLICY_CODE"
DEFAULT_DB_NAME = "kodi_dev"
EVENT_RETENTION_DAYS = 3650
EVENT_RETENTION_POLICY_CODE = "event_store_default_retention"


def load_database_url() -> str | None:
    """Load DB URL from env, with deterministic .env fallback."""

    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value.strip()

    values = _read_env_file_values()
    raw_database_url = values.get(DATABASE_URL_ENV_VAR)
    if raw_database_url:
        return raw_database_url

    db_user = values.get(DB_USER_ENV_VAR)
    db_password = values.get(DB_PASSWORD_ENV_VAR)
    db_name = values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def _read_env_file_values() -> dict[str, str]:
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        values[normalized_key] = value.strip().strip("\"'")
    return values


def load_event_retention_policy() -> tuple[str, int]:
    """Load deterministic event retention policy code and retention window days."""

    values = _read_env_file_values()
    raw_policy_code = os.getenv(EVENT_RETENTION_POLICY_CODE_ENV_VAR) or values.get(
        EVENT_RETENTION_POLICY_CODE_ENV_VAR,
        EVENT_RETENTION_POLICY_CODE,
    )
    policy_code = (raw_policy_code or "").strip() or EVENT_RETENTION_POLICY_CODE

    raw_days = os.getenv(EVENT_RETENTION_DAYS_ENV_VAR) or values.get(
        EVENT_RETENTION_DAYS_ENV_VAR,
        str(EVENT_RETENTION_DAYS),
    )
    try:
        retention_days = int((raw_days or "").strip())
    except ValueError as error:
        raise ValueError("Event retention days must be a positive integer.") from error

    if retention_days <= 0:
        raise ValueError("Event retention days must be a positive integer.")

    return policy_code, retention_days
