"""Deterministic OAuth JIT provisioning guardrails and conflict handling."""

from __future__ import annotations

import os
from uuid import UUID
from uuid import uuid4
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Callable

from services.auth.app.registration import build_password_hash
from services.auth.app.registration import RegistrationConflictError
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.oauth_linking import OAuthIdentityLinkingError
from services.auth.app.oauth_linking import OAuthIdentityLinkingStoreProtocol

DEFAULT_TENANT_ID = "default_tenant"
AUTH_OAUTH_JIT_PROVISIONING_ENABLED_ENV_VAR = "AUTH_OAUTH_JIT_PROVISIONING_ENABLED"
AUTH_OAUTH_JIT_ELIGIBLE_PROVIDERS_ENV_VAR = "AUTH_OAUTH_JIT_ELIGIBLE_PROVIDERS"
DEFAULT_AUTH_OAUTH_JIT_PROVISIONING_ENABLED = False
DEFAULT_AUTH_OAUTH_JIT_ELIGIBLE_PROVIDERS: tuple[str, ...] = ()
DEFAULT_AUTH_OAUTH_JIT_REQUIRED_CLAIMS: frozenset[str] = frozenset({"sub", "email"})
DEFAULT_AUTH_OAUTH_JIT_ROLE = "IndividualTaxpayer"


@dataclass(frozen=True)
class OAuthJitProvisioningPolicy:
    """Represent deterministic JIT provisioning eligibility policy."""

    enabled: bool
    eligible_providers: frozenset[str]
    required_claims: frozenset[str]
    default_role: str


@dataclass(frozen=True)
class OAuthJitProvisioningResult:
    """Represent deterministic OAuth JIT provisioning outcome."""

    provider_id: str
    provider_subject: str
    user_id: UUID
    tenant_id: str
    role: str
    provisioning_status: str


