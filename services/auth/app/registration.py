"""Implement deterministic registration validation and persistence helpers."""

from __future__ import annotations

import re
from hmac import compare_digest
import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import Literal
from typing import Protocol
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from threading import Lock
from dataclasses import dataclass
from collections.abc import Mapping

import bcrypt
import psycopg
from pydantic import BaseModel

from services.auth.app.config import get_auth_password_bcrypt_cost
from services.auth.app.config import get_auth_password_history_depth
from services.auth.app.account_lifecycle import AccountState
from services.auth.app.account_lifecycle import ensure_state_transition_allowed
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import AuthCockroachTransactionError
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection

ALLOWED_AUTH_ROLES: frozenset[str] = frozenset(
    {
        "IndividualTaxpayer",
        "TaxAgent",
        "Accountant",
        "Administrator",
    }
)
MIN_PASSWORD_LENGTH = 12
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_CLEAN_PATTERN = re.compile(r"[\s\-\(\)]")
_KENYA_NATIONAL_PHONE_PATTERN = re.compile(r"^[17]\d{8}$")
_KRA_PIN_PATTERN = re.compile(r"^[A-Z]\d{9}[A-Z]$")
_DUPLICATE_CONFLICT_MESSAGE = (
    "Registration request conflicts with an existing account."
)
_DUPLICATE_EMAIL_REASON = "registration_duplicate_email"
_DUPLICATE_PHONE_REASON = "registration_duplicate_phone"
_DUPLICATE_EMAIL_OR_PHONE_REASON = "registration_duplicate_email_or_phone"
_EMAIL_UNIQUE_CONSTRAINT_TOKENS: tuple[str, ...] = (
    "uq_users_email_encrypted",
    "users_email_encrypted_key",
    "email_encrypted",
)
_PHONE_UNIQUE_CONSTRAINT_TOKENS: tuple[str, ...] = (
    "uq_users_phone_number_encrypted",
    "users_phone_number_encrypted_key",
    "phone_number_encrypted",
)
_DELEGATION_CONFLICT_MESSAGE = "Delegation request conflicts with an existing active pair."
_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON = "delegation_active_pair_conflict"
_DELEGATION_INVALID_PAIR_REASON = "delegation_invalid_pair"
_BCRYPT_HASH_PREFIXES: tuple[str, ...] = ("$2a$", "$2b$", "$2y$")
_LEGACY_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_VERIFY_DUMMY_BCRYPT_HASH = (
    "$2b$12$Y3PAHZLYXTkh4DON1IT5R.3J5FAvAGm1kCJi1YsmikROxZ.0s2pm6"
)


class RegistrationSuccessEnvelope(BaseModel):
    """Represent deterministic registration success payload."""

    user_id: UUID
    registration_status: Literal["pending_verification"]
    created_at: str


@dataclass(frozen=True)
class RegistrationRequestRecord:
    """Represent validated registration request details."""

    email_normalized: str
    phone_number_normalized: str
    kra_pin_normalized: str
    password: str
    role: str


@dataclass(frozen=True)
class RegisteredUserRecord:
    """Represent one persisted registration record."""

    user_id: UUID
    email_normalized: str
    phone_number_normalized: str
    kra_pin_hash: str
    password_hash: str
    role: str
    created_at: str
    account_state: AccountState = "pending_verification"
    verification_state: Literal["pending_verification", "verified"] = (
        "pending_verification"
    )
    verified_at: str | None = None
    credentials_invalidated_at: str | None = None
    deletion_lifecycle_state: Literal["none", "tombstoned"] = "none"
    anonymized_at: str | None = None
    password_history_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegationRecord:
    """Represent one persisted principal-to-delegate authorization record."""

    delegation_id: UUID
    principal_user_id: UUID
    delegate_user_id: UUID
    granted_at: str
    revoked_at: str | None
    is_active: bool
    created_at: str


class RegistrationValidationError(ValueError):
    """Represent deterministic registration validation failure."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


class RegistrationConflictError(ValueError):
    """Represent deterministic duplicate registration conflict."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason


class RegistrationPersistenceError(ValueError):
    """Represent fail-closed registration persistence/runtime failures."""

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


class DelegationValidationError(ValueError):
    """Represent deterministic delegation validation failure."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


class DelegationConflictError(ValueError):
    """Represent deterministic delegation uniqueness conflict."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason


class DelegationPersistenceError(ValueError):
    """Represent fail-closed delegation persistence/runtime failures."""

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


class RegistrationStoreProtocol(Protocol):
    """Define persistence boundary for registration records."""

    def register_user(
        self,
        *,
        email_normalized: str,
        phone_number_normalized: str,
        kra_pin_hash: str,
        password_hash: str,
        role: str,
        created_at: str,
    ) -> RegisteredUserRecord:
        """Persist one registration record or raise deterministic conflict."""

        ...

    def get_user_by_email(
        self, *, email_normalized: str
    ) -> RegisteredUserRecord | None:
        """Return one registered user by normalized email when present."""

        ...

    def get_user_by_phone(
        self, *, phone_number_normalized: str
    ) -> RegisteredUserRecord | None:
        """Return one registered user by normalized phone when present."""

        ...

    def get_user_by_id(self, *, user_id: UUID) -> RegisteredUserRecord | None:
        """Return one registered user by identifier when present."""

        ...

    def mark_user_email_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        """Transition one registered user into verified state deterministically."""

        ...

    def mark_user_phone_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        """Transition one registered user into verified state deterministically."""

        ...

    def update_user_phone_number(
        self,
        *,
        user_id: UUID,
        phone_number_normalized: str,
    ) -> RegisteredUserRecord:
        """Update one user's normalized phone number deterministically."""

        ...

    def lock_user(
        self,
        *,
        user_id: UUID,
    ) -> RegisteredUserRecord:
        """Transition one user into locked state deterministically."""

        ...

    def disable_user(
        self,
        *,
        user_id: UUID,
    ) -> RegisteredUserRecord:
        """Transition one user into disabled state deterministically."""

        ...

    def update_user_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
    ) -> RegisteredUserRecord:
        """Update one user's password hash deterministically."""

        ...

    def tombstone_user_and_invalidate_credentials(
        self,
        *,
        user_id: UUID,
        tombstoned_at: str,
    ) -> RegisteredUserRecord:
        """Apply deterministic tombstone transform and credential invalidation."""

        ...

    def is_password_valid(
        self,
        *,
        user_id: UUID,
        password: str,
    ) -> bool:
        """Return whether supplied password currently validates for one user."""

        ...

    def is_password_reused(
        self,
        *,
        user_id: UUID,
        password: str,
        history_depth: int | None = None,
    ) -> bool:
        """Return whether supplied password matches recent password-history hashes."""

        ...


class DelegationStoreProtocol(Protocol):
    """Define persistence boundary for delegation records."""

    def create_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        """Persist one active delegation record deterministically."""

        ...

    def grant_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        """Alias for deterministic delegation creation."""

        ...

    def reactivate_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        """Alias for deterministic delegation re-granting after revocation."""

        ...

    def revoke_delegation(
        self,
        *,
        delegation_id: UUID,
        revoked_at: str,
    ) -> DelegationRecord:
        """Revoke one delegation record deterministically."""

        ...

    def get_delegation_by_id(
        self,
        *,
        delegation_id: UUID,
    ) -> DelegationRecord | None:
        """Return one delegation record by identifier when present."""

        ...

    def get_active_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> DelegationRecord | None:
        """Return one active delegation for a principal/delegate pair."""

        ...


