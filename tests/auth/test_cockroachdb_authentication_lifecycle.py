"""CockroachDB-backed integration proof for the auth lifecycle boundary."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from hashlib import sha256
from dataclasses import dataclass

import pytest
import psycopg

from services.auth.app.login import LoginError
from services.auth.app.login import LoginRequest
from services.auth.app.login import login_with_credentials
from services.auth.app.login import PersistentLoginStepUpStore
from services.auth.app.login import PersistentLoginLockoutStore
from services.auth.app.config import DEFAULT_AUTH_TENANT_ID
from services.auth.app.phone_change import PhoneChangeError
from services.auth.app.phone_change import PersistentPhoneChangeStore
from services.auth.app.phone_change import create_phone_change_request
from services.auth.app.phone_change import confirm_phone_change_request
from services.auth.app.registration import register_user
from services.auth.app.registration import PersistentDelegationStore
from services.auth.app.registration import RegistrationRequestRecord
from services.auth.app.registration import parse_registration_request
from services.auth.app.registration import PersistentRegistrationStore
from services.auth.app.password_reset import PasswordResetError
from services.auth.app.password_reset import PasswordResetConfirmRequest
from services.auth.app.password_reset import PasswordResetInitiateRequest
from services.auth.app.password_reset import PersistentPasswordResetStore
from services.auth.app.password_reset import confirm_password_reset_challenge
from services.auth.app.password_reset import initiate_password_reset_challenge
from services.auth.app.account_deletion import AccountDeletionRequestError
from services.auth.app.account_deletion import create_account_deletion_request
from services.auth.app.account_deletion import confirm_account_deletion_request
from services.auth.app.account_deletion import execute_account_deletion_request
from services.auth.app.account_deletion import PersistentAccountDeletionRequestStore
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.session_issuance import PersistentSessionIssuanceStore
from services.auth.app.email_verification import PersistentEmailVerificationStore
from services.auth.app.phone_verification import PersistentPhoneVerificationStore
from services.auth.app.phone_verification import PhoneVerificationChallengeRequest
from services.auth.app.phone_verification import issue_phone_verification_challenge
from services.auth.app.phone_verification import verify_phone_verification_challenge
from services.auth.app.phone_verification import parse_phone_verification_verify_request
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.otp_delivery_adapters import StubSmsOtpDeliveryAdapter


@dataclass(frozen=True)
class AuthStores:
    registration: PersistentRegistrationStore
    email_verification: PersistentEmailVerificationStore
    phone_verification: PersistentPhoneVerificationStore
    login_lockout: PersistentLoginLockoutStore
    login_step_up: PersistentLoginStepUpStore
    session_issuance: PersistentSessionIssuanceStore
    password_reset: PersistentPasswordResetStore
    phone_change: PersistentPhoneChangeStore
    account_deletion: PersistentAccountDeletionRequestStore
    delegation: PersistentDelegationStore


@pytest.fixture(scope="session")
def cockroach_auth_database_url() -> str:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth lifecycle tests.")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            row = cursor.fetchone()

    if row is None:
        pytest.skip("DATABASE_URL validation did not return a database row.")
    if str(row[0]) != "kodi_dev":
        pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")
    if "CockroachDB" not in str(row[1]):
        pytest.skip("DATABASE_URL does not target CockroachDB.")
    return database_url


def _build_stores(database_url: str) -> AuthStores:
    return AuthStores(
        registration=PersistentRegistrationStore(database_url=database_url),
        email_verification=PersistentEmailVerificationStore(database_url=database_url),
        phone_verification=PersistentPhoneVerificationStore(database_url=database_url),
        login_lockout=PersistentLoginLockoutStore(database_url=database_url),
        login_step_up=PersistentLoginStepUpStore(database_url=database_url),
        session_issuance=PersistentSessionIssuanceStore(database_url=database_url),
        password_reset=PersistentPasswordResetStore(database_url=database_url),
        phone_change=PersistentPhoneChangeStore(database_url=database_url),
        account_deletion=PersistentAccountDeletionRequestStore(database_url=database_url),
        delegation=PersistentDelegationStore(database_url=database_url),
    )


def _unique_email(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}@example.com"


def _unique_phone() -> str:
    return f"+2547{uuid4().int % 100_000_000:08d}"


def _unique_password(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}-Aa1!"


def _unique_kra_pin() -> str:
    suffix = uuid4().int % 1_000_000_000
    return f"A{suffix:09d}Z"


def _auth_header(*, user_id: UUID, tenant_id: str = DEFAULT_AUTH_TENANT_ID, role: str) -> str:
    return f"Bearer user_id={user_id};tenant_id={tenant_id};role={role}"


def _register_user(
    *,
    stores: AuthStores,
    email: str,
    phone: str,
    password: str,
    role: str = "IndividualTaxpayer",
) -> tuple[UUID, RegistrationRequestRecord]:
    request_record = parse_registration_request(
        {
            "email": email,
            "phone_number": phone,
            "kra_pin": _unique_kra_pin(),
            "password": password,
            "role": role,
        }
    )
    response = register_user(
        request_record=request_record,
        registration_store=stores.registration,
    )
    assert response.registration_status == "pending_verification"
    user = stores.registration.get_user_by_id(user_id=response.user_id)
    assert user is not None
    assert user.account_state == "pending_verification"
    assert user.verification_state == "pending_verification"
    assert user.email_normalized == email.lower()
    assert user.phone_number_normalized == phone
    return response.user_id, request_record


def _verify_registration_via_phone(
    *,
    stores: AuthStores,
    phone: str,
    email: str,
) -> None:
    challenge_response = issue_phone_verification_challenge(
        request_model=PhoneVerificationChallengeRequest(
            purpose="registration_verify",
            channel="sms",
            phone_number=phone,
            email=email,
            fallback_channel="email",
        ),
        idempotency_key=f"phone-verification:{phone}",
        phone_verification_store=stores.phone_verification,
        email_verification_store=stores.email_verification,
        sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
    )
    challenge_record = stores.phone_verification.get_challenge(
        challenge_id=challenge_response.challenge_id
    )
    assert challenge_record is not None
    verify_response = verify_phone_verification_challenge(
        verify_request=parse_phone_verification_verify_request(
            {
                "challenge_id": challenge_response.challenge_id,
                "otp_code": challenge_record.otp_code,
            }
        ),
        phone_verification_store=stores.phone_verification,
        registration_store=stores.registration,
    )
    assert verify_response.status == "verified"
    assert verify_response.verification_status == "verified"


def _login_with_step_up(
    *,
    stores: AuthStores,
    login_id: str,
    password: str,
    source_ip: str = "127.0.0.1",
    device_fingerprint: str | None = None,
) -> tuple[Any, Any]:
    pending = login_with_credentials(
        payload=LoginRequest(
            login_id=login_id,
            password=password,
            device_fingerprint=device_fingerprint,
        ).model_dump(mode="python"),
        source_ip=source_ip,
        registration_store=stores.registration,
        session_issuance_store=stores.session_issuance,
        login_lockout_store=stores.login_lockout,
        login_step_up_store=stores.login_step_up,
        email_verification_store=stores.email_verification,
        phone_verification_store=stores.phone_verification,
        sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
    )
    assert pending.status == "pending_step_up"
    assert pending.step_up_channel == "email"

    challenge = stores.email_verification.get_challenge(
        challenge_id=pending.step_up_challenge_id
    )
    assert challenge is not None

    success = login_with_credentials(
        payload=LoginRequest(
            login_id=login_id,
            password=password,
            device_fingerprint=device_fingerprint,
            step_up_challenge_id=pending.step_up_challenge_id,
            step_up_otp_code=challenge.otp_code,
        ).model_dump(mode="python"),
        source_ip=source_ip,
        registration_store=stores.registration,
        session_issuance_store=stores.session_issuance,
        login_lockout_store=stores.login_lockout,
        login_step_up_store=stores.login_step_up,
        email_verification_store=stores.email_verification,
        phone_verification_store=stores.phone_verification,
        sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
    )
    assert success.status == "authenticated"
    return success, challenge


def _session_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _assert_refresh_row_state(
    *,
    database_url: str,
    refresh_token: str,
    expected_consumed: bool,
    expected_session_id: UUID,
) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT is_consumed, session_id
                FROM auth_session_refresh_tokens
                WHERE refresh_token_hash = %s
                """,
                (_session_hash(refresh_token),),
            )
            row = cursor.fetchone()

    assert row is not None
    assert bool(row[0]) is expected_consumed
    assert row[1] == expected_session_id


