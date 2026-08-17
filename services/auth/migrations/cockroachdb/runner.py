"""Run the auth-only CockroachDB migration lane for Kodi Solutions AI Platform."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import LiteralString
from typing import cast

import psycopg
from psycopg import sql

from services.auth.app.persistence_support import load_auth_database_url

AUTH_DATABASE_NAME = "kodi_dev"
AUTH_DATABASE_ENGINE_TOKEN = "CockroachDB"
AUTH_PUBLIC_SCHEMA = "public"
MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent
MIGRATION_LEDGER_TABLE = "auth_cockroachdb_schema_migrations"
MIGRATION_LEDGER_COLUMNS = (
    "migration_name",
    "checksum_sha256",
    "applied_at",
)


@dataclass(frozen=True)
class ColumnExpectation:
    """Represent one required column contract."""

    types: tuple[str, ...]
    nullable: bool


@dataclass(frozen=True)
class ForeignKeyExpectation:
    """Represent one required foreign-key contract."""

    column: str
    referenced_table: str
    referenced_column: str


@dataclass(frozen=True)
class ConstraintExpectation:
    """Represent one required constraint contract."""

    constraint_type: str
    columns: tuple[str, ...] | None = None
    check_fragment: str | None = None
    referenced_table: str | None = None
    referenced_column: str | None = None


@dataclass(frozen=True)
class IndexExpectation:
    """Represent one required index contract."""

    index_name: str
    fragment: str | None = None


@dataclass(frozen=True)
class TableExpectation:
    """Represent one required table contract."""

    columns: dict[str, ColumnExpectation]
    primary_key: tuple[str, ...] | None = None
    constraints: dict[str, ConstraintExpectation] | None = None
    indexes: tuple[IndexExpectation, ...] = ()
    foreign_keys: dict[str, ForeignKeyExpectation] | None = None


@dataclass(frozen=True)
class MigrationExpectation:
    """Represent one migration's validation boundary."""

    tables: tuple[str, ...]


class AuthMigrationError(RuntimeError):
    """Represent a sanitized auth migration failure."""


class AuthSchemaMismatchError(AuthMigrationError):
    """Represent a sanitized auth schema mismatch."""


class AuthTargetError(AuthMigrationError):
    """Represent a sanitized target-database mismatch."""