class InMemoryRegistrationStore:
    """Persist registration records in memory for deterministic baseline behavior."""

    def __init__(self) -> None:
        self._records_by_email: dict[str, RegisteredUserRecord] = {}
        self._records_by_phone: dict[str, RegisteredUserRecord] = {}
        self._records_by_user_id: dict[UUID, RegisteredUserRecord] = {}
        self._lock = Lock()

    def register_user(
        self,
        *,
        email_normalized: str,
        phone_number_normalized: str,
        kra_pin_hash: str,
        password_hash: str,
        role: str,
        created_at: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            if (
                email_normalized in self._records_by_email
                or phone_number_normalized in self._records_by_phone
            ):
                raise RegistrationConflictError(
                    error_code=_DUPLICATE_EMAIL_OR_PHONE_REASON,
                    message=_DUPLICATE_CONFLICT_MESSAGE,
                    reason=_DUPLICATE_EMAIL_OR_PHONE_REASON,
                )

            record = RegisteredUserRecord(
                user_id=uuid4(),
                email_normalized=email_normalized,
                phone_number_normalized=phone_number_normalized,
                kra_pin_hash=kra_pin_hash,
                password_hash=password_hash,
                role=role,
                created_at=created_at,
                account_state="pending_verification",
                verification_state="pending_verification",
                verified_at=None,
                credentials_invalidated_at=None,
                deletion_lifecycle_state="none",
                anonymized_at=None,
                password_history_hashes=(password_hash,),
            )
            self._records_by_email[email_normalized] = record
            self._records_by_phone[phone_number_normalized] = record
            self._records_by_user_id[record.user_id] = record
            return record

    def get_user_by_email(
        self, *, email_normalized: str
    ) -> RegisteredUserRecord | None:
        with self._lock:
            return self._records_by_email.get(email_normalized)

    def get_user_by_phone(
        self, *, phone_number_normalized: str
    ) -> RegisteredUserRecord | None:
        with self._lock:
            return self._records_by_phone.get(phone_number_normalized)

    def get_user_by_id(self, *, user_id: UUID) -> RegisteredUserRecord | None:
        with self._lock:
            return self._records_by_user_id.get(user_id)

    def mark_user_email_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            ensure_state_transition_allowed(
                current_state=existing.account_state,
                requested_state="active",
            )
            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=existing.email_normalized,
                phone_number_normalized=existing.phone_number_normalized,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=existing.password_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state="active",
                verification_state="verified",
                verified_at=verified_at,
                credentials_invalidated_at=existing.credentials_invalidated_at,
                deletion_lifecycle_state=existing.deletion_lifecycle_state,
                anonymized_at=existing.anonymized_at,
                password_history_hashes=existing.password_history_hashes,
            )
            self._records_by_user_id[user_id] = updated
            self._records_by_email[existing.email_normalized] = updated
            self._records_by_phone[existing.phone_number_normalized] = updated
            return updated

    def mark_user_phone_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            ensure_state_transition_allowed(
                current_state=existing.account_state,
                requested_state="active",
            )
            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=existing.email_normalized,
                phone_number_normalized=existing.phone_number_normalized,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=existing.password_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state="active",
                verification_state="verified",
                verified_at=verified_at,
                credentials_invalidated_at=existing.credentials_invalidated_at,
                deletion_lifecycle_state=existing.deletion_lifecycle_state,
                anonymized_at=existing.anonymized_at,
                password_history_hashes=existing.password_history_hashes,
            )
            self._records_by_user_id[user_id] = updated
            self._records_by_email[existing.email_normalized] = updated
            self._records_by_phone[existing.phone_number_normalized] = updated
            return updated

    def update_user_phone_number(
        self,
        *,
        user_id: UUID,
        phone_number_normalized: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            existing_phone_owner = self._records_by_phone.get(
                phone_number_normalized
            )
            if (
                existing_phone_owner is not None
                and existing_phone_owner.user_id != existing.user_id
            ):
                raise RegistrationConflictError(
                    error_code=_DUPLICATE_PHONE_REASON,
                    message=_DUPLICATE_CONFLICT_MESSAGE,
                    reason=_DUPLICATE_PHONE_REASON,
                )
            if existing.phone_number_normalized == phone_number_normalized:
                return existing

            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=existing.email_normalized,
                phone_number_normalized=phone_number_normalized,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=existing.password_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state=existing.account_state,
                verification_state=existing.verification_state,
                verified_at=existing.verified_at,
                credentials_invalidated_at=existing.credentials_invalidated_at,
                deletion_lifecycle_state=existing.deletion_lifecycle_state,
                anonymized_at=existing.anonymized_at,
                password_history_hashes=existing.password_history_hashes,
            )
            self._records_by_phone.pop(existing.phone_number_normalized, None)
            self._records_by_user_id[user_id] = updated
            self._records_by_email[existing.email_normalized] = updated
            self._records_by_phone[updated.phone_number_normalized] = updated
            return updated

    def lock_user(
        self,
        *,
        user_id: UUID,
    ) -> RegisteredUserRecord:
        return self._transition_user_state(
            user_id=user_id, requested_state="locked"
        )

    def disable_user(
        self,
        *,
        user_id: UUID,
    ) -> RegisteredUserRecord:
        return self._transition_user_state(
            user_id=user_id, requested_state="disabled"
        )

    def update_user_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            password_history_depth = get_auth_password_history_depth()
            password_history_hashes = tuple(
                value
                for value in (password_hash, *existing.password_history_hashes)
                if value
            )[:password_history_depth]
            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=existing.email_normalized,
                phone_number_normalized=existing.phone_number_normalized,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=password_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state=existing.account_state,
                verification_state=existing.verification_state,
                verified_at=existing.verified_at,
                credentials_invalidated_at=None,
                deletion_lifecycle_state=existing.deletion_lifecycle_state,
                anonymized_at=existing.anonymized_at,
                password_history_hashes=password_history_hashes,
            )
            self._records_by_user_id[user_id] = updated
            self._records_by_email[existing.email_normalized] = updated
            self._records_by_phone[existing.phone_number_normalized] = updated
            return updated

    def tombstone_user_and_invalidate_credentials(
        self,
        *,
        user_id: UUID,
        tombstoned_at: str,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            if existing.account_state != "disabled":
                ensure_state_transition_allowed(
                    current_state=existing.account_state,
                    requested_state="disabled",
                )

            anonymized_email = f"deleted-{existing.user_id.hex}@deleted.invalid"
            anonymized_phone = _build_tombstoned_phone_number(
                user_id=existing.user_id
            )
            invalidated_hash = sha256(
                f"tombstoned:{existing.user_id}:{tombstoned_at}".encode()
            ).hexdigest()
            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=anonymized_email,
                phone_number_normalized=anonymized_phone,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=invalidated_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state="disabled",
                verification_state=existing.verification_state,
                verified_at=existing.verified_at,
                credentials_invalidated_at=tombstoned_at,
                deletion_lifecycle_state="tombstoned",
                anonymized_at=tombstoned_at,
                password_history_hashes=existing.password_history_hashes,
            )
            self._records_by_email.pop(existing.email_normalized, None)
            self._records_by_phone.pop(existing.phone_number_normalized, None)
            self._records_by_user_id[user_id] = updated
            self._records_by_email[updated.email_normalized] = updated
            self._records_by_phone[updated.phone_number_normalized] = updated
            return updated

    def is_password_valid(
        self,
        *,
        user_id: UUID,
        password: str,
    ) -> bool:
        with self._lock:
            existing = self._records_by_user_id.get(user_id)
            if existing is None:
                return False
            if existing.credentials_invalidated_at is not None:
                return False
            return verify_password_against_hash(
                password=password,
                password_hash=existing.password_hash,
            )

    def is_password_reused(
        self,
        *,
        user_id: UUID,
        password: str,
        history_depth: int | None = None,
    ) -> bool:
        with self._lock:
            existing = self._records_by_user_id.get(user_id)
            if existing is None:
                return False
            effective_depth = (
                history_depth
                if history_depth is not None
                else get_auth_password_history_depth()
            )
            if effective_depth <= 0:
                return False
            history_slice = existing.password_history_hashes[:effective_depth]
            for history_hash in history_slice:
                if verify_password_against_hash(
                    password=password,
                    password_hash=history_hash,
                ):
                    return True
            return False

    def _transition_user_state(
        self,
        *,
        user_id: UUID,
        requested_state: AccountState,
    ) -> RegisteredUserRecord:
        with self._lock:
            existing = self._records_by_user_id[user_id]
            ensure_state_transition_allowed(
                current_state=existing.account_state,
                requested_state=requested_state,
            )
            updated = RegisteredUserRecord(
                user_id=existing.user_id,
                email_normalized=existing.email_normalized,
                phone_number_normalized=existing.phone_number_normalized,
                kra_pin_hash=existing.kra_pin_hash,
                password_hash=existing.password_hash,
                role=existing.role,
                created_at=existing.created_at,
                account_state=requested_state,
                verification_state=_resolve_verification_state(
                    previous_state=existing.verification_state,
                    requested_state=requested_state,
                ),
                verified_at=existing.verified_at,
                credentials_invalidated_at=existing.credentials_invalidated_at,
                deletion_lifecycle_state=existing.deletion_lifecycle_state,
                anonymized_at=existing.anonymized_at,
                password_history_hashes=existing.password_history_hashes,
            )
            self._records_by_user_id[user_id] = updated
            self._records_by_email[existing.email_normalized] = updated
            self._records_by_phone[existing.phone_number_normalized] = updated
            return updated