def _cleanup_user_artifacts(
    *,
    database_url: str,
    user_id: UUID,
    email: str,
    phone: str,
    old_phone: str | None = None,
    session_ids: tuple[UUID, ...] = (),
    request_ids: tuple[UUID, ...] = (),
) -> None:
    phones = [phone]
    if old_phone is not None and old_phone not in phones:
        phones.append(old_phone)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM auth_login_lockouts WHERE login_id_normalized = %s",
                (phone,),
            )
            if old_phone is not None and old_phone != phone:
                cursor.execute(
                    "DELETE FROM auth_login_lockouts WHERE login_id_normalized = %s",
                    (old_phone,),
                )
            cursor.execute(
                "DELETE FROM auth_login_step_up_states WHERE login_id_normalized = %s",
                (phone,),
            )
            if old_phone is not None and old_phone != phone:
                cursor.execute(
                    "DELETE FROM auth_login_step_up_states WHERE login_id_normalized = %s",
                    (old_phone,),
                )
            cursor.execute(
                "DELETE FROM auth_phone_change_audit_events WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_phone_change_requests WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_reauth_proofs WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_otp_proofs WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_audit_events WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_notifications WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_incidents WHERE actor_user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_requests WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM delegations WHERE principal_user_id = %s OR delegate_user_id = %s",
                (user_id, user_id),
            )
            for subject in {email, *phones}:
                cursor.execute(
                    "DELETE FROM auth_otp_challenges WHERE subject_normalized = %s",
                    (subject,),
                )
                cursor.execute(
                    "DELETE FROM auth_password_reset_challenges WHERE subject_normalized = %s",
                    (subject,),
                )
            if session_ids:
                cursor.execute(
                    "DELETE FROM auth_session_refresh_tokens WHERE session_id = ANY(%s::uuid[])",
                    (list(session_ids),),
                )
                cursor.execute(
                    "DELETE FROM sessions WHERE id = ANY(%s::uuid[])",
                    (list(session_ids),),
                )
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        connection.commit()