TABLE_EXPECTATIONS: dict[str, TableExpectation] = {
    "auth_cockroachdb_schema_migrations": TableExpectation(
        columns={
            "migration_name": ColumnExpectation(types=("text", "string"), nullable=False),
            "checksum_sha256": ColumnExpectation(types=("text", "string"), nullable=False),
            "applied_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("migration_name",),
    ),
    "users": TableExpectation(
        columns={
            "id": ColumnExpectation(types=("uuid",), nullable=False),
            "phone_number_encrypted": ColumnExpectation(types=("text", "string"), nullable=False),
            "email_encrypted": ColumnExpectation(types=("text", "string"), nullable=False),
            "kra_pin_encrypted": ColumnExpectation(types=("text", "string"), nullable=True),
            "role": ColumnExpectation(types=("text", "string"), nullable=False),
            "subscription_tier": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "updated_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "is_locked": ColumnExpectation(types=("boolean", "bool"), nullable=False),
            "lock_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "password_hash": ColumnExpectation(types=("text", "string"), nullable=True),
            "password_history_hashes": ColumnExpectation(types=("jsonb",), nullable=False),
            "account_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "verification_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "verified_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "credentials_invalidated_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "deletion_lifecycle_state": ColumnExpectation(
                types=("text", "string"),
                nullable=False,
            ),
            "anonymized_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
        },
        primary_key=("id",),
        constraints={
            "uq_users_phone_number_encrypted": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("phone_number_encrypted",),
            ),
            "uq_users_email_encrypted": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("email_encrypted",),
            ),
            "chk_users_exactly_one_role": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')",
            ),
            "chk_users_account_state_allowed": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="account_state IN ('pending_verification', 'active', 'locked', 'disabled')",
            ),
            "chk_users_verification_state_allowed": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="verification_state IN ('pending_verification', 'verified')",
            ),
            "chk_users_deletion_lifecycle_state_allowed": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="deletion_lifecycle_state IN ('none', 'tombstoned')",
            ),
        },
    ),
    "sessions": TableExpectation(
        columns={
            "id": ColumnExpectation(types=("uuid",), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "idempotency_key": ColumnExpectation(types=("text", "string"), nullable=False),
            "issued_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "inactivity_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "last_activity_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "role": ColumnExpectation(types=("text", "string"), nullable=False),
            "device_fingerprint_hash": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "is_invalidated": ColumnExpectation(types=("boolean", "bool"), nullable=False),
            "invalidated_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "invalidated_reason": ColumnExpectation(types=("text", "string"), nullable=True),
            "access_token_hash": ColumnExpectation(types=("text", "string"), nullable=True),
            "refresh_token_hash": ColumnExpectation(types=("text", "string"), nullable=True),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("id",),
        constraints={
            "uq_sessions_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("idempotency_key",),
            ),
            "chk_sessions_expires_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="expires_at > issued_at",
            ),
            "chk_sessions_inactivity_expires_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="inactivity_expires_at > issued_at",
            ),
            "chk_sessions_last_activity_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="last_activity_at >= issued_at",
            ),
            "chk_sessions_invalidated_at_consistency": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment=(
                    "is_invalidated = FALSE"
                    " AND invalidated_at IS NULL"
                    " AND invalidated_reason IS NULL"
                ),
            ),
            "chk_sessions_role_allowed": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')",
            ),
        },
        foreign_keys={
            "sessions_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_sessions_user_issued_at",
                fragment="(user_id, issued_at, id)",
            ),
            IndexExpectation(
                index_name="idx_sessions_access_token_hash",
                fragment="access_token_hash",
            ),
            IndexExpectation(
                index_name="idx_sessions_refresh_token_hash",
                fragment="refresh_token_hash",
            ),
        ),
    ),
    "delegations": TableExpectation(
        columns={
            "id": ColumnExpectation(types=("uuid",), nullable=False),
            "principal_user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "delegate_user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "granted_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "revoked_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "is_active": ColumnExpectation(types=("boolean", "bool"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("id",),
        constraints={
            "chk_delegations_distinct_users": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="principal_user_id <> delegate_user_id",
            ),
            "chk_delegations_revoked_after_granted": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="revoked_at IS NULL OR revoked_at >= granted_at",
            ),
            "chk_delegations_active_revocation_consistency": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="is_active = TRUE AND revoked_at IS NULL",
            ),
        },
        foreign_keys={
            "delegations_principal_user_id_fkey": ForeignKeyExpectation(
                column="principal_user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "delegations_delegate_user_id_fkey": ForeignKeyExpectation(
                column="delegate_user_id",
                referenced_table="users",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="uq_delegations_active_pair",
                fragment="WHERE is_active",
            ),
        ),
    ),
    "auth_session_refresh_tokens": TableExpectation(
        columns={
            "refresh_token_hash": ColumnExpectation(types=("text", "string"), nullable=False),
            "session_id": ColumnExpectation(types=("uuid",), nullable=False),
            "issued_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "is_consumed": ColumnExpectation(types=("boolean", "bool"), nullable=False),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
        },
        primary_key=("refresh_token_hash",),
        constraints={
            "chk_auth_session_refresh_tokens_consumed_consistency": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="is_consumed = FALSE AND consumed_at IS NULL",
            ),
        },
        foreign_keys={
            "auth_session_refresh_tokens_session_id_fkey": ForeignKeyExpectation(
                column="session_id",
                referenced_table="sessions",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="uq_auth_session_refresh_tokens_active_session",
                fragment="WHERE is_consumed = FALSE",
            ),
            IndexExpectation(
                index_name="idx_auth_session_refresh_tokens_session_id",
                fragment="(session_id)",
            ),
        ),
    ),
    "auth_login_lockouts": TableExpectation(
        columns={
            "login_id_normalized": ColumnExpectation(types=("text", "string"), nullable=False),
            "source_ip": ColumnExpectation(types=("text", "string"), nullable=False),
            "failed_attempt_count": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "last_failed_attempt_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "lockout_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "updated_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("login_id_normalized", "source_ip"),
        constraints={
            "chk_auth_login_lockouts_failed_attempt_count_non_negative": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="failed_attempt_count >= 0",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_login_lockouts_expires_at",
                fragment="lockout_expires_at",
            ),
        ),
    ),
    "auth_idempotency_preclaims": TableExpectation(
        columns={
            "scope": ColumnExpectation(types=("text", "string"), nullable=False),
            "idempotency_key": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_fingerprint": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("scope", "idempotency_key"),
    ),
    "auth_otp_challenges": TableExpectation(
        columns={
            "challenge_id": ColumnExpectation(types=("uuid",), nullable=False),
            "channel": ColumnExpectation(types=("text", "string"), nullable=False),
            "purpose": ColumnExpectation(types=("text", "string"), nullable=False),
            "subject_normalized": ColumnExpectation(types=("text", "string"), nullable=False),
            "otp_code": ColumnExpectation(types=("text", "string"), nullable=False),
            "issued_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "failed_attempt_count": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "max_attempts": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "cooldown_seconds": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "cooldown_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "idempotency_key": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_fingerprint": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("challenge_id",),
        constraints={
            "ck_auth_otp_challenges_channel": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="channel IN ('sms', 'email')",
            ),
            "ck_auth_otp_challenges_expires_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="expires_at > issued_at",
            ),
            "ck_auth_otp_challenges_consumed_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="consumed_at IS NULL OR consumed_at >= issued_at",
            ),
            "ck_auth_otp_challenges_cooldown_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="cooldown_expires_at IS NULL OR cooldown_expires_at >= issued_at",
            ),
            "ck_auth_otp_challenges_failed_attempt_count_non_negative": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="failed_attempt_count >= 0",
            ),
            "ck_auth_otp_challenges_max_attempts_positive": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="max_attempts > 0",
            ),
            "uq_auth_otp_challenges_channel_idempotency": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("channel", "idempotency_key"),
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_otp_challenges_subject_issue",
                fragment="(channel, purpose, subject_normalized, issued_at)",
            ),
            IndexExpectation(
                index_name="idx_auth_otp_challenges_cooldown",
                fragment="(channel, purpose, subject_normalized, cooldown_expires_at)",
            ),
        ),
    ),
    "auth_password_reset_challenges": TableExpectation(
        columns={
            "challenge_id": ColumnExpectation(types=("uuid",), nullable=False),
            "purpose": ColumnExpectation(types=("text", "string"), nullable=False),
            "channel": ColumnExpectation(types=("text", "string"), nullable=False),
            "subject_normalized": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=True),
            "reset_code": ColumnExpectation(types=("text", "string"), nullable=False),
            "issued_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "failed_attempt_count": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "max_attempts": ColumnExpectation(types=("integer", "int4", "int"), nullable=False),
            "idempotency_key": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_fingerprint": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("challenge_id",),
        constraints={
            "uq_auth_password_reset_challenges_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("idempotency_key",),
            ),
            "ck_auth_password_reset_channel": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="channel IN ('email', 'sms')",
            ),
            "ck_auth_password_reset_expires_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="expires_at > issued_at",
            ),
            "ck_auth_password_reset_consumed_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="consumed_at IS NULL OR consumed_at >= issued_at",
            ),
            "ck_auth_password_reset_failed_attempt_count_non_negative": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="failed_attempt_count >= 0",
            ),
            "ck_auth_password_reset_max_attempts_positive": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="max_attempts > 0",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_password_reset_subject_issue",
                fragment="(channel, subject_normalized, issued_at)",
            ),
        ),
    ),
    "auth_login_step_up_states": TableExpectation(
        columns={
            "login_id_normalized": ColumnExpectation(types=("text", "string"), nullable=False),
            "source_ip": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "challenge_id": ColumnExpectation(types=("uuid",), nullable=False),
            "challenge_channel": ColumnExpectation(types=("text", "string"), nullable=False),
            "issued_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "challenge_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "updated_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("login_id_normalized", "source_ip"),
        constraints={
            "ck_auth_login_step_up_states_channel_allowed": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="challenge_channel IN ('email', 'sms')",
            ),
            "ck_auth_login_step_up_states_expires_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="challenge_expires_at > issued_at",
            ),
            "ck_auth_login_step_up_states_consumed_after_issue": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="consumed_at IS NULL OR consumed_at >= issued_at",
            ),
            "uq_auth_login_step_up_states_challenge_id": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("challenge_id",),
            ),
        },
        foreign_keys={
            "auth_login_step_up_states_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_login_step_up_states_challenge_id_fkey": ForeignKeyExpectation(
                column="challenge_id",
                referenced_table="auth_otp_challenges",
                referenced_column="challenge_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_login_step_up_states_user_id",
                fragment="(user_id, challenge_expires_at, updated_at)",
            ),
            IndexExpectation(
                index_name="idx_auth_login_step_up_states_expires_at",
                fragment="(challenge_expires_at, updated_at)",
            ),
        ),
    ),
    "auth_phone_change_requests": TableExpectation(
        columns={
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "requested_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "current_phone_number_normalized": ColumnExpectation(
                types=("text", "string"),
                nullable=False,
            ),
            "new_phone_number_normalized": ColumnExpectation(
                types=("text", "string"),
                nullable=False,
            ),
            "phone_change_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "step_up_challenge_id": ColumnExpectation(types=("uuid",), nullable=False),
            "step_up_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "request_idempotency_key": ColumnExpectation(
                types=("text", "string"),
                nullable=False,
            ),
            "request_fingerprint": ColumnExpectation(types=("text", "string"), nullable=False),
            "confirmed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "confirm_idempotency_key": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "confirm_request_fingerprint": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("request_id",),
        constraints={
            "uq_auth_phone_change_requests_request_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("request_idempotency_key",),
            ),
            "uq_auth_phone_change_requests_confirm_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("confirm_idempotency_key",),
            ),
            "ck_auth_phone_change_state": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment=(
                    "phone_change_state IN ('pending_confirmation', 'superseded', 'confirmed')"
                ),
            ),
        },
        foreign_keys={
            "auth_phone_change_requests_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_phone_change_requests_step_up_challenge_id_fkey": ForeignKeyExpectation(
                column="step_up_challenge_id",
                referenced_table="auth_otp_challenges",
                referenced_column="challenge_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_phone_change_requests_user_requested",
                fragment="(user_id, requested_at, created_at)",
            ),
            IndexExpectation(
                index_name="uq_auth_phone_change_requests_pending_user",
                fragment="WHERE phone_change_state = 'pending_confirmation'",
            ),
        ),
    ),
    "auth_phone_change_audit_events": TableExpectation(
        columns={
            "audit_evidence_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "event_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "event_type": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "phone_change_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "occurred_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "correlation_id": ColumnExpectation(types=("text", "string"), nullable=True),
            "trace_ref": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("audit_evidence_id",),
        constraints={
        },
        foreign_keys={
            "auth_phone_change_audit_events_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_phone_change_audit_events_request_id_fkey": ForeignKeyExpectation(
                column="request_id",
                referenced_table="auth_phone_change_requests",
                referenced_column="request_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_phone_change_audit_user_time",
                fragment="(user_id, occurred_at, created_at)",
            ),
            IndexExpectation(
                index_name="uq_auth_phone_change_audit_events_event_id",
                fragment="(event_id)",
            ),
        ),
    ),
    "auth_account_deletion_requests": TableExpectation(
        columns={
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_reason": ColumnExpectation(types=("text", "string"), nullable=False),
            "requested_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "deletion_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "blocker_reasons": ColumnExpectation(types=("jsonb",), nullable=False),
            "request_idempotency_key": ColumnExpectation(
                types=("text", "string"),
                nullable=False,
            ),
            "request_fingerprint": ColumnExpectation(types=("text", "string"), nullable=False),
            "confirmed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "cooldown_expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "executed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "execution_outcome": ColumnExpectation(types=("text", "string"), nullable=True),
            "revoked_session_count": ColumnExpectation(types=("integer", "int4", "int"), nullable=True),
            "confirm_idempotency_key": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "confirm_request_fingerprint": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "cancel_idempotency_key": ColumnExpectation(types=("text", "string"), nullable=True),
            "cancel_request_fingerprint": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "execute_idempotency_key": ColumnExpectation(types=("text", "string"), nullable=True),
            "execute_request_fingerprint": ColumnExpectation(
                types=("text", "string"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("request_id",),
        constraints={
            "uq_auth_account_deletion_requests_request_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("request_idempotency_key",),
            ),
            "uq_auth_account_deletion_requests_confirm_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("confirm_idempotency_key",),
            ),
            "uq_auth_account_deletion_requests_cancel_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("cancel_idempotency_key",),
            ),
            "uq_auth_account_deletion_requests_execute_idempotency_key": ConstraintExpectation(
                constraint_type="UNIQUE",
                columns=("execute_idempotency_key",),
            ),
            "ck_auth_account_deletion_state": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment=(
                    "deletion_state IN ('requested', 'blocked', 'confirmed', 'cancelled', 'executed')"
                ),
            ),
            "ck_auth_account_deletion_execution_outcome": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="execution_outcome IS NULL OR execution_outcome IN ('tombstoned')",
            ),
            "ck_auth_account_deletion_revoked_session_count_non_negative": ConstraintExpectation(
                constraint_type="CHECK",
                check_fragment="revoked_session_count IS NULL OR revoked_session_count >= 0",
            ),
        },
        foreign_keys={
            "auth_account_deletion_requests_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_requests_user_requested",
                fragment="(user_id, requested_at, created_at)",
            ),
            IndexExpectation(
                index_name="uq_auth_account_deletion_requests_active_user",
                fragment="WHERE deletion_state IN ('requested', 'blocked', 'confirmed')",
            ),
        ),
    ),
    "auth_account_deletion_audit_events": TableExpectation(
        columns={
            "audit_evidence_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "event_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "event_type": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "action": ColumnExpectation(types=("text", "string"), nullable=False),
            "action_status": ColumnExpectation(types=("text", "string"), nullable=False),
            "deletion_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "occurred_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "correlation_id": ColumnExpectation(types=("text", "string"), nullable=True),
            "blocker_reasons": ColumnExpectation(types=("jsonb",), nullable=False),
            "reason_code": ColumnExpectation(types=("text", "string"), nullable=True),
            "trace_ref": ColumnExpectation(types=("text", "string"), nullable=False),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("audit_evidence_id",),
        constraints={
        },
        foreign_keys={
            "auth_account_deletion_audit_events_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_account_deletion_audit_events_request_id_fkey": ForeignKeyExpectation(
                column="request_id",
                referenced_table="auth_account_deletion_requests",
                referenced_column="request_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_audit_user_time",
                fragment="(user_id, occurred_at, created_at)",
            ),
            IndexExpectation(
                index_name="uq_auth_account_deletion_audit_events_event_id",
                fragment="(event_id)",
            ),
        ),
    ),
    "auth_account_deletion_notifications": TableExpectation(
        columns={
            "notification_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "channel": ColumnExpectation(types=("text", "string"), nullable=False),
            "status": ColumnExpectation(types=("text", "string"), nullable=False),
            "attempted_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "event_type": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "deletion_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "correlation_id": ColumnExpectation(types=("text", "string"), nullable=True),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("notification_id",),
        foreign_keys={
            "auth_account_deletion_notifications_request_id_fkey": ForeignKeyExpectation(
                column="request_id",
                referenced_table="auth_account_deletion_requests",
                referenced_column="request_id",
            ),
            "auth_account_deletion_notifications_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_notifications_user_time",
                fragment="(user_id, attempted_at, created_at)",
            ),
        ),
    ),
    "auth_account_deletion_incidents": TableExpectation(
        columns={
            "audit_reference_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "incident_code": ColumnExpectation(types=("text", "string"), nullable=False),
            "message": ColumnExpectation(types=("text", "string"), nullable=False),
            "reason": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "actor_user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "account_deletion_state": ColumnExpectation(types=("text", "string"), nullable=False),
            "occurred_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "correlation_id": ColumnExpectation(types=("text", "string"), nullable=True),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("audit_reference_id",),
        foreign_keys={
            "auth_account_deletion_incidents_actor_user_id_fkey": ForeignKeyExpectation(
                column="actor_user_id",
                referenced_table="users",
                referenced_column="id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_incidents_user_time",
                fragment="(actor_user_id, occurred_at, created_at)",
            ),
        ),
    ),
    "auth_account_deletion_reauth_proofs": TableExpectation(
        columns={
            "proof_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("proof_id",),
        foreign_keys={
            "auth_account_deletion_reauth_proofs_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_account_deletion_reauth_proofs_request_id_fkey": ForeignKeyExpectation(
                column="request_id",
                referenced_table="auth_account_deletion_requests",
                referenced_column="request_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_reauth_user_request",
                fragment="(user_id, request_id, expires_at)",
            ),
        ),
    ),
    "auth_account_deletion_otp_proofs": TableExpectation(
        columns={
            "otp_verification_id": ColumnExpectation(types=("uuid",), nullable=False),
            "user_id": ColumnExpectation(types=("uuid",), nullable=False),
            "tenant_id": ColumnExpectation(types=("text", "string"), nullable=False),
            "request_id": ColumnExpectation(types=("uuid",), nullable=False),
            "expires_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
            "consumed_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=True,
            ),
            "created_at": ColumnExpectation(
                types=("timestamp with time zone", "timestamptz"),
                nullable=False,
            ),
        },
        primary_key=("otp_verification_id",),
        foreign_keys={
            "auth_account_deletion_otp_proofs_user_id_fkey": ForeignKeyExpectation(
                column="user_id",
                referenced_table="users",
                referenced_column="id",
            ),
            "auth_account_deletion_otp_proofs_request_id_fkey": ForeignKeyExpectation(
                column="request_id",
                referenced_table="auth_account_deletion_requests",
                referenced_column="request_id",
            ),
        },
        indexes=(
            IndexExpectation(
                index_name="idx_auth_account_deletion_otp_user_request",
                fragment="(user_id, request_id, expires_at)",
            ),
        ),
    ),
}


MIGRATION_EXPECTATIONS: dict[str, MigrationExpectation] = {
    "0001_auth_core.sql": MigrationExpectation(
        tables=("users", "sessions", "delegations")
    ),
    "0002_auth_runtime.sql": MigrationExpectation(
        tables=("auth_session_refresh_tokens", "auth_login_lockouts")
    ),
    "0003_auth_challenges.sql": MigrationExpectation(
        tables=(
            "auth_idempotency_preclaims",
            "auth_otp_challenges",
            "auth_password_reset_challenges",
            "auth_login_step_up_states",
        )
    ),
    "0004_auth_lifecycle.sql": MigrationExpectation(
        tables=(
            "auth_phone_change_requests",
            "auth_phone_change_audit_events",
            "auth_account_deletion_requests",
            "auth_account_deletion_audit_events",
            "auth_account_deletion_notifications",
            "auth_account_deletion_incidents",
            "auth_account_deletion_reauth_proofs",
            "auth_account_deletion_otp_proofs",
        )
    ),
}


def discover_migration_files() -> tuple[Path, ...]:
    """Return auth CockroachDB migration files in lexical order."""

    files = [path for path in MIGRATIONS_DIRECTORY.glob("*.sql") if path.is_file()]
    return tuple(sorted(files, key=lambda item: item.name))


def apply_migrations(database_url: str, migration_files: tuple[Path, ...]) -> None:
    """Apply all auth CockroachDB migrations with checksum protection."""

    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            target = _validate_target_database(connection)
            print(f"Authentication migration target: {target}")
            _ensure_ledger_table(connection)
            for migration_file in migration_files:
                migration_name = migration_file.name
                sql_text = migration_file.read_text(encoding="utf-8")
                checksum_sha256 = _migration_checksum(sql_text)
                applied_checksum = _load_applied_checksum(
                    connection=connection,
                    migration_name=migration_name,
                )
                if applied_checksum is not None:
                    if applied_checksum != checksum_sha256:
                        raise AuthSchemaMismatchError(
                            f"Checksum mismatch for already applied migration {migration_name}."
                        )
                    print(f"{migration_name}: skipped")
                    continue

                _prevalidate_migration_schema(
                    connection=connection,
                    migration_name=migration_name,
                )

                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(cast(LiteralString, sql_text))
                    _validate_migration_schema(
                        connection=connection,
                        migration_name=migration_name,
                    )
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"""
                            INSERT INTO {MIGRATION_LEDGER_TABLE} (
                                migration_name,
                                checksum_sha256
                            )
                            VALUES (%s, %s)
                            """,
                            (migration_name, checksum_sha256),
                        )
                print(f"{migration_name}: applied")

            _validate_final_schema(connection=connection)
            print("Authentication schema validation: PASS")
    except AuthMigrationError:
        raise
    except psycopg.Error as error:
        raise AuthMigrationError("Authentication database connection failed.") from error


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CockroachDB auth migration runner."""

    parser = argparse.ArgumentParser(
        prog="python -m services.auth.migrations.cockroachdb.runner"
    )
    parser.parse_args([] if argv is None else argv)

    database_url = load_auth_database_url()
    if not database_url:
        print("Missing required DATABASE_URL environment variable.", file=sys.stderr)
        return 2

    migration_files = discover_migration_files()
    try:
        apply_migrations(database_url=database_url, migration_files=migration_files)
    except AuthTargetError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AuthSchemaMismatchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AuthMigrationError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def _ensure_ledger_table(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_LEDGER_TABLE} (
                migration_name TEXT PRIMARY KEY,
                checksum_sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def _load_applied_checksum(
    *,
    connection: psycopg.Connection[Any],
    migration_name: str,
) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT checksum_sha256
            FROM {MIGRATION_LEDGER_TABLE}
            WHERE migration_name = %s
            """,
            (migration_name,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


def _migration_checksum(sql_text: str) -> str:
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def _validate_target_database(connection: psycopg.Connection[Any]) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), version()")
        row = cursor.fetchone()
    if row is None:
        raise AuthTargetError("Unable to determine auth database target.")

    current_database = str(row[0] or "")
    version_text = str(row[1] or "")
    if current_database != AUTH_DATABASE_NAME:
        raise AuthTargetError(
            "Authentication migration target rejected: expected database kodi_dev."
        )
    if AUTH_DATABASE_ENGINE_TOKEN not in version_text:
        raise AuthTargetError(
            "Authentication migration target rejected: expected CockroachDB."
        )
    return current_database


