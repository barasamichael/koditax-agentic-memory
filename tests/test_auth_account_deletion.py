"""Runtime tests for deterministic account deletion request and confirmation gate."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.account_deletion import InMemoryAccountDeletionRequestStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[tuple[TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore]]
):
    """Create isolated auth app client with deterministic account deletion request store."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    account_deletion_store = InMemoryAccountDeletionRequestStore()
    app.state.registration_store = registration_store
    app.state.account_deletion_request_store = account_deletion_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, account_deletion_store
    reset_default_registration_store()


def test_account_deletion_request_positive_without_blockers_remains_requested(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-request-requested@example.com",
        phone_number="+254733220101",
        correlation_id="deletion-request-requested-register-corr",
    )

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-request-requested-idem",
            "X-Correlation-ID": "deletion-request-requested-corr",
        },
        json={"request_reason": "Request account deletion with no blockers."},
    )
    payload = _response_json(response)

    assert response.status_code == 201
    assert payload["status"] == "accepted"
    assert payload["deletion_state"] == "requested"
    assert payload["blockers"] == []
    request_id = UUID(cast(str, payload["request_id"]))

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "requested"
    assert persisted_request.blocker_reasons == ()

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 1
    assert audit_events[0].action == "account_deletion_request_created"
    assert audit_events[0].action_status == "created"
    assert audit_events[0].blocker_reasons == ()
    assert audit_events[0].event_id == audit_events[0].audit_evidence_id
    assert audit_events[0].event_type == "account_deletion_request_created"
    assert audit_events[0].deletion_state == "requested"
    assert audit_events[0].correlation_id == "deletion-request-requested-corr"
    assert audit_events[0].occurred_at == audit_events[0].created_at

    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(notification_records) == 1
    assert notification_records[0].event_type == "account_deletion_request_created"
    assert notification_records[0].status == "queued"
    assert notification_records[0].channel == "email"
    assert notification_records[0].correlation_id == "deletion-request-requested-corr"


@pytest.mark.parametrize(
    ("precheck_flags", "expected_reason"),
    [
        ({"compliance_lock": True}, "deletion_blocked_compliance_lock"),
        ({"active_obligation": True}, "deletion_blocked_active_obligation"),
        ({"retention_constraint": True}, "deletion_blocked_retention_constraint"),
    ],
)
def test_account_deletion_request_single_blocker_is_persisted_and_audited(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
    precheck_flags: dict[str, bool],
    expected_reason: str,
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email=f"deletion-request-single-blocker-{expected_reason}@example.com",
        phone_number="+254733220102",
        correlation_id="deletion-request-single-blocker-register-corr",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user_id,
        tenant_id="default_tenant",
        compliance_lock=precheck_flags.get("compliance_lock", False),
        active_obligation=precheck_flags.get("active_obligation", False),
        retention_constraint=precheck_flags.get("retention_constraint", False),
    )

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": f"deletion-request-single-blocker-{expected_reason}-idem",
            "X-Correlation-ID": "deletion-request-single-blocker-corr",
        },
        json={"request_reason": "Request account deletion with one blocker."},
    )
    payload = _response_json(response)

    assert response.status_code == 201
    assert payload["status"] == "accepted"
    assert payload["deletion_state"] == "blocked"
    assert payload["blockers"] == [expected_reason]
    request_id = UUID(cast(str, payload["request_id"]))

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "blocked"
    assert list(persisted_request.blocker_reasons) == [expected_reason]

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 1
    assert audit_events[0].action == "account_deletion_request_blocked"
    assert audit_events[0].action_status == "blocked"
    assert list(audit_events[0].blocker_reasons) == [expected_reason]
    assert audit_events[0].event_type == "account_deletion_request_blocked"
    assert audit_events[0].deletion_state == "blocked"

    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(notification_records) == 1
    assert notification_records[0].event_type == "account_deletion_request_blocked"
    assert notification_records[0].status == "queued"


def test_account_deletion_request_multiple_blockers_are_ordered_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-request-multi-blocker@example.com",
        phone_number="+254733220103",
        correlation_id="deletion-request-multi-blocker-register-corr",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user_id,
        tenant_id="default_tenant",
        compliance_lock=True,
        active_obligation=True,
        retention_constraint=True,
    )

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-request-multi-blocker-idem",
            "X-Correlation-ID": "deletion-request-multi-blocker-corr",
        },
        json={"request_reason": "Request account deletion with all blockers."},
    )
    payload = _response_json(response)
    expected = [
        "deletion_blocked_compliance_lock",
        "deletion_blocked_active_obligation",
        "deletion_blocked_retention_constraint",
    ]

    assert response.status_code == 201
    assert payload["deletion_state"] == "blocked"
    assert payload["blockers"] == expected


