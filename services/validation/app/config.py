"""Configuration helpers for the validation runtime boundary."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

SERVICE_NAME = "validation"
SERVICE_VERSION = "0.2.0"
DATABASE_URL_ENV_VAR = "DATABASE_URL"
VALIDATION_RUNTIME_MODE_ENV_VAR = "VALIDATION_RUNTIME_MODE"
VALIDATION_INTERNAL_API_KEY_ENV_VAR = "VALIDATION_INTERNAL_API_KEY"
VALIDATION_INTERNAL_API_KEY_HEADER = "X-Validation-Internal-Key"
VALIDATION_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:5174",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


@dataclass(frozen=True)
class ValidationConfig:
    """Represent resolved validation runtime configuration."""

    service_name: str
    service_version: str
    runtime_mode: str
    database_url: str | None
    allowed_origins: tuple[str, ...]
    internal_api_key: str | None


def load_validation_config() -> ValidationConfig:
    """Load deterministic validation runtime configuration from env."""

    runtime_mode_raw = os.getenv(VALIDATION_RUNTIME_MODE_ENV_VAR, "development").strip().lower()
    runtime_mode = (
        runtime_mode_raw if runtime_mode_raw in {"development", "production"} else "development"
    )

    return ValidationConfig(
        service_name=SERVICE_NAME,
        service_version=SERVICE_VERSION,
        runtime_mode=runtime_mode,
        database_url=_load_database_url(),
        allowed_origins=VALIDATION_DEFAULT_CORS_ORIGINS,
        internal_api_key=_load_internal_api_key(),
    )


def load_validation_database_url() -> str | None:
    """Expose database-url loading for tests and persistence helpers."""

    return _load_database_url()


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    env_file = Path(".env")
    if not env_file.exists():
        return None

    try:
        env_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in env_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(f"{DATABASE_URL_ENV_VAR}="):
            continue
        parsed_value = line.split("=", maxsplit=1)[1].strip().strip("\"'")
        return parsed_value or None

    return None


def _load_internal_api_key() -> str | None:
    env_value = os.getenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR)
    if env_value is None:
        return None
    normalized = env_value.strip()
    return normalized or None