def _prevalidate_migration_schema(
    *,
    connection: psycopg.Connection[Any],
    migration_name: str,
) -> None:
    for table_name in MIGRATION_EXPECTATIONS[migration_name].tables:
        _validate_existing_table_types(connection=connection, table_name=table_name)


def _validate_migration_schema(
    *,
    connection: psycopg.Connection[Any],
    migration_name: str,
) -> None:
    for table_name in MIGRATION_EXPECTATIONS[migration_name].tables:
        _validate_table_contract(connection=connection, table_name=table_name)


def _validate_final_schema(*, connection: psycopg.Connection[Any]) -> None:
    _validate_table_contract(connection=connection, table_name=MIGRATION_LEDGER_TABLE)
    for table_name in (
        "users",
        "sessions",
        "delegations",
        "auth_session_refresh_tokens",
        "auth_login_lockouts",
        "auth_idempotency_preclaims",
        "auth_otp_challenges",
        "auth_password_reset_challenges",
        "auth_login_step_up_states",
        "auth_phone_change_requests",
        "auth_phone_change_audit_events",
        "auth_account_deletion_requests",
        "auth_account_deletion_audit_events",
        "auth_account_deletion_notifications",
        "auth_account_deletion_incidents",
        "auth_account_deletion_reauth_proofs",
        "auth_account_deletion_otp_proofs",
    ):
        _validate_table_contract(connection=connection, table_name=table_name)