class InMemoryDelegationStore:
    """Persist delegation records in memory for deterministic baseline behavior."""

    def __init__(self) -> None:
        self._records_by_id: dict[UUID, DelegationRecord] = {}
        self._active_by_pair: dict[tuple[UUID, UUID], UUID] = {}
        self._lock = Lock()

    def create_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        return self.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at=granted_at,
        )

    def grant_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        self._validate_delegation_pair(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )
        with self._lock:
            pair_key = (principal_user_id, delegate_user_id)
            if pair_key in self._active_by_pair:
                raise DelegationConflictError(
                    error_code=_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON,
                    message=_DELEGATION_CONFLICT_MESSAGE,
                    reason=_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON,
                )
            record = DelegationRecord(
                delegation_id=uuid4(),
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                granted_at=granted_at,
                revoked_at=None,
                is_active=True,
                created_at=granted_at,
            )
            self._records_by_id[record.delegation_id] = record
            self._active_by_pair[pair_key] = record.delegation_id
            return record

    def reactivate_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        return self.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at=granted_at,
        )

    def revoke_delegation(
        self,
        *,
        delegation_id: UUID,
        revoked_at: str,
    ) -> DelegationRecord:
        _parse_utc_iso(value=revoked_at)
        with self._lock:
            existing = self._records_by_id.get(delegation_id)
            if existing is None:
                raise _delegation_missing_state()
            if not existing.is_active:
                return existing
            updated = DelegationRecord(
                delegation_id=existing.delegation_id,
                principal_user_id=existing.principal_user_id,
                delegate_user_id=existing.delegate_user_id,
                granted_at=existing.granted_at,
                revoked_at=revoked_at,
                is_active=False,
                created_at=existing.created_at,
            )
            self._records_by_id[delegation_id] = updated
            self._active_by_pair.pop(
                (existing.principal_user_id, existing.delegate_user_id),
                None,
            )
            return updated

    def get_delegation_by_id(
        self,
        *,
        delegation_id: UUID,
    ) -> DelegationRecord | None:
        with self._lock:
            return self._records_by_id.get(delegation_id)

    def get_active_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> DelegationRecord | None:
        with self._lock:
            delegation_id = self._active_by_pair.get(
                (principal_user_id, delegate_user_id)
            )
            if delegation_id is None:
                return None
            return self._records_by_id.get(delegation_id)

    def _validate_delegation_pair(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> None:
        if principal_user_id == delegate_user_id:
            raise DelegationValidationError(
                error_code=_DELEGATION_INVALID_PAIR_REASON,
                message="Delegation principal and delegate must differ.",
                reason=_DELEGATION_INVALID_PAIR_REASON,
            )


class UnavailableRegistrationStore:
    """Fail closed when production auth persistence is not available."""

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

    def register_user(
        self,
        *,
        email_normalized: str,
        phone_number_normalized: str,
        kra_pin_hash: str,
        password_hash: str,
        role: str,
        created_at: str,
    ) -> RegisteredUserRecord:
        del (
            email_normalized,
            phone_number_normalized,
            kra_pin_hash,
            password_hash,
            role,
            created_at,
        )
        raise self._error()

    def get_user_by_email(
        self, *, email_normalized: str
    ) -> RegisteredUserRecord | None:
        del email_normalized
        raise self._error()

    def get_user_by_phone(
        self, *, phone_number_normalized: str
    ) -> RegisteredUserRecord | None:
        del phone_number_normalized
        raise self._error()

    def get_user_by_id(self, *, user_id: UUID) -> RegisteredUserRecord | None:
        del user_id
        raise self._error()

    def mark_user_email_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        del user_id, verified_at
        raise self._error()

    def mark_user_phone_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        del user_id, verified_at
        raise self._error()

    def update_user_phone_number(
        self,
        *,
        user_id: UUID,
        phone_number_normalized: str,
    ) -> RegisteredUserRecord:
        del user_id, phone_number_normalized
        raise self._error()

    def lock_user(self, *, user_id: UUID) -> RegisteredUserRecord:
        del user_id
        raise self._error()

    def disable_user(self, *, user_id: UUID) -> RegisteredUserRecord:
        del user_id
        raise self._error()

    def update_user_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
    ) -> RegisteredUserRecord:
        del user_id, password_hash
        raise self._error()

    def tombstone_user_and_invalidate_credentials(
        self,
        *,
        user_id: UUID,
        tombstoned_at: str,
    ) -> RegisteredUserRecord:
        del user_id, tombstoned_at
        raise self._error()

    def is_password_valid(
        self,
        *,
        user_id: UUID,
        password: str,
    ) -> bool:
        del user_id, password
        raise self._error()

    def is_password_reused(
        self,
        *,
        user_id: UUID,
        password: str,
        history_depth: int | None = None,
    ) -> bool:
        del user_id, password, history_depth
        raise self._error()

    def _error(self) -> RegistrationPersistenceError:
        return RegistrationPersistenceError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class UnavailableDelegationStore:
    """Fail closed when production delegation persistence is not available."""

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

    def create_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        del principal_user_id, delegate_user_id, granted_at
        raise self._error()

    def grant_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        del principal_user_id, delegate_user_id, granted_at
        raise self._error()

    def reactivate_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        del principal_user_id, delegate_user_id, granted_at
        raise self._error()

    def revoke_delegation(
        self,
        *,
        delegation_id: UUID,
        revoked_at: str,
    ) -> DelegationRecord:
        del delegation_id, revoked_at
        raise self._error()

    def get_delegation_by_id(
        self,
        *,
        delegation_id: UUID,
    ) -> DelegationRecord | None:
        del delegation_id
        raise self._error()

    def get_active_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> DelegationRecord | None:
        del principal_user_id, delegate_user_id
        raise self._error()

    def _error(self) -> DelegationPersistenceError:
        return DelegationPersistenceError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentRegistrationStore:
    """Persist core auth registration/runtime state in CockroachDB."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def register_user(
        self,
        *,
        email_normalized: str,
        phone_number_normalized: str,
        kra_pin_hash: str,
        password_hash: str,
        role: str,
        created_at: str,
    ) -> RegisteredUserRecord:
        created_at_value = _parse_utc_iso(value=created_at)
        user_id = uuid4()

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> RegisteredUserRecord:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO users (
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            role,
                            created_at,
                            updated_at,
                            password_hash,
                            password_history_hashes,
                            account_state,
                            verification_state,
                            deletion_lifecycle_state
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            LEAST(%s, now()),
                            LEAST(%s, now()),
                            %s,
                            %s::jsonb,
                            %s,
                            %s,
                            %s
                        )
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (
                            user_id,
                            email_normalized,
                            phone_number_normalized,
                            kra_pin_hash,
                            role,
                            created_at_value,
                            created_at_value,
                            password_hash,
                            json.dumps([password_hash]),
                            "pending_verification",
                            "pending_verification",
                            "none",
                        ),
                    )
                    row = cursor.fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise _map_persistent_registration_conflict(error=error) from error

            if row is None:
                raise _registration_missing_state()
            return _row_to_registered_user_record(row=row)

        def _reconcile_callback() -> RegisteredUserRecord | None:
            return self.get_user_by_id(user_id=user_id)

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except RegistrationConflictError:
            raise
        except AuthCockroachTransactionError as error:
            raise _registration_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error

    def get_user_by_email(
        self, *, email_normalized: str
    ) -> RegisteredUserRecord | None:
        return self._get_user_by_field(
            field_name="email_encrypted",
            field_value=email_normalized,
        )

    def get_user_by_phone(
        self, *, phone_number_normalized: str
    ) -> RegisteredUserRecord | None:
        return self._get_user_by_field(
            field_name="phone_number_encrypted",
            field_value=phone_number_normalized,
        )

    def get_user_by_id(self, *, user_id: UUID) -> RegisteredUserRecord | None:
        return self._get_user_by_field(field_name="id", field_value=user_id)

    def mark_user_email_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        return self._update_verification_state(
            user_id=user_id, verified_at=verified_at
        )

    def mark_user_phone_verified(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        return self._update_verification_state(
            user_id=user_id, verified_at=verified_at
        )

    def update_user_phone_number(
        self,
        *,
        user_id: UUID,
        phone_number_normalized: str,
    ) -> RegisteredUserRecord:
        current = self.get_user_by_id(user_id=user_id)
        if current is None:
            raise _registration_missing_state()
        if current.phone_number_normalized == phone_number_normalized:
            return current
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET phone_number_encrypted = %s,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (phone_number_normalized, user_id),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.errors.UniqueViolation as error:
            raise _map_persistent_registration_conflict(error=error) from error
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if row is None:
            raise _registration_missing_state()
        return _row_to_registered_user_record(row=row)

    def lock_user(self, *, user_id: UUID) -> RegisteredUserRecord:
        return self._update_account_state(
            user_id=user_id, requested_state="locked"
        )

    def disable_user(self, *, user_id: UUID) -> RegisteredUserRecord:
        return self._update_account_state(
            user_id=user_id, requested_state="disabled"
        )

    def update_user_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
    ) -> RegisteredUserRecord:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT password_history_hashes
                        FROM users
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (user_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise _registration_missing_state()
                    current_history = _coerce_password_history(row[0])
                    password_history_depth = get_auth_password_history_depth()
                    password_history_hashes = tuple(
                        value
                        for value in (password_hash, *current_history)
                        if value
                    )[:password_history_depth]
                    cursor.execute(
                        """
                        UPDATE users
                        SET password_hash = %s,
                            password_history_hashes = %s::jsonb,
                            credentials_invalidated_at = NULL,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (
                            password_hash,
                            json.dumps(list(password_history_hashes)),
                            user_id,
                        ),
                    )
                    updated_row = cursor.fetchone()
                connection.commit()
        except RegistrationPersistenceError:
            raise
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if updated_row is None:
            raise _registration_missing_state()
        return _row_to_registered_user_record(row=updated_row)

    def tombstone_user_and_invalidate_credentials(
        self,
        *,
        user_id: UUID,
        tombstoned_at: str,
    ) -> RegisteredUserRecord:
        current = self.get_user_by_id(user_id=user_id)
        if current is None:
            raise _registration_missing_state()
        if current.account_state != "disabled":
            ensure_state_transition_allowed(
                current_state=current.account_state,
                requested_state="disabled",
            )

        anonymized_email = f"deleted-{current.user_id.hex}@deleted.invalid"
        anonymized_phone = _build_tombstoned_phone_number(
            user_id=current.user_id
        )
        invalidated_hash = sha256(
            f"tombstoned:{current.user_id}:{tombstoned_at}".encode()
        ).hexdigest()
        tombstoned_at_value = _parse_utc_iso(value=tombstoned_at)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET email_encrypted = %s,
                            phone_number_encrypted = %s,
                            password_hash = %s,
                            account_state = 'disabled',
                            credentials_invalidated_at = %s,
                            deletion_lifecycle_state = 'tombstoned',
                            anonymized_at = %s,
                            updated_at = %s
                        WHERE id = %s
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (
                            anonymized_email,
                            anonymized_phone,
                            invalidated_hash,
                            tombstoned_at_value,
                            tombstoned_at_value,
                            tombstoned_at_value,
                            user_id,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if row is None:
            raise _registration_missing_state()
        return _row_to_registered_user_record(row=row)

    def is_password_valid(
        self,
        *,
        user_id: UUID,
        password: str,
    ) -> bool:
        current = self.get_user_by_id(user_id=user_id)
        if current is None or current.credentials_invalidated_at is not None:
            return False
        return verify_password_against_hash(
            password=password,
            password_hash=current.password_hash,
        )

    def is_password_reused(
        self,
        *,
        user_id: UUID,
        password: str,
        history_depth: int | None = None,
    ) -> bool:
        current = self.get_user_by_id(user_id=user_id)
        if current is None:
            return False
        effective_depth = (
            history_depth
            if history_depth is not None
            else get_auth_password_history_depth()
        )
        if effective_depth <= 0:
            return False
        for history_hash in current.password_history_hashes[:effective_depth]:
            if verify_password_against_hash(
                password=password, password_hash=history_hash
            ):
                return True
        return False

    def _get_user_by_field(
        self,
        *,
        field_name: str,
        field_value: object,
    ) -> RegisteredUserRecord | None:
        if field_name == "email_encrypted":
            query = """
                SELECT
                    id,
                    email_encrypted,
                    phone_number_encrypted,
                    kra_pin_encrypted,
                    password_hash,
                    role,
                    created_at,
                    account_state,
                    verification_state,
                    verified_at,
                    credentials_invalidated_at,
                    deletion_lifecycle_state,
                    anonymized_at,
                    password_history_hashes
                FROM users
                WHERE email_encrypted = %s
            """
        elif field_name == "phone_number_encrypted":
            query = """
                SELECT
                    id,
                    email_encrypted,
                    phone_number_encrypted,
                    kra_pin_encrypted,
                    password_hash,
                    role,
                    created_at,
                    account_state,
                    verification_state,
                    verified_at,
                    credentials_invalidated_at,
                    deletion_lifecycle_state,
                    anonymized_at,
                    password_history_hashes
                FROM users
                WHERE phone_number_encrypted = %s
            """
        elif field_name == "id":
            query = """
                SELECT
                    id,
                    email_encrypted,
                    phone_number_encrypted,
                    kra_pin_encrypted,
                    password_hash,
                    role,
                    created_at,
                    account_state,
                    verification_state,
                    verified_at,
                    credentials_invalidated_at,
                    deletion_lifecycle_state,
                    anonymized_at,
                    password_history_hashes
                FROM users
                WHERE id = %s
            """
        else:
            raise _registration_missing_state()
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (field_value,))
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_registered_user_record(row=row)

    def _update_verification_state(
        self,
        *,
        user_id: UUID,
        verified_at: str,
    ) -> RegisteredUserRecord:
        current = self.get_user_by_id(user_id=user_id)
        if current is None:
            raise _registration_missing_state()
        ensure_state_transition_allowed(
            current_state=current.account_state,
            requested_state="active",
        )
        verified_at_value = _parse_utc_iso(value=verified_at)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET account_state = 'active',
                            verification_state = 'verified',
                            verified_at = %s,
                            updated_at = %s
                        WHERE id = %s
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (verified_at_value, verified_at_value, user_id),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if row is None:
            raise _registration_missing_state()
        return _row_to_registered_user_record(row=row)

    def _update_account_state(
        self,
        *,
        user_id: UUID,
        requested_state: AccountState,
    ) -> RegisteredUserRecord:
        current = self.get_user_by_id(user_id=user_id)
        if current is None:
            raise _registration_missing_state()
        ensure_state_transition_allowed(
            current_state=current.account_state,
            requested_state=requested_state,
        )
        next_verification_state = _resolve_verification_state(
            previous_state=current.verification_state,
            requested_state=requested_state,
        )
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET account_state = %s,
                            verification_state = %s,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING
                            id,
                            email_encrypted,
                            phone_number_encrypted,
                            kra_pin_encrypted,
                            password_hash,
                            role,
                            created_at,
                            account_state,
                            verification_state,
                            verified_at,
                            credentials_invalidated_at,
                            deletion_lifecycle_state,
                            anonymized_at,
                            password_history_hashes
                        """,
                        (requested_state, next_verification_state, user_id),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise _registration_persistence_unavailable() from error
        if row is None:
            raise _registration_missing_state()
        return _row_to_registered_user_record(row=row)


class PersistentDelegationStore:
    """Persist delegation records in CockroachDB."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def create_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        return self.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at=granted_at,
        )

    def grant_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        self._validate_delegation_pair(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )
        granted_at_value = _parse_utc_iso(value=granted_at)
        delegation_id = uuid4()

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> DelegationRecord:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO delegations (
                            id,
                            principal_user_id,
                            delegate_user_id,
                            granted_at,
                            revoked_at,
                            is_active,
                            created_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            NULL,
                            TRUE,
                            %s
                        )
                        RETURNING
                            id,
                            principal_user_id,
                            delegate_user_id,
                            granted_at,
                            revoked_at,
                            is_active,
                            created_at
                        """,
                        (
                            delegation_id,
                            principal_user_id,
                            delegate_user_id,
                            granted_at_value,
                            granted_at_value,
                        ),
                    )
                    row = cursor.fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise _map_persistent_delegation_conflict(error=error) from error

            if row is None:
                raise _delegation_missing_state()
            return _row_to_delegation_record(row=row)

        def _reconcile_callback() -> DelegationRecord | None:
            return self.get_delegation_by_id(delegation_id=delegation_id)

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except DelegationConflictError:
            raise
        except AuthCockroachTransactionError as error:
            raise _delegation_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _delegation_persistence_unavailable() from error

    def reactivate_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
        granted_at: str,
    ) -> DelegationRecord:
        return self.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at=granted_at,
        )

    def revoke_delegation(
        self,
        *,
        delegation_id: UUID,
        revoked_at: str,
    ) -> DelegationRecord:
        revoked_at_value = _parse_utc_iso(value=revoked_at)

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> DelegationRecord:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        principal_user_id,
                        delegate_user_id,
                        granted_at,
                        revoked_at,
                        is_active,
                        created_at
                    FROM delegations
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (delegation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise _delegation_missing_state()

                existing = _row_to_delegation_record(row=row)
                if not existing.is_active:
                    return existing

                cursor.execute(
                    """
                    UPDATE delegations
                    SET revoked_at = %s,
                        is_active = FALSE
                    WHERE id = %s
                    RETURNING
                        id,
                        principal_user_id,
                        delegate_user_id,
                        granted_at,
                        revoked_at,
                        is_active,
                        created_at
                    """,
                    (revoked_at_value, delegation_id),
                )
                updated_row = cursor.fetchone()
            if updated_row is None:
                raise _delegation_missing_state()
            return _row_to_delegation_record(row=updated_row)

        def _reconcile_callback() -> DelegationRecord | None:
            return self.get_delegation_by_id(delegation_id=delegation_id)

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _delegation_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _delegation_persistence_unavailable() from error

    def get_delegation_by_id(
        self,
        *,
        delegation_id: UUID,
    ) -> DelegationRecord | None:
        return self._get_delegation_by_field(field_name="id", field_value=delegation_id)

    def get_active_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> DelegationRecord | None:
        return self._get_active_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )

    def _validate_delegation_pair(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> None:
        if principal_user_id == delegate_user_id:
            raise DelegationValidationError(
                error_code=_DELEGATION_INVALID_PAIR_REASON,
                message="Delegation principal and delegate must differ.",
                reason=_DELEGATION_INVALID_PAIR_REASON,
            )

    def _get_delegation_by_field(
        self,
        *,
        field_name: str,
        field_value: object,
    ) -> DelegationRecord | None:
        if field_name == "id":
            query = """
                SELECT
                    id,
                    principal_user_id,
                    delegate_user_id,
                    granted_at,
                    revoked_at,
                    is_active,
                    created_at
                FROM delegations
                WHERE id = %s
            """
        else:
            raise _delegation_missing_state()
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (field_value,))
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _delegation_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_delegation_record(row=row)

    def _get_active_delegation(
        self,
        *,
        principal_user_id: UUID,
        delegate_user_id: UUID,
    ) -> DelegationRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            id,
                            principal_user_id,
                            delegate_user_id,
                            granted_at,
                            revoked_at,
                            is_active,
                            created_at
                        FROM delegations
                        WHERE principal_user_id = %s
                          AND delegate_user_id = %s
                          AND is_active = TRUE
                        """,
                        (principal_user_id, delegate_user_id),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _delegation_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_delegation_record(row=row)