def test_account_deletion_request_idempotent_replay_and_conflict_with_blockers(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-request-idem@example.com",
        phone_number="+254733220104",
        correlation_id="deletion-request-idem-register-corr",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user_id,
        tenant_id="default_tenant",
        active_obligation=True,
    )
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-request-idem-key",
        "X-Correlation-ID": "deletion-request-idem-corr",
    }
    payload = {"request_reason": "Idempotent blocked request."}

    first = client.post("/v1/auth/account-deletion/requests", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/requests", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)

    conflicting = client.post(
        "/v1/auth/account-deletion/requests",
        headers=headers,
        json={"request_reason": "Conflicting payload for same idempotency key."},
    )
    conflict_error = _extract_error_detail(conflicting)
    assert conflicting.status_code == 409
    assert conflict_error["error_code"] == "idempotency_key_conflict"
    assert conflict_error["reason"] == "idempotency_key_conflict"


def test_account_deletion_confirm_positive_with_valid_bound_proofs(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-positive@example.com",
        phone_number="+254733220001",
        correlation_id="deletion-confirm-positive-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-positive-request-idem",
        correlation_id="deletion-confirm-positive-request-corr",
        request_reason="User confirmed deletion request with valid proofs.",
    )
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )

    confirm_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-positive-confirm-idem",
            "X-Correlation-ID": "deletion-confirm-positive-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )

    payload = _response_json(confirm_response)
    assert confirm_response.status_code == 200
    assert payload["status"] == "confirmed"
    assert payload["deletion_state"] == "cooldown_active"
    assert payload["request_id"] == str(request_id)
    assert cast(str, payload["cooldown_expires_at"]).endswith("Z")

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "confirmed"

    persisted_user = registration_store.get_user_by_id(user_id=user_id)
    assert persisted_user is not None
    assert persisted_user.account_state == "active"

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 2
    assert audit_events[1].action == "account_deletion_request_confirmed"
    assert audit_events[1].action_status == "confirmed"
    assert audit_events[1].event_type == "account_deletion_request_confirmed"
    assert audit_events[1].deletion_state == "confirmed"

    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(notification_records) == 2
    assert notification_records[1].event_type == "account_deletion_request_confirmed"
    assert notification_records[1].status == "queued"


def test_account_deletion_cancel_positive_during_cooldown(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-positive@example.com",
        phone_number="+254733220008",
        correlation_id="deletion-cancel-positive-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-cancel-positive-request-idem",
        request_correlation_id="deletion-cancel-positive-request-corr",
        confirm_idempotency_key="deletion-cancel-positive-confirm-idem",
        confirm_correlation_id="deletion-cancel-positive-confirm-corr",
    )

    cancel_response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-cancel-positive-cancel-idem",
            "X-Correlation-ID": "deletion-cancel-positive-cancel-corr",
        },
        json={"request_id": str(request_id)},
    )
    payload = _response_json(cancel_response)
    assert cancel_response.status_code == 200
    assert payload["status"] == "cancelled"
    assert payload["deletion_state"] == "cancelled"
    assert payload["request_id"] == str(request_id)

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "cancelled"

    persisted_user = registration_store.get_user_by_id(user_id=user_id)
    assert persisted_user is not None
    assert persisted_user.account_state == "active"

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 3
    assert audit_events[2].action == "account_deletion_request_cancelled"
    assert audit_events[2].action_status == "cancelled"
    assert audit_events[2].event_type == "account_deletion_request_cancelled"
    assert audit_events[2].deletion_state == "cancelled"

    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(notification_records) == 3
    assert notification_records[2].event_type == "account_deletion_request_cancelled"
    assert notification_records[2].status == "queued"


