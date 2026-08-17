"""Verify deterministic replay recomputation and persisted-result comparison."""

from __future__ import annotations

import os
import copy
import json
from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from services.tax_core.app.engine.replay import ReplayVerificationError
from services.tax_core.app.engine.replay import verify_persisted_computation_replay
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import PersistedReplaySource
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.persistence.materialization import load_persisted_replay_source
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"
_GOLDEN_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "eval" / "golden" / "tax_core"
_SUPPORTED_HEALTH_REPLAY_FIXTURE_NAMES = (
    "health_contribution_nhif_legacy_2010_case_001.json",
    "health_contribution_nhif_legacy_2015_case_001.json",
    "health_contribution_nhif_legacy_2021_case_001.json",
    "health_contribution_nhif_legacy_case_001.json",
    "health_contribution_sha_shif_case_001.json",
    "health_contribution_sha_shif_2024_non_salaried_case_001.json",
    "health_contribution_sha_shif_2025_salaried_case_001.json",
    "health_contribution_transition_boundary_nhif_case_001.json",
    "health_contribution_transition_boundary_sha_case_001.json",
)


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL replay tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_replay_verification_succeeds_for_unchanged_persisted_computation(
    db_connection: psycopg.Connection,
) -> None:
    """Verify replay succeeds when persisted output still matches deterministic recomputation."""

    user_id = _insert_user(db_connection)
    persisted_computation_id = _materialize_income_tax_computation(db_connection, user_id)
    replay_context = ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
    )

    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=replay_context,
        connection=db_connection,
    )

    assert replay_result.status == "ok"
    assert replay_result.verification_status == "matched"
    assert replay_result.computation_id == persisted_computation_id

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, details->>'verification_outcome'
            FROM audit_events
            WHERE id = %s
            """,
            (replay_result.replay_audit_event_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(str, row[0]) == "computation.replay_verified"
    assert cast(str, row[1]) == "matched"


def test_replay_endpoint_rejects_missing_computation_id_deterministically() -> None:
    """Verify replay boundary rejects malformed request shape with stable error code."""

    client = TestClient(create_app())
    response = client.post(
        "/computations/replay",
        json={},
        headers={
            "Authorization": f"Bearer {uuid4()}:IndividualTaxpayer",
            "Idempotency-Key": f"idem-{uuid4()}",
            "X-Correlation-ID": f"corr-{uuid4()}",
        },
    )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])

    assert response.status_code == 400
    assert cast(str, detail["error_code"]) == "invalid_replay_request"


def test_replay_verification_fails_on_result_mismatch_and_audits_mismatch(
    db_connection: psycopg.Connection,
) -> None:
    """Verify mismatch is a hard failure and emits replay mismatch audit evidence."""

    user_id = _insert_user(db_connection)
    persisted_computation_id = _materialize_income_tax_computation(db_connection, user_id)
    replay_context = ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
    )

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
            (persisted_computation_id,),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
            replay_context=replay_context,
            connection=db_connection,
        )

    assert error_info.value.reason == "replay_result_mismatch"
    details = error_info.value.details()
    replay_audit_event_id = UUID(cast(str, details["replay_audit_event_id"]))

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, details->>'mismatch_reason'
            FROM audit_events
            WHERE id = %s
            """,
            (replay_audit_event_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(str, row[0]) == "computation.replay_mismatch"
    assert cast(str, row[1]) == "replay_result_mismatch"


def test_replay_verification_is_deterministic_for_repeated_replay(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated replay over same computation yields same verification outcome."""

    user_id = _insert_user(db_connection)
    persisted_computation_id = _materialize_income_tax_computation(db_connection, user_id)

    replay_one = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=ReplayVerificationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-{uuid4()}",
            idempotency_key=f"idem-replay-{uuid4()}",
        ),
        connection=db_connection,
    )
    replay_two = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=ReplayVerificationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-{uuid4()}",
            idempotency_key=f"idem-replay-{uuid4()}",
        ),
        connection=db_connection,
    )

    assert replay_one.verification_status == "matched"
    assert replay_two.verification_status == "matched"
    assert replay_one.computation_id == replay_two.computation_id
    assert replay_one.input_hash == replay_two.input_hash


@pytest.mark.parametrize("fixture_name", _SUPPORTED_HEALTH_REPLAY_FIXTURE_NAMES)
def test_supported_health_replay_verification_matches_persisted_history(
    db_connection: psycopg.Connection,
    fixture_name: str,
) -> None:
    """Verify every governed supported health fixture replays to the exact persisted output."""

    user_id = _insert_user(db_connection)
    (
        request_payload,
        expected_output,
        persisted_computation_id,
        persisted_source,
    ) = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name=fixture_name,
    )
    expected_result_payload = cast(dict[str, object], expected_output["result_payload"])
    expected_version_identity = cast(
        dict[str, object],
        expected_result_payload["version_identity"],
    )
    replay_context = ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
    )

    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=replay_context,
        connection=db_connection,
    )

    persisted_version_context = cast(
        dict[str, object],
        persisted_source.persisted_input_payload["version_context"],
    )
    stored_version_identity = cast(
        dict[str, object],
        persisted_source.stored_result_payload["version_identity"],
    )

    assert replay_result.status == "ok"
    assert replay_result.verification_status == "matched"
    assert replay_result.computation_id == persisted_computation_id
    assert replay_result.input_hash == expected_output["input_hash"]
    assert replay_result.tax_type == "health_contribution"
    assert replay_result.regime_type == "health_contribution"
    assert persisted_source.regime_identifier == request_payload["regime_identifier"]
    if "historical_version_id" in persisted_version_context:
        assert (
            persisted_version_context["historical_version_id"]
            == expected_version_identity["historical_version_id"]
        )
    else:
        assert persisted_source.regime_identifier == "transition_boundary"
    assert (
        stored_version_identity["historical_version_id"]
        == expected_version_identity["historical_version_id"]
    )
    assert (
        stored_version_identity["regime_identifier"]
        == expected_version_identity["regime_identifier"]
    )


def test_health_replay_verification_fails_on_result_mismatch_and_audits_mismatch(
    db_connection: psycopg.Connection,
) -> None:
    """Verify health replay mismatch stays deterministic and audit-evidenced."""

    user_id = _insert_user(db_connection)
    _, _, persisted_computation_id, _ = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name="health_contribution_sha_shif_2025_salaried_case_001.json",
    )
    replay_context = ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
    )

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
            (persisted_computation_id,),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
            replay_context=replay_context,
            connection=db_connection,
        )

    assert error_info.value.reason == "replay_result_mismatch"
    details = error_info.value.details()
    replay_audit_event_id = UUID(cast(str, details["replay_audit_event_id"]))

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, details->>'mismatch_reason'
            FROM audit_events
            WHERE id = %s
            """,
            (replay_audit_event_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(str, row[0]) == "computation.replay_mismatch"
    assert cast(str, row[1]) == "replay_result_mismatch"


def test_health_replay_verification_is_deterministic_for_repeated_replay(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated health replay keeps the same outcome, computation ID, and input hash."""

    user_id = _insert_user(db_connection)
    _, expected_output, persisted_computation_id, _ = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name="health_contribution_transition_boundary_sha_case_001.json",
    )

    replay_one = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=ReplayVerificationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-{uuid4()}",
            idempotency_key=f"idem-replay-{uuid4()}",
        ),
        connection=db_connection,
    )
    replay_two = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
        replay_context=ReplayVerificationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-{uuid4()}",
            idempotency_key=f"idem-replay-{uuid4()}",
        ),
        connection=db_connection,
    )

    assert replay_one.verification_status == "matched"
    assert replay_two.verification_status == "matched"
    assert replay_one.computation_id == replay_two.computation_id
    assert replay_one.input_hash == replay_two.input_hash == expected_output["input_hash"]


def test_health_replay_rejects_non_ready_historical_window_claim_canonically(
    db_connection: psycopg.Connection,
) -> None:
    """Verify replay stays fail-closed when persisted health input is tampered to 2003 history."""

    user_id = _insert_user(db_connection)
    _, _, persisted_computation_id, _ = _materialize_health_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture_name="health_contribution_nhif_legacy_2010_case_001.json",
    )
    rejection_request = _load_golden_execution_request(
        "health_contribution_historical_rejection_2003_case_001.json"
    )
    rejection_input_payload = cast(dict[str, object], rejection_request["input_payload"])
    replay_context = ReplayVerificationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-{uuid4()}",
        idempotency_key=f"idem-replay-{uuid4()}",
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{_kodi_replay_context,normalized_input}',
                %s::jsonb
            )
            WHERE computation_id = %s
            """,
            (
                json.dumps(rejection_input_payload, sort_keys=True, separators=(",", ":")),
                persisted_computation_id,
            ),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=persisted_computation_id),
            replay_context=replay_context,
            connection=db_connection,
        )

    error_details = error_info.value.details()
    selection_key = cast(dict[str, object], error_details["selection_key"])

    assert error_info.value.reason == "unsupported_partially_specified_window"
    assert cast(str, error_details["computation_id"]) == str(persisted_computation_id)
    assert cast(str, selection_key["historical_version_id"]) == "HCH-VER-20031205-A"
    assert cast(str, selection_key["regime_identifier"]) == "nhif_legacy"


def _materialize_income_tax_computation(
    connection: psycopg.Connection,
    user_id: UUID,
) -> UUID:
    execution_request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1000, "deductions": {"a": 1, "b": 2}},
    )
    execution_result = execute_computation(execution_request)
    persisted_result = materialize_execution_result(
        execution_request=execution_request,
        execution_result=execution_result,
        context=MaterializationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-execute-{uuid4()}",
            idempotency_key=f"idem-execute-{uuid4()}",
        ),
        connection=connection,
    )
    return persisted_result.computation_id


