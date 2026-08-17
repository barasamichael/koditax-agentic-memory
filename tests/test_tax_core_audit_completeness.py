"""Verify complete structured audit coverage for the computation lifecycle."""

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

from services.tax_core.app.engine.replay import ReplayVerificationError
from services.tax_core.app.engine.replay import verify_persisted_computation_replay
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.validation import DEFAULT_VALIDATION_CONTEXT
from services.tax_core.app.engine.validation import validate_persisted_computation
from services.tax_core.app.engine.finalization import finalize_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationContext
from services.tax_core.app.engine.execution_contract import ComputationValidationRequest
from services.tax_core.app.engine.execution_contract import ComputationFinalizationContext
from services.tax_core.app.engine.execution_contract import ComputationFinalizationRequest
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"
_GOLDEN_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "eval" / "golden" / "tax_core"


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL audit tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_execution_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify computation execution persists a normalized execution audit payload."""

    user_id = _insert_user(db_connection)
    (
        computation_id,
        audit_event_id,
        correlation_id,
        idempotency_key,
    ) = _materialize_income_tax_computation(db_connection, user_id)
    audit_details = _load_audit_details_by_id(db_connection, audit_event_id)

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="execution",
        outcome="succeeded",
        computation_id=computation_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )
    assert audit_details["tax_type"] == "income_tax"
    assert audit_details["regime_type"] == "income_tax"
    assert isinstance(audit_details["result_sha256"], str)
    assert len(audit_details["result_sha256"]) == 64


def test_validation_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify validation persists a normalized validation audit payload."""

    user_id = _insert_user(db_connection)
    computation_id, _, _, _ = _materialize_income_tax_computation(db_connection, user_id)
    validation_context = _build_validation_context(user_id)
    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=validation_context,
        connection=db_connection,
    )
    audit_details = _load_audit_details_by_event_type_and_idempotency(
        db_connection=db_connection,
        event_type="computation.validated",
        idempotency_key=validation_context.idempotency_key,
    )

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="validation",
        outcome="succeeded",
        computation_id=validation_result.computation_id,
        correlation_id=validation_context.correlation_id,
        idempotency_key=validation_context.idempotency_key,
    )
    assert audit_details["validation_id"] == str(validation_result.validation_id)
    assert audit_details["validation_context"] == DEFAULT_VALIDATION_CONTEXT
    assert audit_details["finding_count"] == 2
    assert audit_details["finding_severities"] == ["error", "info"]


def test_finalization_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify finalization persists a normalized finalization audit payload."""

    user_id = _insert_user(db_connection)
    computation_id, _, _, _ = _materialize_income_tax_computation(db_connection, user_id)
    finalization_context = _build_finalization_context(user_id)
    finalization_result = finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=finalization_context,
        connection=db_connection,
    )
    audit_details = _load_audit_details_by_id(
        db_connection,
        finalization_result.finalized_audit_event_id,
    )

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="finalization",
        outcome="finalized",
        computation_id=finalization_result.computation_id,
        correlation_id=finalization_context.correlation_id,
        idempotency_key=finalization_context.idempotency_key,
    )
    assert audit_details["finalization_status"] == "finalized"


def test_replay_success_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify replay success persists a normalized replay audit payload."""

    user_id = _insert_user(db_connection)
    computation_id, _, _, _ = _materialize_income_tax_computation(db_connection, user_id)
    replay_context = _build_replay_context(user_id)
    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=replay_context,
        connection=db_connection,
    )
    audit_details = _load_audit_details_by_id(
        db_connection,
        replay_result.replay_audit_event_id,
    )

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="replay",
        outcome="matched",
        computation_id=replay_result.computation_id,
        correlation_id=replay_context.correlation_id,
        idempotency_key=replay_context.idempotency_key,
    )
    assert audit_details["verification_outcome"] == "matched"
    assert audit_details["stored_result_sha256"] == audit_details["replay_result_sha256"]