def test_account_deletion_cancel_request_not_found_is_deterministic(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-not-found@example.com",
        phone_number="+254733220009",
        correlation_id="deletion-cancel-not-found-register-corr",
    )
    body = {"request_id": str(uuid4())}
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-cancel-not-found-idem",
        "X-Correlation-ID": "deletion-cancel-not-found-corr",
    }
    first = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=body)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 404
    assert first_error["error_code"] == "account_deletion_cancel_request_not_found"
    assert first_error["reason"] == "account_deletion_cancel_request_not_found"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_evidence_append_only_immutability(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-evidence-immutability@example.com",
        phone_number="+254733220210",
        correlation_id="deletion-evidence-immutability-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-evidence-immutability-request-idem",
        correlation_id="deletion-evidence-immutability-request-corr",
        request_reason="Immutability coverage.",
    )
    first_event = account_deletion_store.get_audit_events_for_user(user_id=user_id)[0]
    first_notification = account_deletion_store.get_notification_records_for_user(user_id=user_id)[
        0
    ]
    first_event_id = first_event.event_id
    first_notification_id = first_notification.notification_id
    first_occurred_at = first_event.occurred_at

    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    confirm_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-evidence-immutability-confirm-idem",
            "X-Correlation-ID": "deletion-evidence-immutability-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    assert confirm_response.status_code == 200

    all_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    all_notifications = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(all_events) == 2
    assert len(all_notifications) == 2
    assert all_events[0].event_id == first_event_id
    assert all_events[0].occurred_at == first_occurred_at
    assert all_notifications[0].notification_id == first_notification_id
    assert all_events[0].event_type == "account_deletion_request_created"
    assert all_notifications[0].event_type == "account_deletion_request_created"


def test_account_deletion_cancel_wrong_user_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-owner@example.com",
        phone_number="+254733220010",
        correlation_id="deletion-cancel-owner-register-corr",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-attacker@example.com",
        phone_number="+254733220011",
        correlation_id="deletion-cancel-attacker-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=owner_user_id,
        request_idempotency_key="deletion-cancel-owner-request-idem",
        request_correlation_id="deletion-cancel-owner-request-corr",
        confirm_idempotency_key="deletion-cancel-owner-confirm-idem",
        confirm_correlation_id="deletion-cancel-owner-confirm-corr",
    )
    response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=attacker_user_id),
            "Idempotency-Key": "deletion-cancel-wrong-user-idem",
            "X-Correlation-ID": "deletion-cancel-wrong-user-corr",
        },
        json={"request_id": str(request_id)},
    )
    error = _extract_error_detail(response)

    assert response.status_code == 404
    assert error["error_code"] == "account_deletion_cancel_request_not_found"
    assert error["reason"] == "account_deletion_cancel_request_not_found"


def test_account_deletion_cancel_invalid_state_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-invalid-state@example.com",
        phone_number="+254733220012",
        correlation_id="deletion-cancel-invalid-state-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-cancel-invalid-state-request-idem",
        correlation_id="deletion-cancel-invalid-state-request-corr",
        request_reason="Cancel before confirm should fail.",
    )
    response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-cancel-invalid-state-cancel-idem",
            "X-Correlation-ID": "deletion-cancel-invalid-state-cancel-corr",
        },
        json={"request_id": str(request_id)},
    )
    error = _extract_error_detail(response)

    assert response.status_code == 409
    assert error["error_code"] == "account_deletion_cancel_not_allowed_for_state"
    assert error["reason"] == "account_deletion_cancel_not_allowed_for_state"


def test_account_deletion_cancel_after_cooldown_expiry_is_rejected(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-expired@example.com",
        phone_number="+254733220013",
        correlation_id="deletion-cancel-expired-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-cancel-expired-request-idem",
        request_correlation_id="deletion-cancel-expired-request-corr",
        confirm_idempotency_key="deletion-cancel-expired-confirm-idem",
        confirm_correlation_id="deletion-cancel-expired-confirm-corr",
    )
    account_deletion_store.force_request_cooldown_expired(request_id=request_id)

    response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-cancel-expired-cancel-idem",
            "X-Correlation-ID": "deletion-cancel-expired-cancel-corr",
        },
        json={"request_id": str(request_id)},
    )
    error = _extract_error_detail(response)

    assert response.status_code == 409
    assert error["error_code"] == "account_deletion_cancel_cooldown_expired"
    assert error["reason"] == "account_deletion_cancel_cooldown_expired"