def _materialize_health_fixture_computation(
    connection: psycopg.Connection,
    user_id: UUID,
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object], UUID, PersistedReplaySource]:
    request_payload = _load_golden_execution_request(fixture_name)
    expected_output = cast(
        dict[str, object],
        _load_golden_fixture_payload(fixture_name)["expected_output"],
    )
    execution_request = _build_execution_request(request_payload)
    execution_result = execute_computation(execution_request)
    persisted_result = materialize_execution_result(
        execution_request=execution_request,
        execution_result=execution_result,
        context=MaterializationContext(
            user_id=user_id,
            role_at_time="IndividualTaxpayer",
            correlation_id=f"corr-execute-{uuid4()}",
            idempotency_key=f"idem-execute-{uuid4()}",
        ),
        connection=connection,
    )

    assert execution_result.input_hash == expected_output["input_hash"]
    assert execution_result.result_payload == expected_output["result_payload"]

    with connection.cursor() as cursor:
        persisted_source = load_persisted_replay_source(
            cursor=cursor,
            computation_id=persisted_result.computation_id,
        )

    return request_payload, expected_output, persisted_result.computation_id, persisted_source


def _build_execution_request(request_payload: dict[str, object]) -> ComputationExecutionRequest:
    return ComputationExecutionRequest(
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_payload=cast(
            dict[str, object],
            copy.deepcopy(request_payload["input_payload"]),
        ),
    )


def _load_golden_execution_request(fixture_name: str) -> dict[str, object]:
    fixture_payload = _load_golden_fixture_payload(fixture_name)
    return cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))


def _load_golden_fixture_payload(fixture_name: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((_GOLDEN_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")),
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


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