def test_replay_mismatch_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify replay mismatch persists a normalized mismatch audit payload."""

    user_id = _insert_user(db_connection)
    computation_id, _, _, _ = _materialize_income_tax_computation(db_connection, user_id)
    replay_context = _build_replay_context(user_id)

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{execution_mode}',
                '"tampered"'::jsonb
            )
            WHERE computation_id = %s
            """,
            (computation_id,),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=computation_id),
            replay_context=replay_context,
            connection=db_connection,
        )

    replay_audit_event_id = UUID(cast(str, error_info.value.details()["replay_audit_event_id"]))
    audit_details = _load_audit_details_by_id(db_connection, replay_audit_event_id)

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="replay",
        outcome="mismatch",
        computation_id=computation_id,
        correlation_id=replay_context.correlation_id,
        idempotency_key=replay_context.idempotency_key,
    )
    assert audit_details["verification_outcome"] == "mismatch"
    assert audit_details["mismatch_reason"] == "replay_result_mismatch"
    assert audit_details["stored_result_sha256"] != audit_details["replay_result_sha256"]


def test_health_replay_success_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify supported health replay success persists canonical audit evidence."""

    user_id = _insert_user(db_connection)
    request_payload, _, computation_id, _, _ = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name="health_contribution_transition_boundary_sha_case_001.json",
    )
    replay_context = _build_replay_context(user_id)
    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=replay_context,
        connection=db_connection,
    )
    audit_details = _load_audit_details_by_id(
        db_connection,
        replay_result.replay_audit_event_id,
    )

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="replay",
        outcome="matched",
        computation_id=replay_result.computation_id,
        correlation_id=replay_context.correlation_id,
        idempotency_key=replay_context.idempotency_key,
        tax_year=cast(int, request_payload["tax_year"]),
    )
    assert audit_details["tax_type"] == "health_contribution"
    assert audit_details["regime_type"] == "health_contribution"
    assert audit_details["verification_outcome"] == "matched"
    assert audit_details["stored_result_sha256"] == audit_details["replay_result_sha256"]


def test_health_replay_mismatch_emits_complete_lifecycle_audit_evidence(
    db_connection: psycopg.Connection,
) -> None:
    """Verify health replay mismatch persists canonical mismatch audit evidence."""

    user_id = _insert_user(db_connection)
    request_payload, _, computation_id, _, _ = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name="health_contribution_sha_shif_2025_salaried_case_001.json",
    )
    replay_context = _build_replay_context(user_id)

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{traceability,input_hash}',
                '"tampered"'::jsonb
            )
            WHERE computation_id = %s
            """,
            (computation_id,),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=computation_id),
            replay_context=replay_context,
            connection=db_connection,
        )

    replay_audit_event_id = UUID(cast(str, error_info.value.details()["replay_audit_event_id"]))
    audit_details = _load_audit_details_by_id(db_connection, replay_audit_event_id)

    _assert_common_audit_fields(
        audit_details=audit_details,
        lifecycle_stage="replay",
        outcome="mismatch",
        computation_id=computation_id,
        correlation_id=replay_context.correlation_id,
        idempotency_key=replay_context.idempotency_key,
        tax_year=cast(int, request_payload["tax_year"]),
    )
    assert audit_details["tax_type"] == "health_contribution"
    assert audit_details["regime_type"] == "health_contribution"
    assert audit_details["verification_outcome"] == "mismatch"
    assert audit_details["mismatch_reason"] == "replay_result_mismatch"
    assert audit_details["stored_result_sha256"] != audit_details["replay_result_sha256"]


