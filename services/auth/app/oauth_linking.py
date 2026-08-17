"""Deterministic OAuth external-identity linking to internal auth users."""

from __future__ import annotations

import re
from uuid import UUID
from typing import Literal
from typing import Protocol
from datetime import UTC
from datetime import datetime
from threading import Lock
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Callable

from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.persistence_support import auth_runtime_requires_persistence

DEFAULT_TENANT_ID = "default_tenant"
_PHONE_CLEAN_PATTERN = re.compile(r"[\s\-\(\)]")
_KENYAN_PHONE_DIGITS_PATTERN = re.compile(r"^\d{9}$")


@dataclass(frozen=True)
class OAuthIdentityLinkRecord:
    """Represent one persisted external identity to internal-user binding."""

    provider_id: str
    provider_subject: str
    user_id: UUID
    tenant_id: str
    linked_at: str
    last_seen_at: str


@dataclass(frozen=True)
class OAuthIdentityLinkResult:
    """Represent deterministic identity-link resolution result."""

    provider_id: str
    provider_subject: str
    user_id: UUID
    tenant_id: str
    role: str
    link_status: Literal["linked_existing", "linked_new"]


class OAuthIdentityLinkingError(ValueError):
    """Represent deterministic OAuth identity-linking failure."""

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


def is_oauth_identity_linking_no_match_error(error: OAuthIdentityLinkingError) -> bool:
    """Return whether linking error represents deterministic no-match fallback case."""

    return (
        error.reason == "oauth_identity_linking_not_allowed"
        and error.details.get("requirement") == "existing_account_match_required"
    )


class OAuthIdentityLinkingStoreProtocol(Protocol):
    """Define persistence boundary for external-identity linking records."""

    def get_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
    ) -> OAuthIdentityLinkRecord | None:
        """Return one link record by tenant/provider/subject key when present."""

        ...

    def touch_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        observed_at: str,
    ) -> OAuthIdentityLinkRecord | None:
        """Update one link record last-seen timestamp deterministically."""

        ...

    def create_link(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        user_id: UUID,
        linked_at: str,
    ) -> OAuthIdentityLinkRecord:
        """Create one deterministic link or raise canonical conflict error."""

        ...

    def list_links(self) -> tuple[OAuthIdentityLinkRecord, ...]:
        """Return immutable snapshot of deterministic identity-link records."""

        ...

    def reset(self) -> None:
        """Reset process-local identity-link records for deterministic tests."""

        ...


