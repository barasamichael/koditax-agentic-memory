"""Deterministic storage security controls."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from services.document_ai.app.redaction import redact_sensitive_fields

REQUIRED_ENCRYPTION_HEADERS: dict[str, str] = {
    "x-kodi-encryption-at-rest": "required",
    "x-kodi-encryption-algorithm": "AES256",
}


class SecurityPolicyViolationError(ValueError):
    """Represent deterministic security-policy violation."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = redact_sensitive_fields(details if details is not None else {})


@dataclass(frozen=True)
class StorageSecurityControls:
    """Represent runtime storage security configuration snapshot."""

    endpoint_url: str
    encryption_required: bool
    signing_secret_env_var: str
    provided_secret_literal: str | None = None


def validate_storage_security_controls(controls: StorageSecurityControls) -> None:
    """Validate deterministic storage security controls used by adapter operations."""

    parsed = urlparse(controls.endpoint_url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    is_local_development_endpoint = scheme == "http" and hostname in {
        "127.0.0.1",
        "localhost",
        "testserver",
    }
    if scheme != "https" and not is_local_development_endpoint:
        raise SecurityPolicyViolationError(
            reason="storage_endpoint_must_use_https",
            message="Storage endpoint URL must use HTTPS.",
            details={"storage_endpoint_url": controls.endpoint_url},
        )
    if not controls.encryption_required:
        raise SecurityPolicyViolationError(
            reason="storage_encryption_at_rest_required",
            message="Storage capability operations require encryption-at-rest controls.",
            details={"encryption_required": controls.encryption_required},
        )
    if not controls.signing_secret_env_var.strip():
        raise SecurityPolicyViolationError(
            reason="storage_secret_env_var_missing",
            message="Storage signing secret env-var name must be configured.",
            details={"signing_secret_env_var": controls.signing_secret_env_var},
        )
    if controls.provided_secret_literal is not None:
        raise SecurityPolicyViolationError(
            reason="storage_secret_literal_not_allowed",
            message="Storage signing secret literal is not allowed; use environment source.",
            details={
                "provided_secret_literal": controls.provided_secret_literal,
                "signing_secret_env_var": controls.signing_secret_env_var,
            },
        )
