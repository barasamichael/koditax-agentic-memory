"""Execute DB enforcement checks against a real PostgreSQL instance."""

from __future__ import annotations

import os
from uuid import UUID
from uuid import uuid4
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Iterator

import pytest
import psycopg

DATABASE_URL_ENV_VAR = "DATABASE_URL"


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create a per-test PostgreSQL connection transaction boundary.

    :return: Iterator yielding an active psycopg connection.
    """

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL enforcement tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_audit_events_delete_is_blocked(db_connection: psycopg.Connection) -> None:
    """Verify DELETE on audit_events is rejected by append-only trigger.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    event_id = _insert_audit_event(db_connection, user_id)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM audit_events WHERE id = %s", (event_id,))


def test_audit_events_update_is_blocked(db_connection: psycopg.Connection) -> None:
    """Verify UPDATE on audit_events is rejected by append-only trigger.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    event_id = _insert_audit_event(db_connection, user_id)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE audit_events SET event_type = %s WHERE id = %s",
                ("modified", event_id),
            )


def test_audit_events_idempotency_key_must_be_unique_when_present(
    db_connection: psycopg.Connection,
) -> None:
    """Verify audit_events idempotency_key is unique when provided.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    idempotency_key = f"audit-idem-{uuid4()}"
    _insert_audit_event(db_connection, user_id, idempotency_key=idempotency_key)

    with pytest.raises(psycopg.Error):
        _insert_audit_event(db_connection, user_id, idempotency_key=idempotency_key)