def test_account_deletion_cancel_idempotent_replay_and_conflict(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-cancel-idempotent@example.com",
        phone_number="+254733220014",
        correlation_id="deletion-cancel-idempotent-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-cancel-idempotent-request-idem",
        request_correlation_id="deletion-cancel-idempotent-request-corr",
        confirm_idempotency_key="deletion-cancel-idempotent-confirm-idem",
        confirm_correlation_id="deletion-cancel-idempotent-confirm-corr",
    )
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-cancel-idempotent-cancel-idem",
        "X-Correlation-ID": "deletion-cancel-idempotent-cancel-corr",
    }
    payload = {"request_id": str(request_id)}

    first = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)

    conflicting = client.post(
        "/v1/auth/account-deletion/cancel",
        headers=headers,
        json={"request_id": str(uuid4())},
    )
    conflict_error = _extract_error_detail(conflicting)
    assert conflicting.status_code == 409
    assert conflict_error["error_code"] == "idempotency_key_conflict"
    assert conflict_error["reason"] == "idempotency_key_conflict"


def test_account_deletion_execute_positive_with_session_revocation_and_tombstoning(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-positive@example.com",
        phone_number="+254733220201",
        correlation_id="deletion-execute-positive-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-execute-positive-request-idem",
        request_correlation_id="deletion-execute-positive-request-corr",
        confirm_idempotency_key="deletion-execute-positive-confirm-idem",
        confirm_correlation_id="deletion-execute-positive-confirm-corr",
    )
    account_deletion_store.force_request_cooldown_expired(request_id=request_id)
    session_one = account_deletion_store.issue_test_session_for_user(user_id=user_id)
    session_two = account_deletion_store.issue_test_session_for_user(user_id=user_id)

    execute_response = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-execute-positive-execute-idem",
            "X-Correlation-ID": "deletion-execute-positive-execute-corr",
        },
        json={"request_id": str(request_id)},
    )
    payload = _response_json(execute_response)
    assert execute_response.status_code == 200
    assert payload["status"] == "executed"
    assert payload["deletion_state"] == "executed"
    assert payload["execution_outcome"] == "tombstoned"
    assert payload["request_id"] == str(request_id)
    assert payload["revoked_session_count"] == 2
    assert cast(str, payload["executed_at"]).endswith("Z")

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "executed"
    assert persisted_request.execution_outcome == "tombstoned"
    assert persisted_request.revoked_session_count == 2

    persisted_user = registration_store.get_user_by_id(user_id=user_id)
    assert persisted_user is not None
    assert persisted_user.account_state == "disabled"
    assert persisted_user.deletion_lifecycle_state == "tombstoned"
    assert persisted_user.credentials_invalidated_at is not None
    assert persisted_user.anonymized_at is not None
    assert persisted_user.email_normalized.endswith("@deleted.invalid")
    assert persisted_user.phone_number_normalized.startswith("+9")
    assert not registration_store.is_password_valid(user_id=user_id, password="StrongPassw0rd!")

    assert account_deletion_store.get_active_session_count_for_user(user_id=user_id) == 0
    assert not account_deletion_store.is_session_active(session_id=session_one)
    assert not account_deletion_store.is_session_active(session_id=session_two)

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 3
    assert audit_events[2].action == "account_deletion_request_executed"
    assert audit_events[2].action_status == "executed"
    assert audit_events[2].event_type == "account_deletion_request_executed"
    assert audit_events[2].deletion_state == "executed"

    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(notification_records) == 3
    assert notification_records[2].event_type == "account_deletion_request_executed"
    assert notification_records[2].status == "sent"


