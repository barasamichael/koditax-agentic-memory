"""Load tax-core persistence configuration from environment."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

DATABASE_URL_ENV_VAR = "DATABASE_URL"
RETENTION_DAYS_ENV_VAR = "TAX_CORE_RETENTION_DAYS"
COMPLIANCE_LOCK_DAYS_ENV_VAR = "TAX_CORE_COMPLIANCE_LOCK_DAYS"

DEFAULT_RETENTION_DAYS = 365
DEFAULT_COMPLIANCE_LOCK_DAYS = 30


@dataclass(frozen=True)
class TaxCorePersistenceConfig:
    """Represent persistence settings required by tax-core materialization."""

    database_url: str
    retention_days: int
    compliance_lock_days: int


def load_tax_core_persistence_config() -> TaxCorePersistenceConfig:
    """Load tax-core persistence settings from environment and .env fallback."""

    database_url = _read_environment_value(DATABASE_URL_ENV_VAR)
    if database_url is None or not database_url.strip():
        raise RuntimeError("DATABASE_URL is required for tax-core materialization.")

    retention_days = _parse_positive_int(
        raw_value=_read_environment_value(RETENTION_DAYS_ENV_VAR),
        default_value=DEFAULT_RETENTION_DAYS,
        env_var=RETENTION_DAYS_ENV_VAR,
    )
    compliance_lock_days = _parse_positive_int(
        raw_value=_read_environment_value(COMPLIANCE_LOCK_DAYS_ENV_VAR),
        default_value=DEFAULT_COMPLIANCE_LOCK_DAYS,
        env_var=COMPLIANCE_LOCK_DAYS_ENV_VAR,
    )
    return TaxCorePersistenceConfig(
        database_url=database_url,
        retention_days=retention_days,
        compliance_lock_days=compliance_lock_days,
    )


def _parse_positive_int(raw_value: str | None, default_value: int, env_var: str) -> int:
    if raw_value is None or not raw_value.strip():
        return default_value

    try:
        parsed_value = int(raw_value.strip())
    except ValueError as error:
        raise RuntimeError(f"{env_var} must be a positive integer.") from error

    if parsed_value <= 0:
        raise RuntimeError(f"{env_var} must be a positive integer.")

    return parsed_value


def _read_environment_value(key: str) -> str | None:
    env_value = os.getenv(key)
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
        if not line.startswith(f"{key}="):
            continue
        parsed_value = line.split("=", maxsplit=1)[1].strip().strip("\"'")
        return parsed_value or None

    return None