def test_cockroachdb_authentication_lifecycle_registration_login_refresh(
    cockroach_auth_database_url: str,
) -> None:
    stores = _build_stores(cockroach_auth_database_url)
    email = _unique_email("auth.lifecycle.registration")
    phone = _unique_phone()
    password = _unique_password("InitialPass")
    user_id, _ = _register_user(
        stores=stores,
        email=email,
        phone=phone,
        password=password,
    )

    try:
        _verify_registration_via_phone(stores=stores, phone=phone, email=email)

        verified_user = stores.registration.get_user_by_id(user_id=user_id)
        assert verified_user is not None
        assert verified_user.account_state == "active"
        assert verified_user.verification_state == "verified"
        assert verified_user.verified_at is not None

        login_success, _ = _login_with_step_up(
            stores=stores,
            login_id=phone,
            password=password,
            device_fingerprint="device-registration",
        )

        session_id = login_success.session.session_id
        refresh_token = login_success.refresh_token

        active_session = stores.session_issuance.get_session(session_id=session_id)
        assert active_session is not None
        assert active_session.user_id == user_id
        assert active_session.is_invalidated is False

        before_activity = stores.session_issuance.evaluate_session(session_id=session_id)
        assert before_activity is not None
        touched = stores.session_issuance.touch_session_activity(session_id=session_id)
        assert touched is not None
        after_activity = stores.session_issuance.evaluate_session(session_id=session_id)
        assert after_activity is not None
        assert after_activity.last_activity_at >= before_activity.last_activity_at

        refreshed = stores.session_issuance.refresh_session(refresh_token=refresh_token)
        assert refreshed.session_id == session_id
        assert refreshed.refresh_token != refresh_token
        _assert_refresh_row_state(
            database_url=cockroach_auth_database_url,
            refresh_token=refresh_token,
            expected_consumed=True,
            expected_session_id=session_id,
        )
        _assert_refresh_row_state(
            database_url=cockroach_auth_database_url,
            refresh_token=refreshed.refresh_token,
            expected_consumed=False,
            expected_session_id=session_id,
        )

        with pytest.raises(SessionIssuanceError) as excinfo:
            stores.session_issuance.refresh_session(refresh_token=refresh_token)
        assert excinfo.value.reason == "refresh_token_reused"

        revoked_count = stores.session_issuance.revoke_session(
            user_id=user_id,
            session_id=session_id,
        )
        assert revoked_count == 1
        revoked_session = stores.session_issuance.evaluate_session(session_id=session_id)
        assert revoked_session is not None
        assert revoked_session.is_invalidated is True
        assert revoked_session.invalidated_reason == "session_revoked"

        with pytest.raises(SessionIssuanceError) as excinfo:
            stores.session_issuance.refresh_session(refresh_token=refreshed.refresh_token)
        assert excinfo.value.reason == "refresh_token_session_revoked"
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
            session_ids=(session_id,) if "session_id" in locals() else (),
        )