def _row_to_registered_user_record(
    *, row: tuple[object, ...]
) -> RegisteredUserRecord:
    return RegisteredUserRecord(
        user_id=UUID(str(row[0])),
        email_normalized=str(row[1]),
        phone_number_normalized=str(row[2]),
        kra_pin_hash=str(row[3] or ""),
        password_hash=str(row[4] or ""),
        role=str(row[5]),
        created_at=_utc_iso_value(row[6]),
        account_state=cast(AccountState, str(row[7])),
        verification_state=cast(
            Literal["pending_verification", "verified"],
            str(row[8]),
        ),
        verified_at=_optional_utc_iso_value(row[9]),
        credentials_invalidated_at=_optional_utc_iso_value(row[10]),
        deletion_lifecycle_state=cast(
            Literal["none", "tombstoned"], str(row[11])
        ),
        anonymized_at=_optional_utc_iso_value(row[12]),
        password_history_hashes=_coerce_password_history(row[13]),
    )


def _coerce_password_history(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if isinstance(value, list):
        value_list = cast(list[object], value)
        return tuple(item for item in value_list if isinstance(item, str))
    if isinstance(value, tuple):
        value_tuple = cast(tuple[object, ...], value)
        return tuple(item for item in value_tuple if isinstance(item, str))
    return ()


def _registration_persistence_unavailable() -> RegistrationPersistenceError:
    return RegistrationPersistenceError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _registration_missing_state() -> RegistrationPersistenceError:
    return RegistrationPersistenceError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


def _map_persistent_registration_conflict(
    *,
    error: psycopg.errors.UniqueViolation,
) -> RegistrationConflictError:
    constraint_name = getattr(error.diag, "constraint_name", "") or ""
    normalized_constraint_name = str(constraint_name)
    if normalized_constraint_name in _EMAIL_UNIQUE_CONSTRAINT_TOKENS:
        return _build_duplicate_conflict(reason=_DUPLICATE_EMAIL_REASON)
    if normalized_constraint_name in _PHONE_UNIQUE_CONSTRAINT_TOKENS:
        return _build_duplicate_conflict(reason=_DUPLICATE_PHONE_REASON)
    return _build_duplicate_conflict(reason=_DUPLICATE_EMAIL_OR_PHONE_REASON)


def _parse_utc_iso(*, value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _utc_iso_value(value: object) -> str:
    assert isinstance(value, datetime)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_utc_iso_value(value: object) -> str | None:
    if value is None:
        return None
    return _utc_iso_value(value)


def _row_to_delegation_record(*, row: tuple[object, ...]) -> DelegationRecord:
    return DelegationRecord(
        delegation_id=UUID(str(row[0])),
        principal_user_id=UUID(str(row[1])),
        delegate_user_id=UUID(str(row[2])),
        granted_at=_utc_iso_value(row[3]),
        revoked_at=_optional_utc_iso_value(row[4]),
        is_active=bool(row[5]),
        created_at=_utc_iso_value(row[6]),
    )


def _delegation_persistence_unavailable() -> DelegationPersistenceError:
    return DelegationPersistenceError(
        status_code=503,
        error_code="auth_delegation_persistence_unavailable",
        message="Auth delegation persistence is unavailable.",
        reason="auth_delegation_persistence_unavailable",
    )


def _delegation_missing_state() -> DelegationPersistenceError:
    return DelegationPersistenceError(
        status_code=503,
        error_code="auth_delegation_persistence_missing_state",
        message="Required auth delegation persistence state is missing.",
        reason="auth_delegation_persistence_missing_state",
    )


def _map_persistent_delegation_conflict(
    *,
    error: psycopg.errors.UniqueViolation,
) -> DelegationConflictError:
    constraint_name = getattr(error.diag, "constraint_name", "") or ""
    normalized_constraint_name = str(constraint_name)
    if "uq_delegations_active_pair" in normalized_constraint_name:
        return _build_delegation_conflict(
            reason=_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON
        )
    if "principal_user_id" in normalized_constraint_name:
        return _build_delegation_conflict(
            reason=_DELEGATION_INVALID_PAIR_REASON
        )
    signature = _build_error_signature(error=error)
    if "uq_delegations_active_pair" in signature:
        return _build_delegation_conflict(
            reason=_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON
        )
    return _build_delegation_conflict(
        reason=_DELEGATION_ACTIVE_PAIR_CONFLICT_REASON
    )


def _build_delegation_conflict(*, reason: str) -> DelegationConflictError:
    return DelegationConflictError(
        error_code=reason,
        message=_DELEGATION_CONFLICT_MESSAGE,
        reason=reason,
    )


_REGISTRATION_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "users": (
        "id",
        "email_encrypted",
        "phone_number_encrypted",
        "kra_pin_encrypted",
        "password_hash",
        "password_history_hashes",
        "role",
        "created_at",
        "account_state",
        "verification_state",
        "verified_at",
        "credentials_invalidated_at",
        "deletion_lifecycle_state",
        "anonymized_at",
    ),
}

_DELEGATION_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "delegations": (
        "id",
        "principal_user_id",
        "delegate_user_id",
        "granted_at",
        "revoked_at",
        "is_active",
        "created_at",
    ),
}