class InMemoryOAuthIdentityLinkingStore:
    """Persist OAuth identity-link records in memory for deterministic behavior."""

    def __init__(self) -> None:
        self._records_by_identity: dict[tuple[str, str, str], OAuthIdentityLinkRecord] = {}
        self._subject_by_user_provider: dict[tuple[str, str, UUID], str] = {}
        self._lock = Lock()

    def get_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
    ) -> OAuthIdentityLinkRecord | None:
        with self._lock:
            return self._records_by_identity.get(
                _identity_key(
                    tenant_id=tenant_id,
                    provider_id=provider_id,
                    provider_subject=provider_subject,
                )
            )

    def touch_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        observed_at: str,
    ) -> OAuthIdentityLinkRecord | None:
        with self._lock:
            key = _identity_key(
                tenant_id=tenant_id,
                provider_id=provider_id,
                provider_subject=provider_subject,
            )
            existing = self._records_by_identity.get(key)
            if existing is None:
                return None
            updated = OAuthIdentityLinkRecord(
                provider_id=existing.provider_id,
                provider_subject=existing.provider_subject,
                user_id=existing.user_id,
                tenant_id=existing.tenant_id,
                linked_at=existing.linked_at,
                last_seen_at=observed_at,
            )
            self._records_by_identity[key] = updated
            return updated

    def create_link(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        user_id: UUID,
        linked_at: str,
    ) -> OAuthIdentityLinkRecord:
        with self._lock:
            key = _identity_key(
                tenant_id=tenant_id,
                provider_id=provider_id,
                provider_subject=provider_subject,
            )
            existing = self._records_by_identity.get(key)
            if existing is not None:
                if existing.user_id != user_id:
                    raise OAuthIdentityLinkingError(
                        status_code=409,
                        error_code="oauth_identity_already_linked_to_different_user",
                        message=(
                            "OAuth external identity is already linked to a "
                            "different internal user."
                        ),
                        reason="oauth_identity_already_linked_to_different_user",
                        details={"provider_id": provider_id, "tenant_id": tenant_id},
                    )
                updated = OAuthIdentityLinkRecord(
                    provider_id=existing.provider_id,
                    provider_subject=existing.provider_subject,
                    user_id=existing.user_id,
                    tenant_id=existing.tenant_id,
                    linked_at=existing.linked_at,
                    last_seen_at=linked_at,
                )
                self._records_by_identity[key] = updated
                return updated

            user_provider_key = _user_provider_key(
                tenant_id=tenant_id,
                provider_id=provider_id,
                user_id=user_id,
            )
            existing_subject = self._subject_by_user_provider.get(user_provider_key)
            if existing_subject is not None and existing_subject != provider_subject:
                raise OAuthIdentityLinkingError(
                    status_code=403,
                    error_code="oauth_identity_linking_not_allowed",
                    message="OAuth identity linking is not allowed for this account context.",
                    reason="oauth_identity_linking_not_allowed",
                    details={
                        "provider_id": provider_id,
                        "tenant_id": tenant_id,
                        "requirement": "provider_subject_per_user_unique",
                    },
                )

            record = OAuthIdentityLinkRecord(
                provider_id=provider_id,
                provider_subject=provider_subject,
                user_id=user_id,
                tenant_id=tenant_id,
                linked_at=linked_at,
                last_seen_at=linked_at,
            )
            self._records_by_identity[key] = record
            self._subject_by_user_provider[user_provider_key] = provider_subject
            return record

    def list_links(self) -> tuple[OAuthIdentityLinkRecord, ...]:
        with self._lock:
            return tuple(self._records_by_identity.values())

    def reset(self) -> None:
        with self._lock:
            self._records_by_identity.clear()
            self._subject_by_user_provider.clear()