def test_account_deletion_execute_request_not_found_is_deterministic(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-not-found@example.com",
        phone_number="+254733220202",
        correlation_id="deletion-execute-not-found-register-corr",
    )
    body = {"request_id": str(uuid4())}
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-execute-not-found-idem",
        "X-Correlation-ID": "deletion-execute-not-found-corr",
    }

    first = client.post("/v1/auth/account-deletion/execute", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/execute", headers=headers, json=body)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 404
    assert first_error["error_code"] == "account_deletion_execute_request_not_found"
    assert first_error["reason"] == "account_deletion_execute_request_not_found"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_execute_invalid_state_and_cooldown_guard(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-state@example.com",
        phone_number="+254733220203",
        correlation_id="deletion-execute-state-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-execute-state-request-idem",
        correlation_id="deletion-execute-state-request-corr",
        request_reason="Execution state guard validation.",
    )
    invalid_state = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-execute-state-invalid-idem",
            "X-Correlation-ID": "deletion-execute-state-invalid-corr",
        },
        json={"request_id": str(request_id)},
    )
    invalid_state_error = _extract_error_detail(invalid_state)
    assert invalid_state.status_code == 409
    assert invalid_state_error["error_code"] == "account_deletion_execute_invalid_state"
    assert invalid_state_error["reason"] == "account_deletion_execute_invalid_state"
    notifications_after_invalid_state = account_deletion_store.get_notification_records_for_user(
        user_id=user_id
    )
    assert len(notifications_after_invalid_state) == 1
    assert notifications_after_invalid_state[0].event_type == "account_deletion_request_created"

    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    confirm_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-execute-state-confirm-idem",
            "X-Correlation-ID": "deletion-execute-state-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    assert confirm_response.status_code == 200

    cooldown_active = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-execute-state-cooldown-idem",
            "X-Correlation-ID": "deletion-execute-state-cooldown-corr",
        },
        json={"request_id": str(request_id)},
    )
    cooldown_error = _extract_error_detail(cooldown_active)
    assert cooldown_active.status_code == 409
    assert cooldown_error["error_code"] == "account_deletion_execute_not_allowed"
    assert cooldown_error["reason"] == "account_deletion_execute_not_allowed"
    notifications_after_cooldown_reject = account_deletion_store.get_notification_records_for_user(
        user_id=user_id
    )
    assert len(notifications_after_cooldown_reject) == 2
    assert (
        notifications_after_cooldown_reject[-1].event_type == "account_deletion_request_confirmed"
    )


def test_account_deletion_execute_idempotent_replay_and_conflicts(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-idem@example.com",
        phone_number="+254733220204",
        correlation_id="deletion-execute-idem-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-execute-idem-request-idem",
        request_correlation_id="deletion-execute-idem-request-corr",
        confirm_idempotency_key="deletion-execute-idem-confirm-idem",
        confirm_correlation_id="deletion-execute-idem-confirm-corr",
    )
    account_deletion_store.force_request_cooldown_expired(request_id=request_id)
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-execute-idem-execute-idem",
        "X-Correlation-ID": "deletion-execute-idem-execute-corr",
    }
    payload = {"request_id": str(request_id)}

    first = client.post("/v1/auth/account-deletion/execute", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/execute", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)
    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(audit_events) == 3
    assert len(notification_records) == 3

    conflicting = client.post(
        "/v1/auth/account-deletion/execute",
        headers=headers,
        json={"request_id": str(uuid4())},
    )
    conflict_error = _extract_error_detail(conflicting)
    assert conflicting.status_code == 409
    assert conflict_error["error_code"] == "idempotency_key_conflict"
    assert conflict_error["reason"] == "idempotency_key_conflict"

    already_completed = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-execute-idem-fresh-key",
            "X-Correlation-ID": "deletion-execute-idem-fresh-corr",
        },
        json=payload,
    )
    already_completed_error = _extract_error_detail(already_completed)
    assert already_completed.status_code == 409
    assert already_completed_error["error_code"] == "account_deletion_execute_already_completed"
    assert already_completed_error["reason"] == "account_deletion_execute_already_completed"


def test_account_deletion_confirm_request_not_found_is_deterministic(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-not-found@example.com",
        phone_number="+254733220002",
        correlation_id="deletion-confirm-not-found-register-corr",
    )
    request_id = uuid4()
    body = {
        "request_id": str(request_id),
        "reauth_proof": "reauth:missing",
        "otp_verification_id": str(uuid4()),
    }
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-confirm-not-found-idem",
        "X-Correlation-ID": "deletion-confirm-not-found-corr",
    }

    first = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=body)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 404
    assert first_error["error_code"] == "account_deletion_confirm_request_not_found"
    assert first_error["reason"] == "account_deletion_confirm_request_not_found"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_confirm_invalid_state_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-state@example.com",
        phone_number="+254733220003",
        correlation_id="deletion-confirm-state-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-state-request-idem",
        correlation_id="deletion-confirm-state-request-corr",
        request_reason="State transition validation.",
    )

    first_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    first_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    confirmed = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-state-first-confirm-idem",
            "X-Correlation-ID": "deletion-confirm-state-first-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": first_reauth,
            "otp_verification_id": str(first_otp),
        },
    )
    assert confirmed.status_code == 200

    second_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    second_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    invalid_state = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-state-second-confirm-idem",
            "X-Correlation-ID": "deletion-confirm-state-second-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": second_reauth,
            "otp_verification_id": str(second_otp),
        },
    )
    invalid_state_error = _extract_error_detail(invalid_state)

    assert invalid_state.status_code == 409
    assert invalid_state_error["error_code"] == "account_deletion_confirm_invalid_state"
    assert invalid_state_error["reason"] == "account_deletion_confirm_invalid_state"