def test_cockroachdb_authentication_lifecycle_password_reset_and_history(
    cockroach_auth_database_url: str,
) -> None:
    stores = _build_stores(cockroach_auth_database_url)
    email = _unique_email("auth.lifecycle.password")
    phone = _unique_phone()
    password_v1 = _unique_password("PasswordOne")
    password_v2 = _unique_password("PasswordTwo")
    user_id, _ = _register_user(
        stores=stores,
        email=email,
        phone=phone,
        password=password_v1,
    )
    post_reset_session_id: UUID | None = None

    try:
        _verify_registration_via_phone(stores=stores, phone=phone, email=email)

        login_success, _ = _login_with_step_up(
            stores=stores,
            login_id=phone,
            password=password_v1,
            device_fingerprint="device-password-reset",
        )
        pre_reset_session_id = login_success.session.session_id

        reset_challenge = initiate_password_reset_challenge(
            request_model=PasswordResetInitiateRequest(
                purpose="password_reset",
                channel="sms",
                phone_number=phone,
            ),
            idempotency_key=f"password-reset:{phone}",
            registration_store=stores.registration,
            password_reset_store=stores.password_reset,
        )
        challenge_record = stores.password_reset.get_challenge(
            challenge_id=reset_challenge.challenge_id
        )
        assert challenge_record is not None

        reset_confirm = confirm_password_reset_challenge(
            request_model=PasswordResetConfirmRequest(
                challenge_id=reset_challenge.challenge_id,
                reset_code=challenge_record.reset_code,
                new_password=password_v2,
            ),
            registration_store=stores.registration,
            password_reset_store=stores.password_reset,
        )
        assert reset_confirm.status == "password_updated"

        refreshed_user = stores.registration.get_user_by_id(user_id=user_id)
        assert refreshed_user is not None
        assert stores.registration.is_password_valid(
            user_id=user_id,
            password=password_v2,
        )
        assert not stores.registration.is_password_valid(
            user_id=user_id,
            password=password_v1,
        )

        with pytest.raises(LoginError) as excinfo:
            login_with_credentials(
                payload=LoginRequest(
                    login_id=phone,
                    password=password_v1,
                    device_fingerprint="device-password-reset",
                ).model_dump(mode="python"),
                source_ip="127.0.0.1",
                registration_store=stores.registration,
                session_issuance_store=stores.session_issuance,
                login_lockout_store=stores.login_lockout,
                login_step_up_store=stores.login_step_up,
                email_verification_store=stores.email_verification,
                phone_verification_store=stores.phone_verification,
                sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
            )
        assert excinfo.value.reason == "login_invalid_credentials"

        post_reset_login, _ = _login_with_step_up(
            stores=stores,
            login_id=phone,
            password=password_v2,
            device_fingerprint="device-password-reset",
        )
        assert post_reset_login.status == "authenticated"
        assert post_reset_login.session.user_id == user_id
        post_reset_session_id = post_reset_login.session.session_id

        with pytest.raises(PasswordResetError) as excinfo:
            confirm_password_reset_challenge(
                request_model=PasswordResetConfirmRequest(
                    challenge_id=reset_challenge.challenge_id,
                    reset_code=challenge_record.reset_code,
                    new_password=password_v1,
                ),
                registration_store=stores.registration,
                password_reset_store=stores.password_reset,
            )
        assert excinfo.value.reason in {
            "password_reset_token_already_used",
            "password_reset_token_invalid",
        }

    finally:
        session_ids_to_cleanup = tuple(
            session_id
            for session_id in (pre_reset_session_id, post_reset_session_id)
            if session_id is not None
        )
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
            session_ids=session_ids_to_cleanup,
        )