class UnavailableOAuthIdentityLinkingStore:
    """Fail closed when durable OAuth identity-link persistence is unavailable."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
    ) -> None:
        self._status_code = status_code
        self._error_code = error_code
        self._message = message
        self._reason = reason

    def _raise(self) -> None:
        raise OAuthIdentityLinkingError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )

    def get_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
    ) -> OAuthIdentityLinkRecord | None:
        del tenant_id, provider_id, provider_subject
        self._raise()

    def touch_by_identity(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        observed_at: str,
    ) -> OAuthIdentityLinkRecord | None:
        del tenant_id, provider_id, provider_subject, observed_at
        self._raise()

    def create_link(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        provider_subject: str,
        user_id: UUID,
        linked_at: str,
    ) -> OAuthIdentityLinkRecord:
        del tenant_id, provider_id, provider_subject, user_id, linked_at
        self._raise()

    def list_links(self) -> tuple[OAuthIdentityLinkRecord, ...]:
        self._raise()

    def reset(self) -> None:
        return None


def resolve_or_link_oauth_identity(
    *,
    provider_id: str,
    validated_claims: Mapping[str, object],
    tenant_id: str,
    registration_store: RegistrationStoreProtocol,
    linking_store: OAuthIdentityLinkingStoreProtocol,
    now_provider: Callable[[], datetime] | None = None,
) -> OAuthIdentityLinkResult:
    """Resolve deterministic external-identity binding or link it safely."""

    normalized_provider_id = provider_id.strip().lower()
    if not normalized_provider_id:
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_linking_not_allowed",
            message="OAuth identity linking is not allowed for this account context.",
            reason="oauth_identity_linking_not_allowed",
            details={"requirement": "provider_id_required"},
        )
    normalized_tenant_id = _normalize_tenant_id(tenant_id=tenant_id)
    _validate_claim_tenant(
        claims=validated_claims,
        expected_tenant_id=normalized_tenant_id,
        provider_id=normalized_provider_id,
    )
    provider_subject = _extract_provider_subject(
        claims=validated_claims,
        provider_id=normalized_provider_id,
    )
    observed_at = _utc_iso(_now(now_provider=now_provider))
    existing_binding = linking_store.get_by_identity(
        tenant_id=normalized_tenant_id,
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
    )
    if existing_binding is not None:
        if existing_binding.tenant_id != normalized_tenant_id:
            raise OAuthIdentityLinkingError(
                status_code=403,
                error_code="oauth_identity_tenant_mismatch",
                message="OAuth identity tenant context does not match account scope.",
                reason="oauth_identity_tenant_mismatch",
                details={"provider_id": normalized_provider_id},
            )
        linked_user = registration_store.get_user_by_id(user_id=existing_binding.user_id)
        if linked_user is None:
            raise OAuthIdentityLinkingError(
                status_code=403,
                error_code="oauth_identity_linking_not_allowed",
                message="OAuth identity linking is not allowed for this account context.",
                reason="oauth_identity_linking_not_allowed",
                details={
                    "provider_id": normalized_provider_id,
                    "requirement": "linked_user_must_exist",
                },
            )
        linking_store.touch_by_identity(
            tenant_id=normalized_tenant_id,
            provider_id=normalized_provider_id,
            provider_subject=provider_subject,
            observed_at=observed_at,
        )
        return OAuthIdentityLinkResult(
            provider_id=normalized_provider_id,
            provider_subject=provider_subject,
            user_id=linked_user.user_id,
            tenant_id=normalized_tenant_id,
            role=linked_user.role,
            link_status="linked_existing",
        )

    candidate_user = _resolve_candidate_user_for_linking(
        claims=validated_claims,
        registration_store=registration_store,
    )
    if candidate_user is None:
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_linking_not_allowed",
            message="OAuth identity linking is not allowed for this account context.",
            reason="oauth_identity_linking_not_allowed",
            details={
                "provider_id": normalized_provider_id,
                "requirement": "existing_account_match_required",
            },
        )
    if candidate_user.account_state != "active":
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_linking_not_allowed",
            message="OAuth identity linking is not allowed for this account context.",
            reason="oauth_identity_linking_not_allowed",
            details={
                "provider_id": normalized_provider_id,
                "account_state": candidate_user.account_state,
            },
        )
    linking_store.create_link(
        tenant_id=normalized_tenant_id,
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
        user_id=candidate_user.user_id,
        linked_at=observed_at,
    )
    return OAuthIdentityLinkResult(
        provider_id=normalized_provider_id,
        provider_subject=provider_subject,
        user_id=candidate_user.user_id,
        tenant_id=normalized_tenant_id,
        role=candidate_user.role,
        link_status="linked_new",
    )


def get_default_oauth_identity_linking_store() -> OAuthIdentityLinkingStoreProtocol:
    """Return deterministic process-local OAuth identity-linking store."""

    return _default_oauth_identity_linking_store


def build_default_oauth_identity_linking_store() -> OAuthIdentityLinkingStoreProtocol:
    """Build the OAuth identity-linking store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryOAuthIdentityLinkingStore()
    return UnavailableOAuthIdentityLinkingStore(
        status_code=503,
        error_code="oauth_identity_linking_persistence_unavailable",
        message="OAuth identity-link persistence is unavailable.",
        reason="oauth_identity_linking_persistence_unavailable",
    )


def reset_default_oauth_identity_linking_store() -> None:
    """Reset deterministic process-local OAuth identity-linking store."""

    global _default_oauth_identity_linking_store
    _default_oauth_identity_linking_store = build_default_oauth_identity_linking_store()


def _validate_claim_tenant(
    *,
    claims: Mapping[str, object],
    expected_tenant_id: str,
    provider_id: str,
) -> None:
    raw_tenant_claim = claims.get("tenant_id")
    if raw_tenant_claim is None:
        return
    if not isinstance(raw_tenant_claim, str):
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_tenant_mismatch",
            message="OAuth identity tenant context does not match account scope.",
            reason="oauth_identity_tenant_mismatch",
            details={"provider_id": provider_id},
        )
    normalized_tenant_claim = raw_tenant_claim.strip()
    if not normalized_tenant_claim or normalized_tenant_claim != expected_tenant_id:
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_tenant_mismatch",
            message="OAuth identity tenant context does not match account scope.",
            reason="oauth_identity_tenant_mismatch",
            details={"provider_id": provider_id},
        )