def test_lifecycle_audit_payload_shape_is_consistent_across_stages(
    db_connection: psycopg.Connection,
) -> None:
    """Verify common audit payload keys are present across all lifecycle stages."""

    user_id = _insert_user(db_connection)
    computation_id, execution_audit_event_id, _, _ = _materialize_income_tax_computation(
        db_connection, user_id
    )
    validation_context = _build_validation_context(user_id)
    validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=validation_context,
        connection=db_connection,
    )
    finalization_context = _build_finalization_context(user_id)
    finalization_result = finalize_computation(
        finalization_request=ComputationFinalizationRequest(computation_id=computation_id),
        finalization_context=finalization_context,
        connection=db_connection,
    )
    replay_context = _build_replay_context(user_id)
    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=replay_context,
        connection=db_connection,
    )

    validation_audit = _load_audit_details_by_event_type_and_idempotency(
        db_connection=db_connection,
        event_type="computation.validated",
        idempotency_key=validation_context.idempotency_key,
    )
    finalization_audit = _load_audit_details_by_id(
        db_connection,
        finalization_result.finalized_audit_event_id,
    )
    replay_audit = _load_audit_details_by_id(
        db_connection,
        replay_result.replay_audit_event_id,
    )
    execution_audit = _load_audit_details_by_id(db_connection, execution_audit_event_id)

    common_keys = {
        "lifecycle_stage",
        "computation_id",
        "correlation_id",
        "idempotency_key",
        "tax_year",
        "rule_version",
        "input_hash",
        "outcome",
    }
    for audit_details in (
        execution_audit,
        validation_audit,
        finalization_audit,
        replay_audit,
    ):
        assert common_keys.issubset(set(audit_details))


def _materialize_income_tax_computation(
    connection: psycopg.Connection,
    user_id: UUID,
) -> tuple[UUID, UUID, str, str]:
    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1700, "deductions": {"housing": 30}},
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
    return (
        materialized.computation_id,
        materialized.audit_event_id,
        materialized.correlation_id,
        materialized.idempotency_key,
    )


def _materialize_health_fixture_computation(
    connection: psycopg.Connection,
    user_id: UUID,
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object], UUID, UUID, str]:
    fixture_payload = _load_golden_fixture_payload(fixture_name)
    request_payload = cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))
    expected_output = cast(dict[str, object], fixture_payload["expected_output"])
    request = ComputationExecutionRequest(
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_payload=cast(dict[str, object], request_payload["input_payload"]),
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

    assert result.input_hash == expected_output["input_hash"]
    assert result.result_payload == expected_output["result_payload"]

    return (
        request_payload,
        expected_output,
        materialized.computation_id,
        materialized.audit_event_id,
        materialized.idempotency_key,
    )


def _load_audit_details_by_id(
    connection: psycopg.Connection,
    audit_event_id: UUID,
) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT details
            FROM audit_events
            WHERE id = %s
            """,
            (audit_event_id,),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(dict[str, object], row[0])


def _load_audit_details_by_event_type_and_idempotency(
    db_connection: psycopg.Connection,
    event_type: str,
    idempotency_key: str,
) -> dict[str, object]:
    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT details
            FROM audit_events
            WHERE event_type = %s
              AND idempotency_key = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (event_type, idempotency_key),
        )
        row = cursor.fetchone()
    assert row is not None
    return cast(dict[str, object], row[0])


def _assert_common_audit_fields(
    audit_details: dict[str, object],
    lifecycle_stage: str,
    outcome: str,
    computation_id: UUID,
    correlation_id: str,
    idempotency_key: str,
    tax_year: int = 2025,
    rule_version: str = "v1",
) -> None:
    assert audit_details["lifecycle_stage"] == lifecycle_stage
    assert audit_details["computation_id"] == str(computation_id)
    assert audit_details["correlation_id"] == correlation_id
    assert audit_details["idempotency_key"] == idempotency_key
    assert audit_details["tax_year"] == tax_year
    assert audit_details["rule_version"] == rule_version
    assert isinstance(audit_details["input_hash"], str)
    assert audit_details["outcome"] == outcome


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


def _build_validation_context(user_id: UUID) -> ComputationValidationContext:
    return ComputationValidationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-validate-{uuid4()}",
        idempotency_key=f"idem-validate-{uuid4()}",
    )


def _build_finalization_context(user_id: UUID) -> ComputationFinalizationContext:
    return ComputationFinalizationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-finalize-{uuid4()}",
        idempotency_key=f"idem-finalize-{uuid4()}",
    )


def _build_replay_context(user_id: UUID) -> ReplayVerificationContext:
    return ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-replay-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
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


def _load_golden_fixture_payload(fixture_name: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((_GOLDEN_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")),
    )