def test_account_deletion_confirm_reauth_invalid_and_expired_are_rejected(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-reauth@example.com",
        phone_number="+254733220004",
        correlation_id="deletion-confirm-reauth-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-reauth-request-idem",
        correlation_id="deletion-confirm-reauth-request-corr",
        request_reason="Re-auth proof validation.",
    )
    valid_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )

    invalid_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-reauth-invalid-idem",
            "X-Correlation-ID": "deletion-confirm-reauth-invalid-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": "reauth:invalid",
            "otp_verification_id": str(valid_otp),
        },
    )
    invalid_error = _extract_error_detail(invalid_response)
    assert invalid_response.status_code == 409
    assert invalid_error["error_code"] == "account_deletion_confirm_reauth_invalid"
    assert invalid_error["reason"] == "account_deletion_confirm_reauth_invalid"

    expired_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
        ttl_seconds=-1,
    )
    fresh_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    expired_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-reauth-expired-idem",
            "X-Correlation-ID": "deletion-confirm-reauth-expired-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": expired_reauth,
            "otp_verification_id": str(fresh_otp),
        },
    )
    expired_error = _extract_error_detail(expired_response)
    assert expired_response.status_code == 409
    assert expired_error["error_code"] == "account_deletion_confirm_reauth_expired"
    assert expired_error["reason"] == "account_deletion_confirm_reauth_expired"


def test_account_deletion_confirm_otp_invalid_and_expired_are_rejected(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-otp@example.com",
        phone_number="+254733220005",
        correlation_id="deletion-confirm-otp-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-otp-request-idem",
        correlation_id="deletion-confirm-otp-request-corr",
        request_reason="OTP proof validation.",
    )
    valid_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )

    invalid_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-otp-invalid-idem",
            "X-Correlation-ID": "deletion-confirm-otp-invalid-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": valid_reauth,
            "otp_verification_id": str(uuid4()),
        },
    )
    invalid_error = _extract_error_detail(invalid_response)
    assert invalid_response.status_code == 409
    assert invalid_error["error_code"] == "account_deletion_confirm_otp_invalid"
    assert invalid_error["reason"] == "account_deletion_confirm_otp_invalid"

    fresh_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    expired_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
        ttl_seconds=-1,
    )
    expired_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-otp-expired-idem",
            "X-Correlation-ID": "deletion-confirm-otp-expired-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": fresh_reauth,
            "otp_verification_id": str(expired_otp),
        },
    )
    expired_error = _extract_error_detail(expired_response)
    assert expired_response.status_code == 409
    assert expired_error["error_code"] == "account_deletion_confirm_otp_expired"
    assert expired_error["reason"] == "account_deletion_confirm_otp_expired"


def test_account_deletion_confirm_proof_context_mismatch_is_rejected(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-mismatch@example.com",
        phone_number="+254733220006",
        correlation_id="deletion-confirm-mismatch-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-mismatch-request-idem",
        correlation_id="deletion-confirm-mismatch-request-corr",
        request_reason="Proof context mismatch validation.",
    )
    mismatched_request_id = uuid4()
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=mismatched_request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )

    response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-confirm-mismatch-confirm-idem",
            "X-Correlation-ID": "deletion-confirm-mismatch-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    error = _extract_error_detail(response)

    assert response.status_code == 409
    assert error["error_code"] == "account_deletion_confirm_proof_context_mismatch"
    assert error["reason"] == "account_deletion_confirm_proof_context_mismatch"