class OAuthJitProvisioningError(ValueError):
    """Represent deterministic OAuth JIT provisioning denial outcomes."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


def get_default_oauth_jit_provisioning_policy() -> OAuthJitProvisioningPolicy:
    """Return deterministic OAuth JIT provisioning policy from environment."""

    enabled = _read_bool_env(
        env_var=AUTH_OAUTH_JIT_PROVISIONING_ENABLED_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_JIT_PROVISIONING_ENABLED,
    )
    eligible_providers = _read_csv_lower_set(
        env_var=AUTH_OAUTH_JIT_ELIGIBLE_PROVIDERS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_JIT_ELIGIBLE_PROVIDERS,
    )
    return OAuthJitProvisioningPolicy(
        enabled=enabled,
        eligible_providers=eligible_providers,
        required_claims=DEFAULT_AUTH_OAUTH_JIT_REQUIRED_CLAIMS,
        default_role=DEFAULT_AUTH_OAUTH_JIT_ROLE,
    )


def provision_oauth_identity_if_eligible(
    *,
    provider_id: str,
    validated_claims: Mapping[str, object],
    tenant_id: str,
    policy: OAuthJitProvisioningPolicy,
    registration_store: RegistrationStoreProtocol,
    linking_store: OAuthIdentityLinkingStoreProtocol,
    now_provider: Callable[[], datetime] | None = None,
) -> OAuthJitProvisioningResult:
    """Provision first-time OAuth identity deterministically when policy allows."""

    normalized_provider_id = provider_id.strip().lower()
    normalized_tenant_id = _normalize_tenant_id(tenant_id=tenant_id)
    if normalized_tenant_id != DEFAULT_TENANT_ID:
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_tenant_resolution_failed",
            message="OAuth JIT tenant resolution failed.",
            reason="oauth_jit_tenant_resolution_failed",
            details={"provider_id": normalized_provider_id},
        )
    if not policy.enabled:
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_provisioning_not_allowed",
            message="OAuth JIT provisioning is not allowed by policy.",
            reason="oauth_jit_provisioning_not_allowed",
            details={"provider_id": normalized_provider_id},
        )
    if normalized_provider_id not in policy.eligible_providers:
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_provider_not_eligible",
            message="OAuth provider is not eligible for JIT provisioning.",
            reason="oauth_jit_provider_not_eligible",
            details={"provider_id": normalized_provider_id},
        )

    provider_subject = _extract_required_claim(
        claims=validated_claims,
        claim_name="sub",
        provider_id=normalized_provider_id,
    )
    email_normalized = _extract_required_email_claim(
        claims=validated_claims,
        provider_id=normalized_provider_id,
    )
    _validate_tenant_claim(
        claims=validated_claims,
        expected_tenant_id=normalized_tenant_id,
        provider_id=normalized_provider_id,
    )
    _validate_required_claims_present(
        policy=policy,
        claims=validated_claims,
        provider_id=normalized_provider_id,
    )

    existing_identity_link = linking_store.get_by_identity(
        tenant_id=normalized_tenant_id,
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
    )
    if existing_identity_link is not None:
        raise OAuthJitProvisioningError(
            status_code=409,
            error_code="oauth_jit_identity_conflict",
            message="OAuth JIT identity conflicts with existing account state.",
            reason="oauth_jit_identity_conflict",
            details={
                "provider_id": normalized_provider_id,
                "conflict_class": "identity_already_linked",
            },
        )
    if registration_store.get_user_by_email(email_normalized=email_normalized) is not None:
        raise OAuthJitProvisioningError(
            status_code=409,
            error_code="oauth_jit_identity_conflict",
            message="OAuth JIT identity conflicts with existing account state.",
            reason="oauth_jit_identity_conflict",
            details={
                "provider_id": normalized_provider_id,
                "conflict_class": "email_already_registered",
            },
        )

    now = _now(now_provider=now_provider)
    created_at = _utc_iso(now)
    placeholder_phone = _build_placeholder_phone(
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
        tenant_id=normalized_tenant_id,
    )
    password_seed = f"oauth-jit:{uuid4().hex}:{normalized_provider_id}:{provider_subject}"
    password_hash = build_password_hash(password=password_seed)
    kra_pin_hash = sha256(
        f"oauth-jit:{normalized_provider_id}:{provider_subject}".encode()
    ).hexdigest()
    try:
        created_user = registration_store.register_user(
            email_normalized=email_normalized,
            phone_number_normalized=placeholder_phone,
            kra_pin_hash=kra_pin_hash,
            password_hash=password_hash,
            role=policy.default_role,
            created_at=created_at,
        )
    except RegistrationConflictError as error:
        raise OAuthJitProvisioningError(
            status_code=409,
            error_code="oauth_jit_identity_conflict",
            message="OAuth JIT identity conflicts with existing account state.",
            reason="oauth_jit_identity_conflict",
            details={
                "provider_id": normalized_provider_id,
                "conflict_class": error.reason,
            },
        ) from error
    activated_user = registration_store.mark_user_email_verified(
        user_id=created_user.user_id,
        verified_at=created_at,
    )
    try:
        linking_store.create_link(
            tenant_id=normalized_tenant_id,
            provider_id=normalized_provider_id,
            provider_subject=provider_subject,
            user_id=activated_user.user_id,
            linked_at=created_at,
        )
    except OAuthIdentityLinkingError as error:
        raise OAuthJitProvisioningError(
            status_code=409,
            error_code="oauth_jit_identity_conflict",
            message="OAuth JIT identity conflicts with existing account state.",
            reason="oauth_jit_identity_conflict",
            details={
                "provider_id": normalized_provider_id,
                "conflict_class": error.reason,
            },
        ) from error
    return OAuthJitProvisioningResult(
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
        user_id=activated_user.user_id,
        tenant_id=normalized_tenant_id,
        role=activated_user.role,
        provisioning_status="jit_provisioning_allowed",
    )


def _extract_required_claim(
    *,
    claims: Mapping[str, object],
    claim_name: str,
    provider_id: str,
) -> str:
    raw_value = claims.get(claim_name)
    if not isinstance(raw_value, str):
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_required_claims_missing",
            message="OAuth JIT required claims are missing.",
            reason="oauth_jit_required_claims_missing",
            details={"provider_id": provider_id, "missing_claim": claim_name},
        )
    normalized_value = raw_value.strip()
    if not normalized_value:
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_required_claims_missing",
            message="OAuth JIT required claims are missing.",
            reason="oauth_jit_required_claims_missing",
            details={"provider_id": provider_id, "missing_claim": claim_name},
        )
    return normalized_value


def _extract_required_email_claim(
    *,
    claims: Mapping[str, object],
    provider_id: str,
) -> str:
    raw_email = _extract_required_claim(
        claims=claims,
        claim_name="email",
        provider_id=provider_id,
    )
    return raw_email.lower()


def _validate_required_claims_present(
    *,
    policy: OAuthJitProvisioningPolicy,
    claims: Mapping[str, object],
    provider_id: str,
) -> None:
    for claim_name in sorted(policy.required_claims):
        raw_value = claims.get(claim_name)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise OAuthJitProvisioningError(
                status_code=403,
                error_code="oauth_jit_required_claims_missing",
                message="OAuth JIT required claims are missing.",
                reason="oauth_jit_required_claims_missing",
                details={"provider_id": provider_id, "missing_claim": claim_name},
            )


def _validate_tenant_claim(
    *,
    claims: Mapping[str, object],
    expected_tenant_id: str,
    provider_id: str,
) -> None:
    raw_tenant_claim = claims.get("tenant_id")
    if raw_tenant_claim is None:
        return
    if not isinstance(raw_tenant_claim, str):
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_tenant_resolution_failed",
            message="OAuth JIT tenant resolution failed.",
            reason="oauth_jit_tenant_resolution_failed",
            details={"provider_id": provider_id},
        )
    normalized_tenant_claim = raw_tenant_claim.strip()
    if normalized_tenant_claim != expected_tenant_id:
        raise OAuthJitProvisioningError(
            status_code=403,
            error_code="oauth_jit_tenant_resolution_failed",
            message="OAuth JIT tenant resolution failed.",
            reason="oauth_jit_tenant_resolution_failed",
            details={"provider_id": provider_id},
        )


def _build_placeholder_phone(
    *,
    provider_id: str,
    provider_subject: str,
    tenant_id: str,
) -> str:
    digest = sha256(f"{provider_id}:{provider_subject}:{tenant_id}".encode()).hexdigest()
    suffix = str(int(digest[:16], 16) % 10_000_000_000_000).zfill(13)
    return f"+9{suffix}"


def _normalize_tenant_id(*, tenant_id: str) -> str:
    normalized = tenant_id.strip()
    if not normalized:
        return DEFAULT_TENANT_ID
    return normalized


def _read_bool_env(*, env_var: str, default: bool) -> bool:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_csv_lower_set(*, env_var: str, default: tuple[str, ...]) -> frozenset[str]:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return frozenset(value.strip().lower() for value in default if value.strip())
    output: set[str] = set()
    for item in raw_value.split(","):
        normalized = item.strip().lower()
        if normalized:
            output.add(normalized)
    return frozenset(output)


def _now(*, now_provider: Callable[[], datetime] | None) -> datetime:
    if now_provider is None:
        return datetime.now(UTC)
    return now_provider()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
