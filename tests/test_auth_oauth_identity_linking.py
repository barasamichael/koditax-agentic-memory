"""Focused tests for deterministic OAuth external-identity linking behavior."""

from __future__ import annotations

from collections.abc import Callable

from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.oauth_linking import OAuthIdentityLinkingError
from services.auth.app.oauth_linking import resolve_or_link_oauth_identity
from services.auth.app.oauth_linking import InMemoryOAuthIdentityLinkingStore


def test_known_linked_identity_resolves_to_correct_internal_user() -> None:
    registration_store = InMemoryRegistrationStore()
    linked_user = _create_active_user(
        registration_store=registration_store,
        email="linked.user@example.com",
        phone="+254712345678",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()
    linking_store.create_link(
        tenant_id="default_tenant",
        provider_id="google",
        provider_subject="oidc-sub-001",
        user_id=linked_user.user_id,
        linked_at="2026-04-01T10:10:00Z",
    )

    result = resolve_or_link_oauth_identity(
        provider_id="google",
        validated_claims={"sub": "oidc-sub-001"},
        tenant_id="default_tenant",
        registration_store=registration_store,
        linking_store=linking_store,
    )

    assert result.user_id == linked_user.user_id
    assert result.tenant_id == "default_tenant"
    assert result.link_status == "linked_existing"


def test_first_time_link_allowed_path_creates_deterministic_binding() -> None:
    registration_store = InMemoryRegistrationStore()
    linked_user = _create_active_user(
        registration_store=registration_store,
        email="first.link@example.com",
        phone="+254711222333",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()

    result = resolve_or_link_oauth_identity(
        provider_id="google",
        validated_claims={
            "sub": "oidc-sub-first-link",
            "email": "first.link@example.com",
        },
        tenant_id="default_tenant",
        registration_store=registration_store,
        linking_store=linking_store,
    )

    assert result.user_id == linked_user.user_id
    assert result.link_status == "linked_new"
    persisted = linking_store.get_by_identity(
        tenant_id="default_tenant",
        provider_id="google",
        provider_subject="oidc-sub-first-link",
    )
    assert persisted is not None
    assert persisted.user_id == linked_user.user_id


def test_identity_already_linked_to_different_user_is_blocked_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    first_user = _create_active_user(
        registration_store=registration_store,
        email="first.user@example.com",
        phone="+254712000111",
    )
    second_user = _create_active_user(
        registration_store=registration_store,
        email="second.user@example.com",
        phone="+254712000222",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()
    linking_store.create_link(
        tenant_id="default_tenant",
        provider_id="google",
        provider_subject="oidc-sub-conflict",
        user_id=first_user.user_id,
        linked_at="2026-04-01T10:15:00Z",
    )

    first_error = _capture_linking_error(
        lambda: linking_store.create_link(
            tenant_id="default_tenant",
            provider_id="google",
            provider_subject="oidc-sub-conflict",
            user_id=second_user.user_id,
            linked_at="2026-04-01T10:16:00Z",
        )
    )
    second_error = _capture_linking_error(
        lambda: linking_store.create_link(
            tenant_id="default_tenant",
            provider_id="google",
            provider_subject="oidc-sub-conflict",
            user_id=second_user.user_id,
            linked_at="2026-04-01T10:17:00Z",
        )
    )

    assert first_error["reason"] == "oauth_identity_already_linked_to_different_user"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_claim_collision_path_is_blocked_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    _create_active_user(
        registration_store=registration_store,
        email="collision.email@example.com",
        phone="+254733000111",
    )
    _create_active_user(
        registration_store=registration_store,
        email="different.user@example.com",
        phone="+254711999888",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()

    error = _capture_linking_error(
        lambda: resolve_or_link_oauth_identity(
            provider_id="google",
            validated_claims={
                "sub": "oidc-sub-claim-conflict",
                "email": "collision.email@example.com",
                "phone_number": "+254711999888",
            },
            tenant_id="default_tenant",
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )

    assert error["reason"] == "oauth_identity_claim_conflict"


def test_tenant_mismatch_is_blocked_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    _create_active_user(
        registration_store=registration_store,
        email="tenant.user@example.com",
        phone="+254701222333",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()

    first_error = _capture_linking_error(
        lambda: resolve_or_link_oauth_identity(
            provider_id="google",
            validated_claims={
                "sub": "oidc-sub-tenant-mismatch",
                "email": "tenant.user@example.com",
                "tenant_id": "other_tenant",
            },
            tenant_id="default_tenant",
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    second_error = _capture_linking_error(
        lambda: resolve_or_link_oauth_identity(
            provider_id="google",
            validated_claims={
                "sub": "oidc-sub-tenant-mismatch",
                "email": "tenant.user@example.com",
                "tenant_id": "other_tenant",
            },
            tenant_id="default_tenant",
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )

    assert first_error["reason"] == "oauth_identity_tenant_mismatch"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def _create_active_user(
    *,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone: str,
) -> RegisteredUserRecord:
    created = registration_store.register_user(
        email_normalized=email,
        phone_number_normalized=phone,
        kra_pin_hash=f"kra-hash:{email}",
        password_hash=f"password-hash:{email}",
        role="IndividualTaxpayer",
        created_at="2026-04-01T10:00:00Z",
    )
    return registration_store.mark_user_email_verified(
        user_id=created.user_id,
        verified_at="2026-04-01T10:01:00Z",
    )


def _capture_linking_error(action: Callable[[], object]) -> dict[str, object]:
    try:
        action()
    except OAuthIdentityLinkingError as error:
        return {
            "error_code": error.error_code,
            "message": error.message,
            "reason": error.reason,
        }
    raise AssertionError("Expected OAuthIdentityLinkingError")