def test_cockroachdb_authentication_lifecycle_phone_change_supersession_and_login_identifier_update(
    cockroach_auth_database_url: str,
) -> None:
    stores = _build_stores(cockroach_auth_database_url)
    email = _unique_email("auth.lifecycle.phone")
    old_phone = _unique_phone()
    password = _unique_password("PhoneChangePass")
    user_id, _ = _register_user(
        stores=stores,
        email=email,
        phone=old_phone,
        password=password,
    )
    session_id: UUID | None = None
    new_session_id: UUID | None = None
    second_target_phone: str | None = None

    try:
        _verify_registration_via_phone(stores=stores, phone=old_phone, email=email)

        login_success, _ = _login_with_step_up(
            stores=stores,
            login_id=old_phone,
            password=password,
            device_fingerprint="device-phone-change",
        )
        session_id = login_success.session.session_id

        first_target_phone = _unique_phone()
        second_target_phone = _unique_phone()
        first_request = create_phone_change_request(
            payload={
                "new_phone_number": first_target_phone,
                "current_password": password,
            },
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"phone-change:{user_id}:a",
            correlation_id=None,
            registration_store=stores.registration,
            phone_verification_store=stores.phone_verification,
            phone_change_store=stores.phone_change,
            sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
        )
        first_request_record = stores.phone_change.get_request_by_id(
            request_id=first_request.request_id
        )
        assert first_request_record is not None
        assert first_request_record.phone_change_state == "pending_confirmation"

        second_request = create_phone_change_request(
            payload={
                "new_phone_number": second_target_phone,
                "current_password": password,
            },
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"phone-change:{user_id}:b",
            correlation_id=None,
            registration_store=stores.registration,
            phone_verification_store=stores.phone_verification,
            phone_change_store=stores.phone_change,
            sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
        )
        superseded_first_request = stores.phone_change.get_request_by_id(
            request_id=first_request.request_id
        )
        assert superseded_first_request is not None
        assert superseded_first_request.phone_change_state == "superseded"
        second_request_record = stores.phone_change.get_request_by_id(
            request_id=second_request.request_id
        )
        assert second_request_record is not None
        assert second_request_record.phone_change_state == "pending_confirmation"

        first_challenge = stores.phone_verification.get_challenge(
            challenge_id=first_request.step_up_challenge_id
        )
        assert first_challenge is not None
        with pytest.raises(PhoneChangeError) as excinfo:
            confirm_phone_change_request(
                payload={
                    "request_id": first_request.request_id,
                    "step_up_challenge_id": first_request.step_up_challenge_id,
                    "step_up_otp_code": first_challenge.otp_code,
                },
                authorization_header=_auth_header(
                    user_id=user_id,
                    role="IndividualTaxpayer",
                ),
                idempotency_key=f"phone-change:{user_id}:confirm-a",
                correlation_id=None,
                registration_store=stores.registration,
                phone_verification_store=stores.phone_verification,
                phone_change_store=stores.phone_change,
            )
        assert excinfo.value.reason == "phone_change_request_invalid"

        second_challenge = stores.phone_verification.get_challenge(
            challenge_id=second_request.step_up_challenge_id
        )
        assert second_challenge is not None
        confirm_second = confirm_phone_change_request(
            payload={
                "request_id": second_request.request_id,
                "step_up_challenge_id": second_request.step_up_challenge_id,
                "step_up_otp_code": second_challenge.otp_code,
            },
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"phone-change:{user_id}:confirm-b",
            correlation_id=None,
            registration_store=stores.registration,
            phone_verification_store=stores.phone_verification,
            phone_change_store=stores.phone_change,
        )
        assert confirm_second.status == "phone_updated"
        assert confirm_second.updated_phone_number == second_target_phone

        updated_user = stores.registration.get_user_by_id(user_id=user_id)
        assert updated_user is not None
        assert updated_user.phone_number_normalized == second_target_phone
        assert updated_user.account_state == "active"
        assert updated_user.verification_state == "verified"

        with pytest.raises(LoginError):
            login_with_credentials(
                payload=LoginRequest(
                    login_id=old_phone,
                    password=password,
                    device_fingerprint="device-phone-change",
                ).model_dump(mode="python"),
                source_ip="127.0.0.1",
                registration_store=stores.registration,
                session_issuance_store=stores.session_issuance,
                login_lockout_store=stores.login_lockout,
                login_step_up_store=stores.login_step_up,
                email_verification_store=stores.email_verification,
                phone_verification_store=stores.phone_verification,
                sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
            )

        new_login, _ = _login_with_step_up(
            stores=stores,
            login_id=second_target_phone,
            password=password,
            device_fingerprint="device-phone-change",
        )
        assert new_login.session.user_id == user_id
        new_session_id = new_login.session.session_id
    finally:
        cleanup_phone = (
            second_target_phone if second_target_phone is not None else old_phone
        )
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=cleanup_phone,
            old_phone=old_phone,
            session_ids=tuple(
                item for item in (session_id, new_session_id) if item is not None
            ),
        )