def _extract_provider_subject(
    *,
    claims: Mapping[str, object],
    provider_id: str,
) -> str:
    raw_subject = claims.get("sub")
    if not isinstance(raw_subject, str):
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_linking_not_allowed",
            message="OAuth identity linking is not allowed for this account context.",
            reason="oauth_identity_linking_not_allowed",
            details={"provider_id": provider_id, "requirement": "subject_claim_required"},
        )
    normalized_subject = raw_subject.strip()
    if not normalized_subject:
        raise OAuthIdentityLinkingError(
            status_code=403,
            error_code="oauth_identity_linking_not_allowed",
            message="OAuth identity linking is not allowed for this account context.",
            reason="oauth_identity_linking_not_allowed",
            details={"provider_id": provider_id, "requirement": "subject_claim_required"},
        )
    return normalized_subject


def _resolve_candidate_user_for_linking(
    *,
    claims: Mapping[str, object],
    registration_store: RegistrationStoreProtocol,
) -> RegisteredUserRecord | None:
    email_claim = _normalize_email_claim(claims.get("email"))
    phone_claim = _normalize_phone_claim(claims.get("phone_number"))
    email_user = (
        None
        if email_claim is None
        else registration_store.get_user_by_email(email_normalized=email_claim)
    )
    phone_user = (
        None
        if phone_claim is None
        else registration_store.get_user_by_phone(phone_number_normalized=phone_claim)
    )
    if (
        email_user is not None
        and phone_user is not None
        and email_user.user_id != phone_user.user_id
    ):
        raise OAuthIdentityLinkingError(
            status_code=409,
            error_code="oauth_identity_claim_conflict",
            message="OAuth identity claims conflict with existing internal accounts.",
            reason="oauth_identity_claim_conflict",
            details={"claim_fields": ["email", "phone_number"]},
        )
    if email_user is not None:
        return email_user
    if phone_user is not None:
        return phone_user
    return None


def _normalize_email_claim(raw_claim: object) -> str | None:
    if not isinstance(raw_claim, str):
        return None
    normalized = raw_claim.strip().lower()
    if not normalized:
        return None
    return normalized


def _normalize_phone_claim(raw_claim: object) -> str | None:
    if not isinstance(raw_claim, str):
        return None
    cleaned = _PHONE_CLEAN_PATTERN.sub("", raw_claim.strip())
    if not cleaned:
        return None
    if cleaned.startswith("+254"):
        national_number = cleaned[4:]
    elif cleaned.startswith("254"):
        national_number = cleaned[3:]
    elif cleaned.startswith("0"):
        national_number = cleaned[1:]
    else:
        return None
    if _KENYAN_PHONE_DIGITS_PATTERN.fullmatch(national_number) is None:
        return None
    if national_number[0] not in {"1", "7"}:
        return None
    return f"+254{national_number}"


def _normalize_tenant_id(*, tenant_id: str) -> str:
    normalized = tenant_id.strip()
    if not normalized:
        return DEFAULT_TENANT_ID
    return normalized


def _identity_key(
    *, tenant_id: str, provider_id: str, provider_subject: str
) -> tuple[str, str, str]:
    return (tenant_id.strip(), provider_id.strip().lower(), provider_subject.strip())


def _user_provider_key(*, tenant_id: str, provider_id: str, user_id: UUID) -> tuple[str, str, UUID]:
    return (tenant_id.strip(), provider_id.strip().lower(), user_id)


def _now(*, now_provider: Callable[[], datetime] | None) -> datetime:
    if now_provider is None:
        return datetime.now(UTC)
    return now_provider()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


_default_oauth_identity_linking_store: OAuthIdentityLinkingStoreProtocol = (
    build_default_oauth_identity_linking_store()
)
