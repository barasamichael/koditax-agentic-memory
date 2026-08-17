"""Verify deterministic tax-core execution materialization against real PostgreSQL."""

from __future__ import annotations

import os
import copy
import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg

from services.tax_core.app.persistence import materialization
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.persistence.materialization import MaterializationError
from services.tax_core.app.persistence.materialization import IdempotencyConflictError
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"
_GOLDEN_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "eval" / "golden" / "tax_core"


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL materialization tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_materialization_persists_computation_result_and_audit_atomically(
    db_connection: psycopg.Connection,
) -> None:
    """Verify one successful execution persists linked computation, result, and audit rows."""

    user_id = _insert_user(db_connection)
    request = _build_income_tax_request(payload={"income": 1000, "deductions": {"a": 1}})
    result = execute_computation(request)
    context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-{uuid4()}",
    )

    persisted = materialize_execution_result(
        execution_request=request,
        execution_result=result,
        context=context,
        connection=db_connection,
    )

    assert persisted.computation_id == persisted.computation_result_id
    assert persisted.input_hash == result.input_hash
    assert persisted.idempotency_key == context.idempotency_key
    assert persisted.correlation_id == context.correlation_id

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM computations
            WHERE id = %s
            """,
            (persisted.computation_id,),
        )
        computation_row = cursor.fetchone()

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM computation_results
            WHERE computation_id = %s
            """,
            (persisted.computation_result_id,),
        )
        result_row = cursor.fetchone()

        cursor.execute(
            """
            SELECT correlation_id, details->>'computation_id'
            FROM audit_events
            WHERE id = %s
            """,
            (persisted.audit_event_id,),
        )
        audit_row = cursor.fetchone()

    assert computation_row is not None
    assert cast(int, computation_row[0]) == 1
    assert result_row is not None
    assert cast(int, result_row[0]) == 1
    assert audit_row is not None
    assert cast(str, audit_row[0]) == context.correlation_id
    assert cast(str, audit_row[1]) == str(persisted.computation_id)