def _validate_existing_table_types(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> None:
    existing_columns = _load_columns(connection=connection, table_name=table_name)
    if not existing_columns:
        return
    table_expectation = TABLE_EXPECTATIONS[table_name]
    for column_name, column_expectation in table_expectation.columns.items():
        current = existing_columns.get(column_name)
        if current is None:
            continue
        current_type, is_nullable = current
        if _normalize_type(current_type) not in {
            _normalize_type(expected_type) for expected_type in column_expectation.types
        }:
            raise AuthSchemaMismatchError(
                f"Schema mismatch for table {table_name}: column {column_name} has type {current_type}."
            )
        if not column_expectation.nullable and is_nullable:
            continue


def _validate_table_contract(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> None:
    table_expectation = TABLE_EXPECTATIONS[table_name]
    existing_columns = _load_columns(connection=connection, table_name=table_name)
    if not existing_columns:
        raise AuthSchemaMismatchError(f"Required table {table_name} is missing.")

    for column_name, column_expectation in table_expectation.columns.items():
        current = existing_columns.get(column_name)
        if current is None:
            raise AuthSchemaMismatchError(
                f"Required column {table_name}.{column_name} is missing."
            )
        current_type, is_nullable = current
        if _normalize_type(current_type) not in {
            _normalize_type(expected_type) for expected_type in column_expectation.types
        }:
            raise AuthSchemaMismatchError(
                f"Schema mismatch for table {table_name}: column {column_name} has type {current_type}."
            )
        if column_expectation.nullable and not is_nullable:
            raise AuthSchemaMismatchError(
                f"Schema mismatch for table {table_name}: column {column_name} should be nullable."
            )
        if not column_expectation.nullable and is_nullable:
            raise AuthSchemaMismatchError(
                f"Schema mismatch for table {table_name}: column {column_name} should be NOT NULL."
            )

    _validate_constraints(connection=connection, table_name=table_name, table_expectation=table_expectation)
    _validate_indexes(connection=connection, table_name=table_name, table_expectation=table_expectation)


def _validate_constraints(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
    table_expectation: TableExpectation,
) -> None:
    constraints = _load_constraints(connection=connection, table_name=table_name)
    if table_expectation.primary_key is not None:
        primary_keys = [
            columns
            for _constraint_name, (constraint_type, _clause, columns) in constraints.items()
            if constraint_type == "PRIMARY KEY"
        ]
        if not primary_keys:
            raise AuthSchemaMismatchError(
                f"Required primary key is missing on table {table_name}."
            )
        if tuple(primary_keys[0]) != table_expectation.primary_key:
            raise AuthSchemaMismatchError(
                f"Primary key on table {table_name} has unexpected columns."
            )

    for constraint_name, expectation in (table_expectation.constraints or {}).items():
        constraint = constraints.get(constraint_name)
        if constraint is None:
            raise AuthSchemaMismatchError(
                f"Required constraint {constraint_name} is missing on table {table_name}."
            )
        constraint_type, clause, columns = constraint
        if constraint_type != expectation.constraint_type:
            raise AuthSchemaMismatchError(
                f"Constraint {constraint_name} on table {table_name} has type {constraint_type}."
            )
        if expectation.columns is not None and tuple(columns) != expectation.columns:
            raise AuthSchemaMismatchError(
                f"Constraint {constraint_name} on table {table_name} has unexpected columns."
            )
        if expectation.check_fragment is not None and _normalized_sql_fragment(
            expectation.check_fragment
        ) not in _normalized_sql_fragment(clause):
            raise AuthSchemaMismatchError(
                f"Constraint {constraint_name} on table {table_name} has an unexpected check clause."
            )

    foreign_keys = _load_foreign_keys(connection=connection, table_name=table_name)
    for constraint_name, expectation in (table_expectation.foreign_keys or {}).items():
        foreign_key = foreign_keys.get(constraint_name)
        if foreign_key is None:
            raise AuthSchemaMismatchError(
                f"Required foreign key {constraint_name} is missing on table {table_name}."
            )
        if foreign_key != (
            expectation.column,
            expectation.referenced_table,
            expectation.referenced_column,
        ):
            raise AuthSchemaMismatchError(
                f"Foreign key {constraint_name} on table {table_name} points to the wrong target."
            )


def _validate_indexes(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
    table_expectation: TableExpectation,
) -> None:
    ddl = _show_create_table(connection=connection, table_name=table_name)
    normalized_ddl = _normalized_sql_fragment(ddl)
    for index_expectation in table_expectation.indexes:
        if _normalized_sql_fragment(index_expectation.index_name) not in normalized_ddl:
            raise AuthSchemaMismatchError(
                f"Required index {index_expectation.index_name} is missing on table {table_name}."
            )
        if index_expectation.fragment is not None and _normalized_sql_fragment(
            index_expectation.fragment
        ) not in normalized_ddl:
            raise AuthSchemaMismatchError(
                f"Required index {index_expectation.index_name} has an unexpected definition on table {table_name}."
            )


def _load_columns(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> dict[str, tuple[str, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            """,
            (AUTH_PUBLIC_SCHEMA, table_name),
        )
        rows = cursor.fetchall()
    columns: dict[str, tuple[str, bool]] = {}
    for row in rows:
        column_name = str(row[0])
        data_type = str(row[1])
        is_nullable = str(row[2]).upper() == "YES"
        columns[column_name] = (data_type, is_nullable)
    return columns


def _load_constraints(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tc.constraint_name, tc.constraint_type, COALESCE(cc.check_clause, '')
            FROM information_schema.table_constraints AS tc
            LEFT JOIN information_schema.check_constraints AS cc
              ON cc.constraint_name = tc.constraint_name
             AND cc.constraint_schema = tc.constraint_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
            """,
            (AUTH_PUBLIC_SCHEMA, table_name),
        )
        rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT constraint_name, column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY constraint_name, ordinal_position
            """,
            (AUTH_PUBLIC_SCHEMA, table_name),
        )
        key_rows = cursor.fetchall()

    columns_by_constraint: dict[str, list[str]] = {}
    for row in key_rows:
        constraint_name = str(row[0])
        column_name = str(row[1])
        columns_by_constraint.setdefault(constraint_name, []).append(column_name)

    constraints: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for row in rows:
        constraint_name = str(row[0])
        constraint_type = str(row[1])
        clause = str(row[2] or "")
        constraints[constraint_name] = (
            constraint_type,
            clause,
            tuple(columns_by_constraint.get(constraint_name, ())),
        )
    return constraints


def _load_foreign_keys(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> dict[str, tuple[str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                tc.constraint_name,
                kcu.column_name,
                ccu.table_name AS referenced_table,
                ccu.column_name AS referenced_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.constraint_schema = tc.constraint_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'FOREIGN KEY'
            """,
            (AUTH_PUBLIC_SCHEMA, table_name),
        )
        rows = cursor.fetchall()
    foreign_keys: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        foreign_keys[str(row[0])] = (str(row[1]), str(row[2]), str(row[3]))
    return foreign_keys


def _show_create_table(
    *,
    connection: psycopg.Connection[Any],
    table_name: str,
) -> str:
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SHOW CREATE TABLE {}").format(sql.Identifier(table_name)))
        row = cursor.fetchone()
    if row is None:
        return ""
    return str(row[-1])


def _normalize_type(value: str) -> str:
    normalized = value.strip().lower()
    return {
        "text": "text",
        "string": "text",
        "character varying": "text",
        "varchar": "text",
        "timestamp with time zone": "timestamptz",
        "timestamptz": "timestamptz",
        "timestamp": "timestamp",
        "boolean": "bool",
        "bool": "bool",
        "integer": "int",
        "int": "int",
        "int4": "int",
        "bigint": "int",
        "jsonb": "jsonb",
        "uuid": "uuid",
    }.get(normalized, normalized)


def _normalized_sql_fragment(fragment: str) -> str:
    normalized = " ".join(fragment.lower().split())
    normalized = normalized.replace('"', "")
    normalized = normalized.replace("(", "").replace(")", "")
    normalized = normalized.replace(",", "")
    normalized = normalized.replace("!=", "<>")
    normalized = re.sub(r":{2,}[a-z_][a-z0-9_]*", "", normalized)
    normalized = re.sub(r"\basc\b", "", normalized)
    normalized = re.sub(r"\bdesc\b", "", normalized)
    normalized = " ".join(normalized.split())
    return normalized


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