def test_account_deletion_confirm_idempotent_replay_and_conflict(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-idempotent@example.com",
        phone_number="+254733220007",
        correlation_id="deletion-confirm-idempotent-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-idempotent-request-idem",
        correlation_id="deletion-confirm-idempotent-request-corr",
        request_reason="Idempotency validation.",
    )
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-confirm-idempotent-confirm-idem",
        "X-Correlation-ID": "deletion-confirm-idempotent-confirm-corr",
    }
    payload = {
        "request_id": str(request_id),
        "reauth_proof": reauth_proof,
        "otp_verification_id": str(otp_verification_id),
    }

    first = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)

    conflict_payload = {
        "request_id": str(request_id),
        "reauth_proof": reauth_proof,
        "otp_verification_id": str(uuid4()),
    }
    conflict = client.post(
        "/v1/auth/account-deletion/confirm", headers=headers, json=conflict_payload
    )
    conflict_error = _extract_error_detail(conflict)
    assert conflict.status_code == 409
    assert conflict_error["error_code"] == "idempotency_key_conflict"
    assert conflict_error["reason"] == "idempotency_key_conflict"


def test_account_deletion_lifecycle_regression_cancel_then_execute_paths(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-lifecycle-regression@example.com",
        phone_number="+254733220130",
        correlation_id="deletion-lifecycle-regression-register-corr",
    )

    first_request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-lifecycle-regression-request-1-idem",
        request_correlation_id="deletion-lifecycle-regression-request-1-corr",
        confirm_idempotency_key="deletion-lifecycle-regression-confirm-1-idem",
        confirm_correlation_id="deletion-lifecycle-regression-confirm-1-corr",
    )

    first_cancel = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-lifecycle-regression-cancel-1-idem",
            "X-Correlation-ID": "deletion-lifecycle-regression-cancel-1-corr",
        },
        json={"request_id": str(first_request_id)},
    )
    first_cancel_payload = _response_json(first_cancel)
    assert first_cancel.status_code == 200
    assert first_cancel_payload["status"] == "cancelled"
    assert first_cancel_payload["deletion_state"] == "cancelled"

    second_request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_idempotency_key="deletion-lifecycle-regression-request-2-idem",
        request_correlation_id="deletion-lifecycle-regression-request-2-corr",
        confirm_idempotency_key="deletion-lifecycle-regression-confirm-2-idem",
        confirm_correlation_id="deletion-lifecycle-regression-confirm-2-corr",
    )
    account_deletion_store.force_request_cooldown_expired(request_id=second_request_id)
    account_deletion_store.issue_test_session_for_user(user_id=user_id)

    execute_headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-lifecycle-regression-execute-idem",
        "X-Correlation-ID": "deletion-lifecycle-regression-execute-corr",
    }
    execute_payload = {"request_id": str(second_request_id)}
    first_execute = client.post(
        "/v1/auth/account-deletion/execute",
        headers=execute_headers,
        json=execute_payload,
    )
    second_execute = client.post(
        "/v1/auth/account-deletion/execute",
        headers=execute_headers,
        json=execute_payload,
    )
    first_execute_payload = _response_json(first_execute)
    second_execute_payload = _response_json(second_execute)
    assert first_execute.status_code == 200
    assert first_execute_payload["status"] == "executed"
    assert first_execute_payload["deletion_state"] == "executed"
    assert first_execute_payload["execution_outcome"] == "tombstoned"
    assert first_execute_payload["revoked_session_count"] == 1
    assert canonical_json_dumps(second_execute_payload) == canonical_json_dumps(
        first_execute_payload
    )

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    notification_records = account_deletion_store.get_notification_records_for_user(user_id=user_id)
    assert len(audit_events) == 6
    assert len(notification_records) == 6
    assert [event.event_type for event in audit_events] == [
        "account_deletion_request_created",
        "account_deletion_request_confirmed",
        "account_deletion_request_cancelled",
        "account_deletion_request_created",
        "account_deletion_request_confirmed",
        "account_deletion_request_executed",
    ]
    assert [record.status for record in notification_records] == [
        "queued",
        "queued",
        "queued",
        "queued",
        "queued",
        "sent",
    ]

    first_event_id = audit_events[0].event_id
    first_notification_id = notification_records[0].notification_id
    assert (
        account_deletion_store.get_audit_events_for_user(user_id=user_id)[0].event_id
        == first_event_id
    )
    assert (
        account_deletion_store.get_notification_records_for_user(user_id=user_id)[0].notification_id
        == first_notification_id
    )


