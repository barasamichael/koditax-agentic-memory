"""Verify computation finalization immutability and idempotent finalization behavior."""

from __future__ import annotations

import os
from uuid import UUID
from uuid import uuid4
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg

from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.finalization import finalize_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationFinalizationContext
from services.tax_core.app.engine.execution_contract import ComputationFinalizationRequest
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL finalization tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_finalization_succeeds_and_writes_audit_link(
    db_connection: psycopg.Connection,
) -> None:
    """Verify finalize persists state and links computation.finalized audit evidence."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)
    finalization_context = _build_finalization_context(user_id)

    finalization_result = finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=finalization_context,
        connection=db_connection,
    )

    assert finalization_result.status == "ok"
    assert finalization_result.finalization_status == "finalized"
    assert finalization_result.computation_id == computation_id

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT finalized_at, finalized_audit_event_id
            FROM computations
            WHERE id = %s
            """,
            (computation_id,),
        )
        finalized_row = cursor.fetchone()
        cursor.execute(
            """
            SELECT event_type, details->>'finalization_status'
            FROM audit_events
            WHERE id = %s
            """,
            (finalization_result.finalized_audit_event_id,),
        )
        audit_row = cursor.fetchone()

    assert finalized_row is not None
    assert cast(object, finalized_row[0]) is not None
    assert cast(UUID, finalized_row[1]) == finalization_result.finalized_audit_event_id
    assert audit_row is not None
    assert cast(str, audit_row[0]) == "computation.finalized"
    assert cast(str, audit_row[1]) == "finalized"


def test_finalized_computation_result_payload_cannot_be_updated(
    db_connection: psycopg.Connection,
) -> None:
    """Verify DB blocks mutation of finalized computation result payload."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)
    _finalize_computation(db_connection, computation_id, user_id)

    with pytest.raises(
        psycopg.Error,
        match="cannot mutate computation_results for finalized computation",
    ):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE computation_results
                SET result_payload = '{}'::jsonb
                WHERE computation_id = %s
                """,
                (computation_id,),
            )


def test_finalized_validation_row_cannot_be_updated_or_deleted(
    db_connection: psycopg.Connection,
) -> None:
    """Verify DB blocks UPDATE and DELETE against finalized validation rows."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)
    validation_id = _insert_validation_row(db_connection, computation_id, user_id)
    _finalize_computation(db_connection, computation_id, user_id)

    with pytest.raises(
        psycopg.Error,
        match="cannot mutate validations for finalized computation",
    ):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE validations
                SET findings = '{"status":"changed"}'::jsonb
                WHERE id = %s
                """,
                (validation_id,),
            )
    db_connection.rollback()

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)
    validation_id = _insert_validation_row(db_connection, computation_id, user_id)
    _finalize_computation(db_connection, computation_id, user_id)

    with pytest.raises(
        psycopg.Error,
        match="cannot mutate validations for finalized computation",
    ):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM validations
                WHERE id = %s
                """,
                (validation_id,),
            )


def test_finalized_audit_link_cannot_be_replaced(
    db_connection: psycopg.Connection,
) -> None:
    """Verify DB blocks replacement of finalized_audit_event_id after finalization."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)
    _finalize_computation(db_connection, computation_id, user_id)

    with pytest.raises(
        psycopg.Error,
        match="computations.finalized_audit_event_id is immutable once finalized",
    ):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE computations
                SET finalized_audit_event_id = gen_random_uuid()
                WHERE id = %s
                """,
                (computation_id,),
            )


def test_finalization_is_idempotent_for_repeated_calls(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated finalization returns identical finalized state."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_income_tax_computation(db_connection, user_id)

    first = finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=_build_finalization_context(user_id),
        connection=db_connection,
    )
    second = finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=_build_finalization_context(user_id),
        connection=db_connection,
    )

    assert second.computation_id == first.computation_id
    assert second.finalized_at == first.finalized_at
    assert second.finalized_audit_event_id == first.finalized_audit_event_id


def _finalize_computation(
    connection: psycopg.Connection,
    computation_id: UUID,
    user_id: UUID,
) -> None:
    finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=_build_finalization_context(user_id),
        connection=connection,
    )


def _materialize_income_tax_computation(
    connection: psycopg.Connection,
    user_id: UUID,
) -> UUID:
    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1400, "deductions": {"housing": 10}},
    )
    result = execute_computation(request)
    materialized = materialize_execution_result(
        execution_request=request,
        execution_result=result,
        context=MaterializationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-execute-{uuid4()}",
            idempotency_key=f"idem-execute-{uuid4()}",
        ),
        connection=connection,
    )
    return materialized.computation_id


def _insert_validation_row(
    connection: psycopg.Connection,
    computation_id: UUID,
    user_id: UUID,
) -> UUID:
    validation_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO validations (
                id,
                computation_id,
                user_id,
                validation_context,
                findings
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (
                validation_id,
                computation_id,
                user_id,
                "finalization-test",
                '{"status":"ok"}',
            ),
        )
    return validation_id


def _insert_user(connection: psycopg.Connection) -> UUID:
    user_id = uuid4()
    suffix = uuid4().hex
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO users (
                id,
                phone_number_encrypted,
                email_encrypted,
                role
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                user_id,
                f"phone-{suffix}",
                f"user-{suffix}@example.com",
                "IndividualTaxpayer",
            ),
        )
    return user_id


def _build_finalization_context(user_id: UUID) -> ComputationFinalizationContext:
    return ComputationFinalizationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-finalize-{uuid4()}",
        idempotency_key=f"idem-finalize-{uuid4()}",
    )


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