def build_default_registration_store() -> RegistrationStoreProtocol:
    """Build the auth registration store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryRegistrationStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableRegistrationStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentRegistrationStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableRegistrationStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableRegistrationStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_registration_store: RegistrationStoreProtocol = (
    build_default_registration_store()
)


def get_default_registration_store() -> RegistrationStoreProtocol:
    """Return deterministic process-local registration store instance."""

    return _default_registration_store


def reset_default_registration_store() -> None:
    """Reset process-local registration store for isolated tests."""

    global _default_registration_store
    _default_registration_store = build_default_registration_store()


def build_default_delegation_store() -> DelegationStoreProtocol:
    """Build the auth delegation store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryDelegationStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableDelegationStore(
            status_code=503,
            error_code="auth_delegation_persistence_unavailable",
            message="Auth delegation persistence is unavailable.",
            reason="auth_delegation_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentDelegationStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableDelegationStore(
            status_code=500,
            error_code="auth_delegation_persistence_schema_mismatch",
            message=(
                "Auth delegation persistence schema is not aligned with runtime requirements."
            ),
            reason="auth_delegation_persistence_schema_mismatch",
        )
    return UnavailableDelegationStore(
        status_code=503,
        error_code="auth_delegation_persistence_unavailable",
        message="Auth delegation persistence is unavailable.",
        reason="auth_delegation_persistence_unavailable",
    )