def test_account_deletion_confirm_blocked_request_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-blocked-state@example.com",
        phone_number="+254733220131",
        correlation_id="deletion-confirm-blocked-state-register-corr",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user_id,
        tenant_id="default_tenant",
        compliance_lock=True,
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-confirm-blocked-state-request-idem",
        correlation_id="deletion-confirm-blocked-state-request-corr",
        request_reason="Blocked request should not be confirmable.",
    )
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )

    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-confirm-blocked-state-confirm-idem",
        "X-Correlation-ID": "deletion-confirm-blocked-state-confirm-corr",
    }
    payload = {
        "request_id": str(request_id),
        "reauth_proof": reauth_proof,
        "otp_verification_id": str(otp_verification_id),
    }
    first = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 409
    assert first_error["error_code"] == "account_deletion_confirm_invalid_state"
    assert first_error["reason"] == "account_deletion_confirm_invalid_state"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_confirm_wrong_user_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-owner@example.com",
        phone_number="+254733220132",
        correlation_id="deletion-confirm-owner-register-corr",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-confirm-attacker@example.com",
        phone_number="+254733220133",
        correlation_id="deletion-confirm-attacker-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=owner_user_id,
        idempotency_key="deletion-confirm-owner-request-idem",
        correlation_id="deletion-confirm-owner-request-corr",
        request_reason="Owner-only confirmation path.",
    )
    owner_reauth = account_deletion_store.issue_test_reauth_proof(
        user_id=owner_user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    owner_otp = account_deletion_store.issue_test_otp_verification_proof(
        user_id=owner_user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    body = {
        "request_id": str(request_id),
        "reauth_proof": owner_reauth,
        "otp_verification_id": str(owner_otp),
    }
    headers = {
        "Authorization": _auth_header(user_id=attacker_user_id),
        "Idempotency-Key": "deletion-confirm-wrong-user-idem",
        "X-Correlation-ID": "deletion-confirm-wrong-user-corr",
    }
    first = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/confirm", headers=headers, json=body)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 404
    assert first_error["error_code"] == "account_deletion_confirm_request_not_found"
    assert first_error["reason"] == "account_deletion_confirm_request_not_found"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_execute_wrong_user_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-owner@example.com",
        phone_number="+254733220134",
        correlation_id="deletion-execute-owner-register-corr",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion-execute-attacker@example.com",
        phone_number="+254733220135",
        correlation_id="deletion-execute-attacker-register-corr",
    )
    request_id = _create_confirmed_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=owner_user_id,
        request_idempotency_key="deletion-execute-owner-request-idem",
        request_correlation_id="deletion-execute-owner-request-corr",
        confirm_idempotency_key="deletion-execute-owner-confirm-idem",
        confirm_correlation_id="deletion-execute-owner-confirm-corr",
    )
    account_deletion_store.force_request_cooldown_expired(request_id=request_id)
    payload = {"request_id": str(request_id)}
    headers = {
        "Authorization": _auth_header(user_id=attacker_user_id),
        "Idempotency-Key": "deletion-execute-wrong-user-idem",
        "X-Correlation-ID": "deletion-execute-wrong-user-corr",
    }
    first = client.post("/v1/auth/account-deletion/execute", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/execute", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 404
    assert first_error["error_code"] == "account_deletion_execute_request_not_found"
    assert first_error["reason"] == "account_deletion_execute_request_not_found"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
    correlation_id: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": correlation_id},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    user_id = UUID(cast(str, payload["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-30T12:00:00Z",
    )
    return user_id


def _create_deletion_request(
    *,
    client: TestClient,
    user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    request_reason: str,
) -> UUID:
    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        },
        json={"request_reason": request_reason},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return UUID(cast(str, payload["request_id"]))


def _create_confirmed_deletion_request(
    *,
    client: TestClient,
    account_deletion_store: InMemoryAccountDeletionRequestStore,
    user_id: UUID,
    request_idempotency_key: str,
    request_correlation_id: str,
    confirm_idempotency_key: str,
    confirm_correlation_id: str,
) -> UUID:
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key=request_idempotency_key,
        correlation_id=request_correlation_id,
        request_reason="Confirmed request for cancel-path testing.",
    )
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": confirm_idempotency_key,
            "X-Correlation-ID": confirm_correlation_id,
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    assert response.status_code == 200
    return request_id


def _auth_header(*, user_id: UUID) -> str:
    return f"Bearer user_id={user_id};tenant_id=default_tenant;role=IndividualTaxpayer"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