def test_cockroachdb_authentication_lifecycle_delegation_and_account_deletion(
    cockroach_auth_database_url: str,
) -> None:
    stores = _build_stores(cockroach_auth_database_url)
    principal_email = _unique_email("auth.lifecycle.delegation.principal")
    principal_phone = _unique_phone()
    principal_password = _unique_password("PrincipalPass")
    delegate_email = _unique_email("auth.lifecycle.delegation.delegate")
    delegate_phone = _unique_phone()
    delegate_password = _unique_password("DelegatePass")
    principal_user_id, _ = _register_user(
        stores=stores,
        email=principal_email,
        phone=principal_phone,
        password=principal_password,
    )
    delegate_user_id, _ = _register_user(
        stores=stores,
        email=delegate_email,
        phone=delegate_phone,
        password=delegate_password,
    )

    try:
        _verify_registration_via_phone(stores=stores, phone=principal_phone, email=principal_email)
        _verify_registration_via_phone(stores=stores, phone=delegate_phone, email=delegate_email)

        delegation = stores.delegation.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at="2026-08-11T00:00:00Z",
        )
        assert delegation.is_active is True
        fresh_delegation_store = PersistentDelegationStore(database_url=cockroach_auth_database_url)
        active_delegation = fresh_delegation_store.get_active_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )
        assert active_delegation is not None
        assert active_delegation.delegation_id == delegation.delegation_id

        revoked = fresh_delegation_store.revoke_delegation(
            delegation_id=delegation.delegation_id,
            revoked_at="2026-08-11T00:05:00Z",
        )
        assert revoked.is_active is False
        assert revoked.revoked_at == "2026-08-11T00:05:00Z"

        reinstated = fresh_delegation_store.reactivate_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at="2026-08-11T00:10:00Z",
        )
        assert reinstated.is_active is True
        assert reinstated.delegation_id != delegation.delegation_id
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=principal_user_id,
            email=principal_email.lower(),
            phone=principal_phone,
        )
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=delegate_user_id,
            email=delegate_email.lower(),
            phone=delegate_phone,
        )