def test_audit_events_allow_multiple_null_idempotency_keys(
    db_connection: psycopg.Connection,
) -> None:
    """Verify audit_events allows multiple rows with NULL idempotency_key.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    _insert_audit_event(db_connection, user_id, idempotency_key=None)
    _insert_audit_event(db_connection, user_id, idempotency_key=None)

    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM audit_events WHERE user_id = %s AND idempotency_key IS NULL",
            (user_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) >= 2


def test_audit_event_default_event_timestamp_is_assigned(
    db_connection: psycopg.Connection,
) -> None:
    """Verify audit_events uses DB default event_timestamp on insert.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    event_id = _insert_audit_event(db_connection, user_id)

    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_timestamp <= now() FROM audit_events WHERE id = %s",
            (event_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(bool, row[0]) is True


def test_audit_hash_chain_first_event_succeeds(db_connection: psycopg.Connection) -> None:
    """Verify first chain event stores NULL previous hash and computed event hash.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    resource_id = uuid4()
    event_id = _insert_audit_event(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT previous_event_hash, event_hash FROM audit_events WHERE id = %s",
            (event_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert row[0] is None
    assert cast(str, row[1]) != ""


def test_audit_hash_chain_second_event_with_correct_previous_hash_succeeds(
    db_connection: psycopg.Connection,
) -> None:
    """Verify second chain event succeeds with matching previous_event_hash.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    resource_id = uuid4()
    first_event_id = _insert_audit_event(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
    )
    first_event_hash = _get_event_hash(db_connection, first_event_id)

    _insert_audit_event(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
        previous_event_hash=first_event_hash,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE user_id = %s
              AND resource_type = %s
              AND resource_id = %s
            """,
            (
                user_id,
                "submission",
                resource_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 2


def test_audit_hash_chain_wrong_previous_hash_is_rejected(
    db_connection: psycopg.Connection,
) -> None:
    """Verify insert fails when previous_event_hash does not match chain tip.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    resource_id = uuid4()
    _insert_audit_event(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
    )

    with pytest.raises(psycopg.Error):
        _insert_audit_event(
            connection=db_connection,
            user_id=user_id,
            resource_id=resource_id,
            previous_event_hash=f"wrong-{uuid4()}",
        )


def test_audit_hash_chain_tampered_event_hash_is_rejected(
    db_connection: psycopg.Connection,
) -> None:
    """Verify insert fails when caller supplies a tampered event_hash value.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        _insert_audit_event(
            connection=db_connection,
            user_id=user_id,
            event_hash="tampered",
        )


def test_audit_hash_chain_is_independent_across_users(
    db_connection: psycopg.Connection,
) -> None:
    """Verify users with same resource_id maintain independent hash chains.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    shared_resource_id = uuid4()

    event_a = _insert_audit_event(
        connection=db_connection,
        user_id=user_a,
        resource_id=shared_resource_id,
    )
    event_b = _insert_audit_event(
        connection=db_connection,
        user_id=user_b,
        resource_id=shared_resource_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_id, previous_event_hash
            FROM audit_events
            WHERE id IN (%s, %s)
            ORDER BY user_id
            """,
            (
                event_a,
                event_b,
            ),
        )
        rows = cursor.fetchall()

    assert len(rows) == 2
    assert rows[0][1] is None
    assert rows[1][1] is None


def test_audit_event_monotonic_chain_rejects_timestamp_regression(
    db_connection: psycopg.Connection,
) -> None:
    """Verify audit_events rejects event_timestamp regression per aggregate.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    resource_id = uuid4()
    first_event_timestamp = datetime.now(UTC) - timedelta(minutes=1)
    regressed_timestamp = first_event_timestamp - timedelta(seconds=1)

    _insert_audit_event_with_timestamp(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
        event_timestamp=first_event_timestamp,
    )

    with pytest.raises(psycopg.Error):
        _insert_audit_event_with_timestamp(
            connection=db_connection,
            user_id=user_id,
            resource_id=resource_id,
            event_timestamp=regressed_timestamp,
        )


def test_audit_event_monotonic_chain_accepts_non_regressing_timestamps(
    db_connection: psycopg.Connection,
) -> None:
    """Verify audit_events accepts monotonic event_timestamp ordering per aggregate.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    resource_id = uuid4()
    first_event_timestamp = datetime.now(UTC) - timedelta(minutes=2)
    second_event_timestamp = first_event_timestamp + timedelta(minutes=1)

    _insert_audit_event_with_timestamp(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
        event_timestamp=first_event_timestamp,
    )
    _insert_audit_event_with_timestamp(
        connection=db_connection,
        user_id=user_id,
        resource_id=resource_id,
        event_timestamp=second_event_timestamp,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE user_id = %s
              AND resource_type = %s
              AND resource_id = %s
            """,
            (
                user_id,
                "submission",
                resource_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 2


def test_audit_event_monotonic_chain_is_scoped_per_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify monotonicity is enforced per user for the same aggregate identity.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    resource_id = uuid4()
    later_timestamp = datetime.now(UTC) - timedelta(minutes=1)
    earlier_timestamp = later_timestamp - timedelta(seconds=1)

    _insert_audit_event_with_timestamp(
        connection=db_connection,
        user_id=user_a,
        resource_id=resource_id,
        event_timestamp=later_timestamp,
    )
    _insert_audit_event_with_timestamp(
        connection=db_connection,
        user_id=user_b,
        resource_id=resource_id,
        event_timestamp=earlier_timestamp,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE resource_type = %s
              AND resource_id = %s
            """,
            (
                "submission",
                resource_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 2


def test_delegations_reject_duplicate_active_pair(
    db_connection: psycopg.Connection,
) -> None:
    """Verify delegations rejects a second active row for the same user pair.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    principal_user_id = _insert_user(db_connection)
    delegate_user_id = _insert_user(db_connection)
    _insert_delegation(
        connection=db_connection,
        principal_user_id=principal_user_id,
        delegate_user_id=delegate_user_id,
    )

    with pytest.raises(psycopg.Error):
        _insert_delegation(
            connection=db_connection,
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )


def test_delegations_allow_new_active_row_after_revocation(
    db_connection: psycopg.Connection,
) -> None:
    """Verify delegations allows a new active row after prior delegation is revoked.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    principal_user_id = _insert_user(db_connection)
    delegate_user_id = _insert_user(db_connection)
    delegation_id = _insert_delegation(
        connection=db_connection,
        principal_user_id=principal_user_id,
        delegate_user_id=delegate_user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE delegations
            SET revoked_at = now(),
                is_active = FALSE
            WHERE id = %s
            """,
            (delegation_id,),
        )

    _insert_delegation(
        connection=db_connection,
        principal_user_id=principal_user_id,
        delegate_user_id=delegate_user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE is_active) AS active_count,
                   COUNT(*) AS total_count
            FROM delegations
            WHERE principal_user_id = %s
              AND delegate_user_id = %s
            """,
            (
                principal_user_id,
                delegate_user_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 1
    assert cast(int, row[1]) == 2


def test_delegations_reject_revoked_at_with_active_true(
    db_connection: psycopg.Connection,
) -> None:
    """Verify delegations rejects rows where revoked_at is set but is_active is TRUE.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    principal_user_id = _insert_user(db_connection)
    delegate_user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO delegations (
                    principal_user_id,
                    delegate_user_id,
                    revoked_at,
                    is_active
                ) VALUES (%s, %s, now(), TRUE)
                """,
                (
                    principal_user_id,
                    delegate_user_id,
                ),
            )


def test_delegations_reject_inactive_without_revoked_at(
    db_connection: psycopg.Connection,
) -> None:
    """Verify delegations rejects inactive rows that do not have revoked_at.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    principal_user_id = _insert_user(db_connection)
    delegate_user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO delegations (
                    principal_user_id,
                    delegate_user_id,
                    revoked_at,
                    is_active
                ) VALUES (%s, %s, NULL, FALSE)
                """,
                (
                    principal_user_id,
                    delegate_user_id,
                ),
            )


def test_delegations_reject_reactivation_of_revoked_row(
    db_connection: psycopg.Connection,
) -> None:
    """Verify revoked delegations cannot be reactivated via UPDATE.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    principal_user_id = _insert_user(db_connection)
    delegate_user_id = _insert_user(db_connection)
    delegation_id = _insert_delegation(
        connection=db_connection,
        principal_user_id=principal_user_id,
        delegate_user_id=delegate_user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE delegations
            SET revoked_at = now(),
                is_active = FALSE
            WHERE id = %s
            """,
            (delegation_id,),
        )

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE delegations SET is_active = TRUE WHERE id = %s",
                (delegation_id,),
            )


def test_form_requires_computation_id(db_connection: psycopg.Connection) -> None:
    """Verify forms cannot be inserted with a NULL computation_id.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO forms (
                    user_id,
                    computation_id,
                    form_type,
                    form_version,
                    retention_expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    None,
                    "itax",
                    "v1",
                    _future_timestamp(),
                ),
            )


def test_form_user_must_match_computation_user(db_connection: psycopg.Connection) -> None:
    """Verify forms reject cross-user computation lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    computation_a = _insert_computation(db_connection, user_id=user_a, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO forms (
                    user_id,
                    computation_id,
                    form_type,
                    form_version,
                    retention_expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_b,
                    computation_a,
                    "itax",
                    "v1",
                    _future_timestamp(),
                ),
            )


def test_form_document_user_must_match_form_user(db_connection: psycopg.Connection) -> None:
    """Verify forms reject cross-user document lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    document_a = _insert_document(db_connection, user_id=user_a, state="uploaded")
    computation_b = _insert_computation(db_connection, user_id=user_b, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        _insert_form(
            connection=db_connection,
            user_id=user_b,
            computation_id=computation_b,
            document_id=document_a,
        )


def test_form_document_same_user_insert_succeeds(db_connection: psycopg.Connection) -> None:
    """Verify forms accept document lineage when user ownership matches.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")
    _insert_form(
        connection=db_connection,
        user_id=user_id,
        computation_id=computation_id,
        document_id=document_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM forms
            WHERE user_id = %s
              AND computation_id = %s
              AND document_id = %s
            """,
            (
                user_id,
                computation_id,
                document_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 1


def test_submission_requires_confirmation_event_id(db_connection: psycopg.Connection) -> None:
    """Verify submissions cannot be inserted with a NULL confirmation_event_id.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO submissions (
                    user_id,
                    computation_id,
                    confirmation_event_id,
                    idempotency_key,
                    status
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    computation_id,
                    None,
                    f"sub-{uuid4()}",
                    "pending",
                ),
            )


def test_submission_user_must_match_confirmation_event_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify submissions reject cross-user confirmation event lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    _insert_computation(db_connection, user_id=user_a, regime_type="income_tax")
    computation_b = _insert_computation(db_connection, user_id=user_b, regime_type="income_tax")
    confirmation_event_for_a = _insert_audit_event(db_connection, user_a)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO submissions (
                    user_id,
                    computation_id,
                    confirmation_event_id,
                    idempotency_key,
                    status
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_b,
                    computation_b,
                    confirmation_event_for_a,
                    f"sub-{uuid4()}",
                    "pending",
                ),
            )


def test_submission_pending_allows_protected_field_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify protected submission fields can change before confirmation.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    context = _create_submission_immutability_context(db_connection, user_id)

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE submissions
            SET confirmation_event_id = %s
            WHERE id = %s
            """,
            (
                context["confirmation_event_id_2"],
                context["submission_id"],
            ),
        )
        cursor.execute(
            "SELECT confirmation_event_id FROM submissions WHERE id = %s",
            (context["submission_id"],),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(UUID, row[0]) == context["confirmation_event_id_2"]


def test_submission_confirmed_blocks_form_id_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify confirmed submissions reject form_id updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    context = _create_submission_immutability_context(db_connection, user_id)
    _confirm_submission(db_connection, context["submission_id"])

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE submissions SET form_id = %s WHERE id = %s",
                (
                    context["form_id_2"],
                    context["submission_id"],
                ),
            )


def test_submission_confirmed_blocks_report_id_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify confirmed submissions reject report_id updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    context = _create_submission_immutability_context(db_connection, user_id)
    _confirm_submission(db_connection, context["submission_id"])

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE submissions SET report_id = %s WHERE id = %s",
                (
                    context["report_id_2"],
                    context["submission_id"],
                ),
            )


def test_submission_confirmed_blocks_computation_id_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify confirmed submissions reject computation_id updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    context = _create_submission_immutability_context(db_connection, user_id)
    _confirm_submission(db_connection, context["submission_id"])

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE submissions SET computation_id = %s WHERE id = %s",
                (
                    context["computation_id_2"],
                    context["submission_id"],
                ),
            )


def test_submission_confirmed_blocks_confirmation_event_id_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify confirmed submissions reject confirmation_event_id updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    context = _create_submission_immutability_context(db_connection, user_id)
    _confirm_submission(db_connection, context["submission_id"])

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE submissions SET confirmation_event_id = %s WHERE id = %s",
                (
                    context["confirmation_event_id_2"],
                    context["submission_id"],
                ),
            )


def test_computation_delete_rejected_when_submission_exists(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation deletion fails when linked submissions exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_unlocked_computation(
        connection=db_connection,
        user_id=user_id,
        regime_type="income_tax",
    )
    confirmation_event_id = _insert_audit_event(db_connection, user_id)
    _insert_submission_row(
        connection=db_connection,
        user_id=user_id,
        computation_id=computation_id,
        form_id=None,
        report_id=None,
        confirmation_event_id=confirmation_event_id,
        status="pending",
    )

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM computations WHERE id = %s", (computation_id,))


def test_computation_delete_rejected_when_non_purged_document_exists(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation deletion fails when linked non-purged documents exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_unlocked_computation(
        connection=db_connection,
        user_id=user_id,
        regime_type="income_tax",
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO documents (
                user_id,
                computation_id,
                storage_key,
                state
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                user_id,
                computation_id,
                f"s3://bucket/document-{uuid4()}",
                "uploaded",
            ),
        )

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM computations WHERE id = %s", (computation_id,))


def test_computation_delete_succeeds_without_submissions_or_non_purged_documents(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation deletion succeeds when no blocking references exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_unlocked_computation(
        connection=db_connection,
        user_id=user_id,
        regime_type="income_tax",
    )

    with db_connection.cursor() as cursor:
        cursor.execute("DELETE FROM computations WHERE id = %s", (computation_id,))
        cursor.execute("SELECT COUNT(*) FROM computations WHERE id = %s", (computation_id,))
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 0


def test_user_delete_rejected_when_audit_events_exist(db_connection: psycopg.Connection) -> None:
    """Verify user deletion fails when audit events exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    _insert_audit_event(db_connection, user_id)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))


def test_user_delete_rejected_when_submissions_exist(db_connection: psycopg.Connection) -> None:
    """Verify user deletion fails when submissions exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_unlocked_computation(
        connection=db_connection,
        user_id=user_id,
        regime_type="income_tax",
    )
    confirmation_event_id = _insert_audit_event(db_connection, user_id)
    _insert_submission_row(
        connection=db_connection,
        user_id=user_id,
        computation_id=computation_id,
        form_id=None,
        report_id=None,
        confirmation_event_id=confirmation_event_id,
        status="pending",
    )

    with pytest.raises(psycopg.Error) as error_info:
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))

    assert "cannot delete user with submissions" in str(error_info.value)


def test_user_delete_succeeds_without_audit_events_or_submissions(
    db_connection: psycopg.Connection,
) -> None:
    """Verify user deletion succeeds when no blocking rows exist.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)

    with db_connection.cursor() as cursor:
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        cursor.execute("SELECT COUNT(*) FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 0


def test_computation_retention_lock_future_blocks_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify future computation compliance lock blocks updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE computations SET rule_version = %s WHERE id = %s",
                (
                    "v2",
                    computation_id,
                ),
            )


def test_computation_retention_lock_future_blocks_delete(
    db_connection: psycopg.Connection,
) -> None:
    """Verify future computation compliance lock blocks deletes.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM computations WHERE id = %s", (computation_id,))


def test_computation_retention_lock_expired_allows_update_and_delete(
    db_connection: psycopg.Connection,
) -> None:
    """Verify expired computation compliance lock allows updates and deletes.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_unlocked_computation(
        connection=db_connection,
        user_id=user_id,
        regime_type="income_tax",
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            "UPDATE computations SET rule_version = %s WHERE id = %s",
            (
                "v2",
                computation_id,
            ),
        )
        cursor.execute("DELETE FROM computations WHERE id = %s", (computation_id,))
        cursor.execute("SELECT COUNT(*) FROM computations WHERE id = %s", (computation_id,))
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 0


def test_document_retention_lock_future_blocks_update(
    db_connection: psycopg.Connection,
) -> None:
    """Verify future document compliance lock blocks updates.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    lock_until = _db_now(db_connection) + timedelta(days=1)
    document_id = _insert_eligible_document_with_lock(
        connection=db_connection,
        user_id=user_id,
        compliance_lock_until=lock_until,
    )

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE documents SET storage_key = %s WHERE id = %s",
                (
                    f"s3://bucket/locked-{uuid4()}",
                    document_id,
                ),
            )


def test_document_retention_lock_future_blocks_delete(
    db_connection: psycopg.Connection,
) -> None:
    """Verify future document compliance lock blocks deletes.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    lock_until = _db_now(db_connection) + timedelta(days=1)
    document_id = _insert_eligible_document_with_lock(
        connection=db_connection,
        user_id=user_id,
        compliance_lock_until=lock_until,
    )

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def test_document_retention_lock_expired_allows_update_and_delete(
    db_connection: psycopg.Connection,
) -> None:
    """Verify expired document compliance lock allows updates and deletes.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    lock_until = _db_now(db_connection) - timedelta(days=1)
    document_id = _insert_eligible_document_with_lock(
        connection=db_connection,
        user_id=user_id,
        compliance_lock_until=lock_until,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            "UPDATE documents SET storage_key = %s WHERE id = %s",
            (
                f"s3://bucket/unlocked-{uuid4()}",
                document_id,
            ),
        )
        cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        cursor.execute("SELECT COUNT(*) FROM documents WHERE id = %s", (document_id,))
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 0


def test_computation_requires_rule_version(db_connection: psycopg.Connection) -> None:
    """Verify computations cannot be inserted with NULL rule_version.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    payload = _computation_payload(user_id=user_id, regime_type="income_tax")
    payload["rule_version"] = None

    with pytest.raises(psycopg.Error):
        _insert_computation_row(db_connection, payload)


def test_computation_requires_tax_year(db_connection: psycopg.Connection) -> None:
    """Verify computations cannot be inserted with NULL tax_year.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    payload = _computation_payload(user_id=user_id, regime_type="income_tax")
    payload["tax_year"] = None

    with pytest.raises(psycopg.Error):
        _insert_computation_row(db_connection, payload)


def test_health_contribution_requires_regime_identifier(
    db_connection: psycopg.Connection,
) -> None:
    """Verify health_contribution computations reject NULL regime_identifier.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    payload = _computation_payload(user_id=user_id, regime_type="health_contribution")
    payload["regime_identifier"] = None

    with pytest.raises(psycopg.Error):
        _insert_computation_row(db_connection, payload)


def test_computation_user_must_match_session_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computations rejects cross-user session lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    session_owner = _insert_user(db_connection)
    computation_owner = _insert_user(db_connection)
    session_id = _insert_session_row(db_connection, user_id=session_owner)
    payload = _computation_payload(user_id=computation_owner, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO computations (
                    user_id,
                    session_id,
                    tax_type,
                    regime_type,
                    regime_identifier,
                    tax_year,
                    rule_version,
                    input_hash,
                    idempotency_key,
                    correlation_id,
                    retention_expires_at,
                    compliance_lock_until
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload["user_id"],
                    session_id,
                    payload["tax_type"],
                    payload["regime_type"],
                    payload["regime_identifier"],
                    payload["tax_year"],
                    payload["rule_version"],
                    payload["input_hash"],
                    payload["idempotency_key"],
                    payload["correlation_id"],
                    payload["retention_expires_at"],
                    payload["compliance_lock_until"],
                ),
            )


def test_computation_results_user_must_match_computation_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation_results rejects cross-user computation lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    computation_a = _insert_computation(db_connection, user_id=user_a, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        _insert_computation_result(
            connection=db_connection,
            computation_id=computation_a,
            user_id=user_b,
        )


def test_computation_results_same_user_insert_succeeds(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation_results accepts aligned computation ownership.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")
    _insert_computation_result(
        connection=db_connection,
        computation_id=computation_id,
        user_id=user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM computation_results
            WHERE computation_id = %s
              AND user_id = %s
            """,
            (
                computation_id,
                user_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 1


def test_validations_user_must_match_computation_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify validations rejects cross-user computation lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    computation_a = _insert_computation(db_connection, user_id=user_a, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        _insert_validation(
            connection=db_connection,
            computation_id=computation_a,
            user_id=user_b,
        )


def test_validations_same_user_insert_succeeds(db_connection: psycopg.Connection) -> None:
    """Verify validations accepts aligned computation ownership.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    computation_id = _insert_computation(db_connection, user_id=user_id, regime_type="income_tax")
    _insert_validation(
        connection=db_connection,
        computation_id=computation_id,
        user_id=user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM validations
            WHERE computation_id = %s
              AND user_id = %s
            """,
            (
                computation_id,
                user_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 1


def test_document_invalid_state_transition_is_rejected(db_connection: psycopg.Connection) -> None:
    """Verify documents trigger rejects illegal state transitions.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE documents
                SET state = %s,
                    purged_at = %s,
                    purge_eligible_at = %s
                WHERE id = %s
                """,
                (
                    "purged",
                    _future_timestamp(),
                    _future_timestamp(),
                    document_id,
                ),
            )


def test_document_user_must_match_computation_user(db_connection: psycopg.Connection) -> None:
    """Verify documents reject cross-user computation lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    computation_a = _insert_computation(db_connection, user_id=user_a, regime_type="income_tax")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO documents (
                    user_id,
                    computation_id,
                    storage_key,
                    state
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    user_b,
                    computation_a,
                    f"s3://bucket/document-{uuid4()}",
                    "uploaded",
                ),
            )


def test_document_delete_requires_eligible_state(db_connection: psycopg.Connection) -> None:
    """Verify documents delete trigger rejects non-eligible deletion.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def test_document_extractions_user_must_match_document_user(
    db_connection: psycopg.Connection,
) -> None:
    """Verify document_extractions rejects cross-user document lineage.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_a = _insert_user(db_connection)
    user_b = _insert_user(db_connection)
    document_a = _insert_document(db_connection, user_id=user_a, state="uploaded")

    with pytest.raises(psycopg.Error):
        _insert_document_extraction(
            connection=db_connection,
            document_id=document_a,
            user_id=user_b,
        )


def test_document_extractions_same_user_insert_succeeds(
    db_connection: psycopg.Connection,
) -> None:
    """Verify document_extractions accepts aligned document ownership.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")
    _insert_document_extraction(
        connection=db_connection,
        document_id=document_id,
        user_id=user_id,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM document_extractions
            WHERE document_id = %s
              AND user_id = %s
            """,
            (
                document_id,
                user_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 1


def test_document_insert_with_future_created_at_is_rejected(
    db_connection: psycopg.Connection,
) -> None:
    """Verify documents rejects INSERT when created_at is in the future.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO documents (
                    user_id,
                    storage_key,
                    state,
                    created_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    user_id,
                    f"s3://bucket/document-{uuid4()}",
                    "uploaded",
                    _future_timestamp(),
                ),
            )


def test_document_eligible_for_purge_requires_purge_eligible_at(
    db_connection: psycopg.Connection,
) -> None:
    """Verify documents cannot become eligible_for_purge without purge_eligible_at.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE documents SET state = %s WHERE id = %s",
                ("eligible_for_purge", document_id),
            )


def test_document_insert_eligible_for_purge_requires_purge_eligible_at(
    db_connection: psycopg.Connection,
) -> None:
    """Verify INSERT rejects eligible_for_purge documents without purge_eligible_at.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO documents (
                    user_id,
                    storage_key,
                    state,
                    purge_eligible_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    user_id,
                    f"s3://bucket/document-{uuid4()}",
                    "eligible_for_purge",
                    None,
                ),
            )


def test_document_eligible_for_purge_requires_non_future_timestamp(
    db_connection: psycopg.Connection,
) -> None:
    """Verify documents cannot become eligible_for_purge with future eligibility.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE documents
                SET state = %s,
                    purge_eligible_at = %s
                WHERE id = %s
                """,
                (
                    "eligible_for_purge",
                    _future_timestamp(),
                    document_id,
                ),
            )


def test_document_purge_positive_path_with_eligible_state(
    db_connection: psycopg.Connection,
) -> None:
    """Verify valid eligible_for_purge transition and purge succeeds.

    :param db_connection: Active database connection fixture.
    :return: None.
    """

    user_id = _insert_user(db_connection)
    document_id = _insert_document(db_connection, user_id=user_id, state="uploaded")

    with db_connection.cursor() as cursor:
        cursor.execute("SELECT uploaded_at FROM documents WHERE id = %s", (document_id,))
        uploaded_row = cursor.fetchone()
        assert uploaded_row is not None
        eligibility_timestamp = cast(datetime, uploaded_row[0])
        cursor.execute("UPDATE documents SET state = %s WHERE id = %s", ("processing", document_id))
        cursor.execute("UPDATE documents SET state = %s WHERE id = %s", ("validated", document_id))
        cursor.execute(
            """
            UPDATE documents
            SET state = %s,
                purge_eligible_at = %s
            WHERE id = %s
            """,
            (
                "eligible_for_purge",
                eligibility_timestamp,
                document_id,
            ),
        )
        cursor.execute(
            """
            UPDATE documents
            SET state = %s,
                purged_at = %s
            WHERE id = %s
            """,
            (
                "purged",
                eligibility_timestamp,
                document_id,
            ),
        )
        cursor.execute("SELECT state, purged_at FROM documents WHERE id = %s", (document_id,))
        row = cursor.fetchone()

    assert row is not None
    assert cast(str, row[0]) == "purged"
    assert row[1] is not None


def _insert_user(connection: psycopg.Connection) -> UUID:
    suffix = uuid4().hex
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO users (
                phone_number_encrypted,
                email_encrypted,
                role
            ) VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                f"phone-{suffix}",
                f"user-{suffix}@example.com",
                "IndividualTaxpayer",
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _insert_session_row(connection: psycopg.Connection, user_id: UUID) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sessions (
                user_id,
                idempotency_key,
                expires_at,
                device_fingerprint_hash
            ) VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                f"session-{uuid4()}",
                _future_timestamp(),
                f"device-{uuid4()}",
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _insert_delegation(
    connection: psycopg.Connection,
    principal_user_id: UUID,
    delegate_user_id: UUID,
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO delegations (
                principal_user_id,
                delegate_user_id
            ) VALUES (%s, %s)
            RETURNING id
            """,
            (
                principal_user_id,
                delegate_user_id,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    return cast(UUID, row[0])


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


def _insert_audit_event(
    connection: psycopg.Connection,
    user_id: UUID,
    resource_id: UUID | None = None,
    previous_event_hash: str | None = None,
    event_hash: str | None = None,
    idempotency_key: str | None = None,
) -> UUID:
    resolved_resource_id = resource_id if resource_id is not None else uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit_events (
                user_id,
                role_at_time,
                event_type,
                resource_type,
                resource_id,
                correlation_id,
                previous_event_hash,
                idempotency_key,
                event_hash,
                retention_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                "IndividualTaxpayer",
                "created",
                "submission",
                resolved_resource_id,
                f"corr-{uuid4()}",
                previous_event_hash,
                idempotency_key,
                event_hash,
                _future_timestamp(),
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _insert_audit_event_with_timestamp(
    connection: psycopg.Connection,
    user_id: UUID,
    resource_id: UUID,
    event_timestamp: datetime,
    previous_event_hash: str | None = None,
    event_hash: str | None = None,
) -> UUID:
    resolved_previous_hash = previous_event_hash
    if resolved_previous_hash is None:
        resolved_previous_hash = _latest_chain_event_hash(
            connection=connection,
            user_id=user_id,
            resource_id=resource_id,
        )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit_events (
                user_id,
                role_at_time,
                event_type,
                resource_type,
                resource_id,
                correlation_id,
                event_timestamp,
                previous_event_hash,
                event_hash,
                retention_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                "IndividualTaxpayer",
                "created",
                "submission",
                resource_id,
                f"corr-{uuid4()}",
                event_timestamp,
                resolved_previous_hash,
                event_hash,
                _future_timestamp(),
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _get_event_hash(connection: psycopg.Connection, event_id: UUID) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT event_hash FROM audit_events WHERE id = %s", (event_id,))
        row = cursor.fetchone()

    assert row is not None
    return cast(str, row[0])


def _latest_chain_event_hash(
    connection: psycopg.Connection,
    user_id: UUID,
    resource_id: UUID,
) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_hash
            FROM audit_events
            WHERE user_id = %s
              AND resource_type = %s
              AND resource_id = %s
            ORDER BY event_timestamp DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (
                user_id,
                "submission",
                resource_id,
            ),
        )
        row = cursor.fetchone()

    if row is None:
        return None
    return cast(str, row[0])


def _insert_computation(connection: psycopg.Connection, user_id: UUID, regime_type: str) -> UUID:
    payload = _computation_payload(user_id=user_id, regime_type=regime_type)
    payload["regime_identifier"] = "sha"
    return _insert_computation_row(connection, payload)


def _insert_unlocked_computation(
    connection: psycopg.Connection,
    user_id: UUID,
    regime_type: str,
) -> UUID:
    payload = _computation_payload(user_id=user_id, regime_type=regime_type)
    payload["compliance_lock_until"] = _db_now(connection) - timedelta(days=1)
    return _insert_computation_row(connection, payload)


def _insert_computation_row(
    connection: psycopg.Connection,
    payload: dict[str, object | None],
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO computations (
                user_id,
                tax_type,
                regime_type,
                regime_identifier,
                tax_year,
                rule_version,
                input_hash,
                idempotency_key,
                correlation_id,
                retention_expires_at,
                compliance_lock_until
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload["user_id"],
                payload["tax_type"],
                payload["regime_type"],
                payload["regime_identifier"],
                payload["tax_year"],
                payload["rule_version"],
                payload["input_hash"],
                payload["idempotency_key"],
                payload["correlation_id"],
                payload["retention_expires_at"],
                payload["compliance_lock_until"],
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _computation_payload(user_id: UUID, regime_type: str) -> dict[str, object | None]:
    return {
        "user_id": user_id,
        "tax_type": "annual_return",
        "regime_type": regime_type,
        "regime_identifier": "default-regime",
        "tax_year": 2025,
        "rule_version": "v1",
        "input_hash": f"input-{uuid4()}",
        "idempotency_key": f"comp-{uuid4()}",
        "correlation_id": f"corr-{uuid4()}",
        "retention_expires_at": _future_timestamp(),
        "compliance_lock_until": _future_timestamp(),
    }


def _insert_document(connection: psycopg.Connection, user_id: UUID, state: str) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO documents (
                user_id,
                storage_key,
                state
            ) VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                f"s3://bucket/document-{uuid4()}",
                state,
            ),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(UUID, row[0])


def _insert_eligible_document_with_lock(
    connection: psycopg.Connection,
    user_id: UUID,
    compliance_lock_until: datetime,
) -> UUID:
    document_id = _insert_document(connection, user_id=user_id, state="uploaded")

    with connection.cursor() as cursor:
        cursor.execute("SELECT uploaded_at FROM documents WHERE id = %s", (document_id,))
        row = cursor.fetchone()
        assert row is not None
        uploaded_at = cast(datetime, row[0])
        cursor.execute(
            """
            UPDATE documents
            SET state = %s,
                purge_eligible_at = %s,
                compliance_lock_until = %s
            WHERE id = %s
            """,
            (
                "eligible_for_purge",
                uploaded_at,
                compliance_lock_until,
                document_id,
            ),
        )

    return document_id


def _insert_computation_result(
    connection: psycopg.Connection,
    computation_id: UUID,
    user_id: UUID,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO computation_results (
                computation_id,
                user_id,
                result_payload
            ) VALUES (%s, %s, %s::jsonb)
            """,
            (
                computation_id,
                user_id,
                "{}",
            ),
        )


def _insert_validation(
    connection: psycopg.Connection,
    computation_id: UUID,
    user_id: UUID,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO validations (
                computation_id,
                user_id,
                validation_context,
                findings
            ) VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                computation_id,
                user_id,
                "unit-test",
                "{}",
            ),
        )


def _insert_document_extraction(
    connection: psycopg.Connection,
    document_id: UUID,
    user_id: UUID,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO document_extractions (
                document_id,
                user_id,
                extracted_fields,
                confidence_scores
            ) VALUES (%s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                document_id,
                user_id,
                "{}",
                "{}",
            ),
        )


def _insert_form(
    connection: psycopg.Connection,
    user_id: UUID,
    computation_id: UUID,
    document_id: UUID | None,
) -> None:
    _insert_form_row(
        connection=connection,
        user_id=user_id,
        computation_id=computation_id,
        document_id=document_id,
    )


def _insert_form_row(
    connection: psycopg.Connection,
    user_id: UUID,
    computation_id: UUID,
    document_id: UUID | None,
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO forms (
                user_id,
                computation_id,
                document_id,
                form_type,
                form_version,
                retention_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                computation_id,
                document_id,
                "itax",
                "v1",
                _future_timestamp(),
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    return cast(UUID, row[0])


def _insert_report_row(
    connection: psycopg.Connection,
    user_id: UUID,
    computation_id: UUID,
    form_id: UUID | None,
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO reports (
                user_id,
                computation_id,
                form_id,
                report_type,
                download_expires_at
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                computation_id,
                form_id,
                "summary",
                _future_timestamp(),
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    return cast(UUID, row[0])


def _insert_submission_row(
    connection: psycopg.Connection,
    user_id: UUID,
    computation_id: UUID,
    form_id: UUID | None,
    report_id: UUID | None,
    confirmation_event_id: UUID,
    status: str,
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO submissions (
                user_id,
                computation_id,
                form_id,
                report_id,
                confirmation_event_id,
                idempotency_key,
                status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                computation_id,
                form_id,
                report_id,
                confirmation_event_id,
                f"sub-{uuid4()}",
                status,
            ),
        )
        row = cursor.fetchone()

    assert row is not None
    return cast(UUID, row[0])


def _create_submission_immutability_context(
    connection: psycopg.Connection,
    user_id: UUID,
) -> dict[str, UUID]:
    computation_id_1 = _insert_computation(connection, user_id=user_id, regime_type="income_tax")
    computation_id_2 = _insert_computation(connection, user_id=user_id, regime_type="income_tax")
    form_id_1 = _insert_form_row(connection, user_id, computation_id_1, None)
    form_id_2 = _insert_form_row(connection, user_id, computation_id_2, None)
    report_id_1 = _insert_report_row(connection, user_id, computation_id_1, form_id_1)
    report_id_2 = _insert_report_row(connection, user_id, computation_id_2, form_id_2)
    confirmation_event_id_1 = _insert_audit_event(connection, user_id)
    confirmation_event_id_2 = _insert_audit_event(connection, user_id)
    submission_id = _insert_submission_row(
        connection=connection,
        user_id=user_id,
        computation_id=computation_id_1,
        form_id=form_id_1,
        report_id=report_id_1,
        confirmation_event_id=confirmation_event_id_1,
        status="pending",
    )

    return {
        "submission_id": submission_id,
        "computation_id_2": computation_id_2,
        "form_id_2": form_id_2,
        "report_id_2": report_id_2,
        "confirmation_event_id_2": confirmation_event_id_2,
    }


def _confirm_submission(connection: psycopg.Connection, submission_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE submissions SET status = %s WHERE id = %s",
            (
                "confirmed",
                submission_id,
            ),
        )


def _db_now(connection: psycopg.Connection) -> datetime:
    with connection.cursor() as cursor:
        cursor.execute("SELECT now()")
        row = cursor.fetchone()

    assert row is not None
    return cast(datetime, row[0])


def _future_timestamp() -> datetime:
    return datetime.now(UTC) + timedelta(days=365)