_default_delegation_store: DelegationStoreProtocol = build_default_delegation_store()


def get_default_delegation_store() -> DelegationStoreProtocol:
    """Return deterministic process-local delegation store instance."""

    return _default_delegation_store


def reset_default_delegation_store() -> None:
    """Reset process-local delegation store for isolated tests."""

    global _default_delegation_store
    _default_delegation_store = build_default_delegation_store()


def parse_registration_request(payload: object) -> RegistrationRequestRecord:
    """Parse and validate raw registration payload deterministically."""

    if not isinstance(payload, Mapping):
        raise RegistrationValidationError(
            error_code="registration_invalid_request",
            message="Invalid registration request payload.",
            reason="registration_invalid_request",
        )

    payload_map = cast(Mapping[str, object], payload)

    email = _require_string_field(payload=payload_map, field_name="email")
    phone_number = _require_string_field(
        payload=payload_map, field_name="phone_number"
    )
    kra_pin = _require_kra_pin_field(payload=payload_map)
    password = _require_string_field(payload=payload_map, field_name="password")
    role = _require_string_field(payload=payload_map, field_name="role")

    email_normalized = email.strip().lower()
    if _EMAIL_PATTERN.fullmatch(email_normalized) is None:
        raise RegistrationValidationError(
            error_code="registration_invalid_email",
            message="Registration email format is invalid.",
            reason="registration_invalid_email",
        )

    phone_number_normalized = normalize_phone_number(phone_number)
    if not phone_number_normalized:
        raise RegistrationValidationError(
            error_code="registration_invalid_phone",
            message="Registration phone-number format is invalid.",
            reason="registration_invalid_phone",
        )

    validate_password_policy(password)

    if role not in ALLOWED_AUTH_ROLES:
        raise RegistrationValidationError(
            error_code="registration_invalid_role",
            message="Registration role is unsupported for auth baseline.",
            reason="registration_invalid_role",
        )

    return RegistrationRequestRecord(
        email_normalized=email_normalized,
        phone_number_normalized=phone_number_normalized,
        kra_pin_normalized=kra_pin,
        password=password,
        role=role,
    )