def test_cockroachdb_authentication_lifecycle_account_deletion_terminal_and_cancellation(
    cockroach_auth_database_url: str,
) -> None:
    stores = _build_stores(cockroach_auth_database_url)
    email = _unique_email("auth.lifecycle.deletion")
    phone = _unique_phone()
    password = _unique_password("DeletePass")
    user_id, _ = _register_user(
        stores=stores,
        email=email,
        phone=phone,
        password=password,
    )
    session_id: UUID | None = None
    deletion_request_id: UUID | None = None
    cancel_user_id: UUID | None = None
    cancel_user_email: str | None = None
    cancel_user_phone: str | None = None
    cancel_request_id: UUID | None = None
    cancel_session_id: UUID | None = None

    try:
        _verify_registration_via_phone(stores=stores, phone=phone, email=email)
        login_success, _ = _login_with_step_up(
            stores=stores,
            login_id=phone,
            password=password,
            device_fingerprint="device-deletion",
        )
        session_id = login_success.session.session_id
        refresh_token = login_success.refresh_token

        deletion_request = create_account_deletion_request(
            payload={"request_reason": "account closure"},
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"account-deletion:{user_id}:request",
            correlation_id=None,
            registration_store=stores.registration,
            account_deletion_store=stores.account_deletion,
        )
        deletion_request_id = deletion_request.request_id
        assert deletion_request.status == "accepted"
        assert deletion_request.deletion_state == "requested"

        reauth_proof = stores.account_deletion.issue_test_reauth_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=deletion_request.request_id,
        )
        otp_verification_id = stores.account_deletion.issue_test_otp_verification_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=deletion_request.request_id,
        )
        deletion_confirm = confirm_account_deletion_request(
            payload={
                "request_id": deletion_request.request_id,
                "reauth_proof": reauth_proof,
                "otp_verification_id": otp_verification_id,
            },
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"account-deletion:{user_id}:confirm",
            correlation_id=None,
            registration_store=stores.registration,
            account_deletion_store=stores.account_deletion,
        )
        assert deletion_confirm.status == "confirmed"
        assert deletion_confirm.deletion_state in {"confirmed", "cooldown_active"}

        stores.account_deletion.force_request_cooldown_expired(
            request_id=deletion_request.request_id
        )
        deletion_execute = execute_account_deletion_request(
            payload={"request_id": deletion_request.request_id},
            authorization_header=_auth_header(
                user_id=user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"account-deletion:{user_id}:execute",
            correlation_id=None,
            registration_store=stores.registration,
            account_deletion_store=stores.account_deletion,
        )
        assert deletion_execute.status == "executed"
        assert deletion_execute.execution_outcome == "tombstoned"
        assert deletion_execute.revoked_session_count >= 1

        deleted_user = stores.registration.get_user_by_id(user_id=user_id)
        assert deleted_user is not None
        assert deleted_user.account_state == "disabled"
        assert deleted_user.deletion_lifecycle_state == "tombstoned"
        assert deleted_user.credentials_invalidated_at is not None
        assert deleted_user.anonymized_at is not None

        deleted_session = stores.session_issuance.evaluate_session(session_id=session_id)
        assert deleted_session is not None
        assert deleted_session.is_invalidated is True
        assert deleted_session.invalidated_reason == "session_revoked"

        with pytest.raises(LoginError):
            _login_with_step_up(
                stores=stores,
                login_id=phone,
                password=password,
                device_fingerprint="device-deletion",
            )
        with pytest.raises(SessionIssuanceError) as excinfo:
            stores.session_issuance.refresh_session(refresh_token=refresh_token)
        assert excinfo.value.reason == "refresh_token_session_revoked"

        cancel_user_email = _unique_email("auth.lifecycle.deletion.cancel")
        cancel_user_phone = _unique_phone()
        cancel_user_password = _unique_password("CancelPass")
        cancel_user_id, _ = _register_user(
            stores=stores,
            email=cancel_user_email,
            phone=cancel_user_phone,
            password=cancel_user_password,
        )
        cancel_request_id = None
        _verify_registration_via_phone(
            stores=stores,
            phone=cancel_user_phone,
            email=cancel_user_email,
        )
        cancel_request = create_account_deletion_request(
            payload={"request_reason": "temporary hold"},
            authorization_header=_auth_header(
                user_id=cancel_user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"account-deletion:{cancel_user_id}:request",
            correlation_id=None,
            registration_store=stores.registration,
            account_deletion_store=stores.account_deletion,
        )
        cancel_request_id = cancel_request.request_id
        cancel_reauth_proof = stores.account_deletion.issue_test_reauth_proof(
            user_id=cancel_user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=cancel_request.request_id,
        )
        cancel_otp_verification_id = (
            stores.account_deletion.issue_test_otp_verification_proof(
                user_id=cancel_user_id,
                tenant_id=DEFAULT_AUTH_TENANT_ID,
                request_id=cancel_request.request_id,
            )
        )
        confirm_account_deletion_request(
            payload={
                "request_id": cancel_request.request_id,
                "reauth_proof": cancel_reauth_proof,
                "otp_verification_id": cancel_otp_verification_id,
            },
            authorization_header=_auth_header(
                user_id=cancel_user_id,
                role="IndividualTaxpayer",
            ),
            idempotency_key=f"account-deletion:{cancel_user_id}:confirm",
            correlation_id=None,
            registration_store=stores.registration,
            account_deletion_store=stores.account_deletion,
        )
        cancelled = stores.account_deletion.create_or_replay_cancel(
            user_id=cancel_user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=cancel_request.request_id,
            idempotency_key=f"account-deletion:{cancel_user_id}:cancel",
            request_fingerprint=f"account_deletion_cancel:{cancel_user_id}:{DEFAULT_AUTH_TENANT_ID}:IndividualTaxpayer:{cancel_request.request_id}",
            correlation_id=None,
        )
        assert cancelled.deletion_state == "cancelled"
        cancelled_request = stores.account_deletion.get_request_by_id(
            request_id=cancel_request.request_id
        )
        assert cancelled_request is not None
        assert cancelled_request.deletion_state == "cancelled"
        with pytest.raises(AccountDeletionRequestError):
            execute_account_deletion_request(
                payload={"request_id": cancel_request.request_id},
                authorization_header=_auth_header(
                    user_id=cancel_user_id,
                    role="IndividualTaxpayer",
                ),
                idempotency_key=f"account-deletion:{cancel_user_id}:execute",
                correlation_id=None,
                registration_store=stores.registration,
                account_deletion_store=stores.account_deletion,
            )
        post_cancel_login, _ = _login_with_step_up(
            stores=stores,
            login_id=cancel_user_phone,
            password=cancel_user_password,
            device_fingerprint="device-deletion-cancel",
        )
        assert post_cancel_login.status == "authenticated"
        cancel_session_id = post_cancel_login.session.session_id
    finally:
        deletion_request_ids = (
            (deletion_request_id,) if deletion_request_id is not None else ()
        )
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
            session_ids=(session_id,) if session_id is not None else (),
            request_ids=deletion_request_ids,
        )
        if (
            cancel_user_id is not None
            and cancel_user_email is not None
            and cancel_user_phone is not None
        ):
            _cleanup_user_artifacts(
                database_url=cockroach_auth_database_url,
                user_id=cancel_user_id,
                email=cancel_user_email.lower(),
                phone=cancel_user_phone,
                session_ids=(
                    (cancel_session_id,) if cancel_session_id is not None else ()
                ),
                request_ids=(
                    (cancel_request_id,) if cancel_request_id is not None else ()
                ),
            )