def test_materialization_reuses_existing_record_for_same_idempotency_key_and_hash(
    db_connection: psycopg.Connection,
) -> None:
    """Verify same key and same logical hash return the same persisted records."""

    user_id = _insert_user(db_connection)
    request = _build_income_tax_request(payload={"income": 900, "allowances": {"transport": 20}})
    result = execute_computation(request)
    context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-{uuid4()}",
    )

    first = materialize_execution_result(
        execution_request=request,
        execution_result=result,
        context=context,
        connection=db_connection,
    )
    second = materialize_execution_result(
        execution_request=request,
        execution_result=result,
        context=context,
        connection=db_connection,
    )

    assert second.computation_id == first.computation_id
    assert second.computation_result_id == first.computation_result_id
    assert second.audit_event_id == first.audit_event_id

    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM computations WHERE idempotency_key = %s",
            (context.idempotency_key,),
        )
        computations_count_row = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM computation_results
            WHERE computation_id = %s
            """,
            (first.computation_id,),
        )
        results_count_row = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*) FROM audit_events WHERE idempotency_key = %s",
            (context.idempotency_key,),
        )
        audit_count_row = cursor.fetchone()

    assert computations_count_row is not None
    assert cast(int, computations_count_row[0]) == 1
    assert results_count_row is not None
    assert cast(int, results_count_row[0]) == 1
    assert audit_count_row is not None
    assert cast(int, audit_count_row[0]) == 1


def test_materialization_rejects_idempotency_key_reuse_with_different_hash(
    db_connection: psycopg.Connection,
) -> None:
    """Verify same idempotency key with changed logical input fails hard."""

    user_id = _insert_user(db_connection)
    idempotency_key = f"idem-{uuid4()}"
    first_request = _build_income_tax_request(payload={"income": 1000})
    second_request = _build_income_tax_request(payload={"income": 1200})
    first_result = execute_computation(first_request)
    second_result = execute_computation(second_request)
    shared_context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=idempotency_key,
    )

    materialize_execution_result(
        execution_request=first_request,
        execution_result=first_result,
        context=shared_context,
        connection=db_connection,
    )

    with pytest.raises(IdempotencyConflictError) as error_info:
        materialize_execution_result(
            execution_request=second_request,
            execution_result=second_result,
            context=shared_context,
            connection=db_connection,
        )

    assert error_info.value.reason == "idempotency_key_input_hash_mismatch"


def test_materialization_rolls_back_without_partial_rows_on_forced_failure(
    db_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify failure after computation/result inserts leaves no partial persisted state."""

    user_id = _insert_user(db_connection)
    request = _build_income_tax_request(payload={"income": 1500})
    result = execute_computation(request)
    context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-{uuid4()}",
    )

    def fail_audit_insert(
        cursor: psycopg.Cursor[tuple[object, ...]],
        computation_id: UUID,
        execution_result: object,
        context: object,
        retention_days: int,
    ) -> UUID:
        _ = cursor
        _ = computation_id
        _ = execution_result
        _ = context
        _ = retention_days
        raise RuntimeError("forced_audit_insert_failure")

    monkeypatch.setattr(materialization, "_insert_audit_event_row", fail_audit_insert)

    with pytest.raises(MaterializationError) as error_info:
        materialize_execution_result(
            execution_request=request,
            execution_result=result,
            context=context,
            connection=db_connection,
        )

    assert error_info.value.reason == "unexpected_materialization_error"

    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM computations WHERE idempotency_key = %s",
            (context.idempotency_key,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(int, row[0]) == 0


def test_materialization_is_deterministic_for_logically_equivalent_inputs(
    db_connection: psycopg.Connection,
) -> None:
    """Verify equivalent inputs produce same hash and same idempotent materialization outcome."""

    user_id = _insert_user(db_connection)
    request_a = _build_income_tax_request(payload={"b": {"y": 2, "x": 1}, "a": [3, {"k": 9}]})
    request_b = _build_income_tax_request(payload={"a": [3, {"k": 9}], "b": {"x": 1, "y": 2}})
    result_a = execute_computation(request_a)
    result_b = execute_computation(request_b)
    context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-{uuid4()}",
    )

    assert result_a.input_hash == result_b.input_hash

    first = materialize_execution_result(
        execution_request=request_a,
        execution_result=result_a,
        context=context,
        connection=db_connection,
    )
    second = materialize_execution_result(
        execution_request=request_b,
        execution_result=result_b,
        context=context,
        connection=db_connection,
    )

    assert second.computation_id == first.computation_id
    assert second.audit_event_id == first.audit_event_id
    assert second.input_hash == first.input_hash


def test_materialization_persists_health_contribution_regime_type_without_alias(
    db_connection: psycopg.Connection,
) -> None:
    """Verify supported health executions persist health_contribution directly in computations."""

    user_id = _insert_user(db_connection)
    fixture_payload = _load_golden_fixture_payload("health_contribution_sha_shif_case_001.json")
    request_payload = cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))
    request = ComputationExecutionRequest(
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_payload=cast(dict[str, object], request_payload["input_payload"]),
    )
    result = execute_computation(request)
    context = MaterializationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-{uuid4()}",
    )

    persisted = materialize_execution_result(
        execution_request=request,
        execution_result=result,
        context=context,
        connection=db_connection,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tax_type, regime_type, regime_identifier
            FROM computations
            WHERE id = %s
            """,
            (persisted.computation_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(str, row[0]) == "health_contribution"
    assert cast(str, row[1]) == "health_contribution"
    assert cast(str, row[2]) == "sha_shif"


def _build_income_tax_request(payload: dict[str, object]) -> ComputationExecutionRequest:
    return ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload=payload,
    )


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


def _load_golden_fixture_payload(fixture_name: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((_GOLDEN_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")),
    )