def register_user(
    *,
    request_record: RegistrationRequestRecord,
    registration_store: RegistrationStoreProtocol,
) -> RegistrationSuccessEnvelope:
    """Persist deterministic registration record and build success response."""

    if not isinstance(registration_store, PersistentRegistrationStore):
        pre_insert_conflict = _detect_duplicate_conflict(
            email_normalized=request_record.email_normalized,
            phone_number_normalized=request_record.phone_number_normalized,
            registration_store=registration_store,
        )
        if pre_insert_conflict is not None:
            raise pre_insert_conflict

    created_at = _utc_now_iso()
    kra_pin_hash = _build_kra_pin_hash(
        kra_pin_normalized=request_record.kra_pin_normalized
    )
    password_hash = build_password_hash(password=request_record.password)
    try:
        created_record = registration_store.register_user(
            email_normalized=request_record.email_normalized,
            phone_number_normalized=request_record.phone_number_normalized,
            kra_pin_hash=kra_pin_hash,
            password_hash=password_hash,
            role=request_record.role,
            created_at=created_at,
        )
    except RegistrationConflictError as error:
        conflict_error = _resolve_registration_conflict_error(
            email_normalized=request_record.email_normalized,
            phone_number_normalized=request_record.phone_number_normalized,
            registration_store=registration_store,
            fallback_error=error,
        )
        raise conflict_error from error
    except Exception as error:
        mapped_conflict = _map_integrity_conflict(error=error)
        if mapped_conflict is not None:
            raise mapped_conflict from error
        raise

    return RegistrationSuccessEnvelope(
        user_id=created_record.user_id,
        registration_status="pending_verification",
        created_at=created_record.created_at,
    )


def _detect_duplicate_conflict(
    *,
    email_normalized: str,
    phone_number_normalized: str,
    registration_store: RegistrationStoreProtocol,
) -> RegistrationConflictError | None:
    email_exists = (
        registration_store.get_user_by_email(email_normalized=email_normalized)
        is not None
    )
    phone_exists = (
        registration_store.get_user_by_phone(
            phone_number_normalized=phone_number_normalized
        )
        is not None
    )
    if email_exists and phone_exists:
        return _build_duplicate_conflict(
            reason=_DUPLICATE_EMAIL_OR_PHONE_REASON
        )
    if email_exists:
        return _build_duplicate_conflict(reason=_DUPLICATE_EMAIL_REASON)
    if phone_exists:
        return _build_duplicate_conflict(reason=_DUPLICATE_PHONE_REASON)
    return None


def _resolve_registration_conflict_error(
    *,
    email_normalized: str,
    phone_number_normalized: str,
    registration_store: RegistrationStoreProtocol,
    fallback_error: RegistrationConflictError,
) -> RegistrationConflictError:
    post_insert_conflict = _detect_duplicate_conflict(
        email_normalized=email_normalized,
        phone_number_normalized=phone_number_normalized,
        registration_store=registration_store,
    )
    if post_insert_conflict is not None:
        return post_insert_conflict
    mapped_integrity_conflict = _map_integrity_conflict(error=fallback_error)
    if mapped_integrity_conflict is not None:
        return mapped_integrity_conflict
    if fallback_error.reason in {
        _DUPLICATE_EMAIL_REASON,
        _DUPLICATE_PHONE_REASON,
        _DUPLICATE_EMAIL_OR_PHONE_REASON,
    }:
        return RegistrationConflictError(
            error_code=fallback_error.error_code,
            message=_DUPLICATE_CONFLICT_MESSAGE,
            reason=fallback_error.reason,
        )
    return _build_duplicate_conflict(reason=_DUPLICATE_EMAIL_OR_PHONE_REASON)


def _map_integrity_conflict(
    *, error: Exception
) -> RegistrationConflictError | None:
    constraint_name = _extract_constraint_name(error=error)
    signature = _build_error_signature(error=error)
    email_hit = _signature_contains_any(
        signature=signature,
        candidates=_EMAIL_UNIQUE_CONSTRAINT_TOKENS,
    ) or _constraint_matches_any(
        constraint_name=constraint_name,
        candidates=_EMAIL_UNIQUE_CONSTRAINT_TOKENS,
    )
    phone_hit = _signature_contains_any(
        signature=signature,
        candidates=_PHONE_UNIQUE_CONSTRAINT_TOKENS,
    ) or _constraint_matches_any(
        constraint_name=constraint_name,
        candidates=_PHONE_UNIQUE_CONSTRAINT_TOKENS,
    )
    if email_hit and phone_hit:
        return _build_duplicate_conflict(
            reason=_DUPLICATE_EMAIL_OR_PHONE_REASON
        )
    if email_hit:
        return _build_duplicate_conflict(reason=_DUPLICATE_EMAIL_REASON)
    if phone_hit:
        return _build_duplicate_conflict(reason=_DUPLICATE_PHONE_REASON)
    return None


def _extract_constraint_name(*, error: Exception) -> str:
    candidate = getattr(error, "constraint_name", None)
    if isinstance(candidate, str):
        return candidate.strip().lower()

    diag = getattr(error, "diag", None)
    diag_candidate = getattr(diag, "constraint_name", None)
    if isinstance(diag_candidate, str):
        return diag_candidate.strip().lower()
    return ""


def _build_error_signature(*, error: Exception) -> str:
    message_parts = [str(error)]
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        message_parts.append(str(cause))
    return " ".join(part.strip().lower() for part in message_parts if part)


def _signature_contains_any(
    *, signature: str, candidates: tuple[str, ...]
) -> bool:
    return any(token in signature for token in candidates)


def _constraint_matches_any(
    *, constraint_name: str, candidates: tuple[str, ...]
) -> bool:
    if not constraint_name:
        return False
    return any(token in constraint_name for token in candidates)


def _build_duplicate_conflict(*, reason: str) -> RegistrationConflictError:
    return RegistrationConflictError(
        error_code=reason,
        message=_DUPLICATE_CONFLICT_MESSAGE,
        reason=reason,
    )


def build_password_hash(*, password: str) -> str:
    """Return bcrypt hash for password using governed minimum cost."""

    bcrypt_cost = get_auth_password_bcrypt_cost()
    generated_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=bcrypt_cost),
    )
    return generated_hash.decode("utf-8")


def verify_password_against_hash(*, password: str, password_hash: str) -> bool:
    """Verify password against bcrypt or legacy sha256 hash deterministically."""

    normalized_hash = password_hash.strip()
    if not normalized_hash:
        return False
    if _is_bcrypt_hash(normalized_hash):
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                normalized_hash.encode("utf-8"),
            )
        except ValueError:
            return False

    # Burn equivalent bcrypt work for legacy hashes to reduce timing drift i
    #   during migration.
    bcrypt.checkpw(
        password.encode("utf-8"),
        _LEGACY_VERIFY_DUMMY_BCRYPT_HASH.encode("utf-8"),
    )
    legacy_hash = sha256(password.encode("utf-8")).hexdigest()
    return compare_digest(legacy_hash, normalized_hash)


def is_legacy_password_hash(*, password_hash: str) -> bool:
    """Return whether hash matches deterministic legacy sha256 format."""

    normalized_hash = password_hash.strip().lower()
    return _LEGACY_SHA256_PATTERN.fullmatch(normalized_hash) is not None


def is_supported_password_hash(*, password_hash: str) -> bool:
    """Return whether password hash format is supported by verification policy."""

    normalized_hash = password_hash.strip()
    if not normalized_hash:
        return False
    return _is_bcrypt_hash(normalized_hash) or is_legacy_password_hash(
        password_hash=normalized_hash
    )


def _is_bcrypt_hash(password_hash: str) -> bool:
    return password_hash.startswith(_BCRYPT_HASH_PREFIXES)


def validate_password_policy(password: str) -> None:
    """Validate password against baseline policy or raise deterministic validation error."""

    if not _is_strong_password(password):
        raise RegistrationValidationError(
            error_code="registration_weak_password",
            message=(
                "Registration password does not meet policy: minimum length 12 with "
                "uppercase, lowercase, digit, and symbol."
            ),
            reason="registration_weak_password",
        )


def _resolve_verification_state(
    *,
    previous_state: Literal["pending_verification", "verified"],
    requested_state: AccountState,
) -> Literal["pending_verification", "verified"]:
    if requested_state == "active":
        return "verified"
    return previous_state


def _require_string_field(
    *, payload: Mapping[str, object], field_name: str
) -> str:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, str):
        raise RegistrationValidationError(
            error_code="registration_invalid_request",
            message="Invalid registration request payload.",
            reason="registration_invalid_request",
            details={"field": field_name},
        )
    normalized = raw_value.strip()
    if not normalized:
        raise RegistrationValidationError(
            error_code="registration_invalid_request",
            message="Invalid registration request payload.",
            reason="registration_invalid_request",
            details={"field": field_name},
        )
    return normalized


def _require_kra_pin_field(*, payload: Mapping[str, object]) -> str:
    raw_kra_pin = payload.get("kra_pin")
    if raw_kra_pin is None:
        raise RegistrationValidationError(
            error_code="registration_missing_kra_pin",
            message="Registration KRA PIN is required.",
            reason="registration_missing_kra_pin",
        )
    if not isinstance(raw_kra_pin, str):
        raise RegistrationValidationError(
            error_code="registration_invalid_kra_pin",
            message="Registration KRA PIN format is invalid.",
            reason="registration_invalid_kra_pin",
        )
    normalized_kra_pin = raw_kra_pin.strip()
    if not normalized_kra_pin:
        raise RegistrationValidationError(
            error_code="registration_missing_kra_pin",
            message="Registration KRA PIN is required.",
            reason="registration_missing_kra_pin",
        )
    if _KRA_PIN_PATTERN.fullmatch(normalized_kra_pin) is None:
        raise RegistrationValidationError(
            error_code="registration_invalid_kra_pin",
            message="Registration KRA PIN format is invalid.",
            reason="registration_invalid_kra_pin",
        )
    return normalized_kra_pin


def normalize_phone_number(phone_number: str) -> str:
    cleaned = _PHONE_CLEAN_PATTERN.sub("", phone_number.strip())
    if not cleaned:
        return ""
    national_number: str
    if cleaned.startswith("+254"):
        national_number = cleaned[4:]
    elif cleaned.startswith("254"):
        national_number = cleaned[3:]
    elif cleaned.startswith("0"):
        national_number = cleaned[1:]
    else:
        return ""
    if _KENYA_NATIONAL_PHONE_PATTERN.fullmatch(national_number) is None:
        return ""
    return f"+254{national_number}"


def _is_strong_password(password: str) -> bool:
    if len(password) < MIN_PASSWORD_LENGTH:
        return False
    has_upper = any(character.isupper() for character in password)
    has_lower = any(character.islower() for character in password)
    has_digit = any(character.isdigit() for character in password)
    has_symbol = any(not character.isalnum() for character in password)
    return has_upper and has_lower and has_digit and has_symbol


def _build_tombstoned_phone_number(*, user_id: UUID) -> str:
    digest_value = int(sha256(user_id.hex.encode("utf-8")).hexdigest()[:16], 16)
    suffix = str(digest_value % 10_000_000_000_000).zfill(13)
    return f"+9{suffix}"


def _build_kra_pin_hash(*, kra_pin_normalized: str) -> str:
    return sha256(kra_pin_normalized.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
