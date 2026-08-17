"""Verify replay coverage for supported governed income-tax lanes."""

from __future__ import annotations

import os
import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import TypedDict
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg

from services.tax_core.app.engine.replay import ReplayVerificationError
from services.tax_core.app.engine.replay import verify_persisted_computation_replay
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.persistence.materialization import load_persisted_replay_source
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"
GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
REQUIRED_REPLAY_FIXTURE_IDS = (
    "income_tax_resident_employment_2021_01_01_case_001",
    "income_tax_non_resident_employment_2021_01_01_case_001",
    "income_tax_resident_employment_2023_07_01_case_001",
    "income_tax_non_resident_employment_2023_07_01_case_001",
    "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
)


class GoldenFixture(TypedDict):
    fixture_version: int
    fixture_id: str
    request: dict[str, object]
    expected_output: dict[str, object]


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL historical replay tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("fixture_id", REQUIRED_REPLAY_FIXTURE_IDS)
def test_supported_income_tax_lane_replays_successfully(
    db_connection: psycopg.Connection,
    fixture_id: str,
) -> None:
    """Verify each supported governed lane replays under its original version window."""

    fixture = _load_fixture(fixture_id)
    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture=fixture,
    )

    replay_result = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=_build_replay_context(user_id),
        connection=db_connection,
    )

    assert replay_result.status == "ok"
    assert replay_result.verification_status == "matched"
    assert replay_result.computation_id == computation_id


@pytest.mark.parametrize(
    ("fixture_id", "expected_historical_version_id", "expected_effective_date"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001",
            "KIT-VER-20210101-A",
            "2021-01-01",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001",
            "KIT-VER-20230701-A",
            "2023-07-01",
        ),
    ],
)
def test_persisted_replay_source_preserves_original_historical_version_context(
    db_connection: psycopg.Connection,
    fixture_id: str,
    expected_historical_version_id: str,
    expected_effective_date: str,
) -> None:
    """Verify replay loads the original governed version context from persistence."""

    fixture = _load_fixture(fixture_id)
    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture=fixture,
    )

    with db_connection.cursor() as cursor:
        persisted_source = load_persisted_replay_source(
            cursor=cursor,
            computation_id=computation_id,
        )

    version_context = cast(
        dict[str, object],
        persisted_source.persisted_input_payload["version_context"],
    )

    assert version_context["historical_version_id"] == expected_historical_version_id
    assert version_context["primary_effective_date"] == expected_effective_date


def test_replay_verification_detects_tampered_mixed_income_result_deterministically(
    db_connection: psycopg.Connection,
) -> None:
    """Verify a tampered mixed-income stored output causes deterministic replay mismatch."""

    fixture = _load_fixture(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001"
    )
    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture=fixture,
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{liability_summary,net_income_tax_due_kes}',
                '"214600.01"'::jsonb
            )
            WHERE computation_id = %s
            """,
            (computation_id,),
        )

    with pytest.raises(ReplayVerificationError) as error_info:
        verify_persisted_computation_replay(
            replay_request=ReplayVerificationRequest(computation_id=computation_id),
            replay_context=_build_replay_context(user_id),
            connection=db_connection,
        )

    assert error_info.value.reason == "replay_result_mismatch"
    assert "replay_audit_event_id" in error_info.value.details()


def test_supported_income_tax_replay_is_deterministic_for_repeated_runs(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated replay over one supported governed lane is deterministic."""

    fixture = _load_fixture("income_tax_non_resident_employment_2021_01_01_case_001")
    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        connection=db_connection,
        user_id=user_id,
        fixture=fixture,
    )

    first = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=_build_replay_context(user_id),
        connection=db_connection,
    )
    second = verify_persisted_computation_replay(
        replay_request=ReplayVerificationRequest(computation_id=computation_id),
        replay_context=_build_replay_context(user_id),
        connection=db_connection,
    )

    assert first.verification_status == second.verification_status == "matched"
    assert first.computation_id == second.computation_id
    assert first.input_hash == second.input_hash


def _materialize_fixture_computation(
    connection: psycopg.Connection,
    user_id: UUID,
    fixture: GoldenFixture,
) -> UUID:
    request = ComputationExecutionRequest.model_validate(fixture["request"])
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


def _load_fixture(fixture_id: str) -> GoldenFixture:
    for path in sorted(GOLDEN_CASE_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fixture_file:
            fixture = cast(GoldenFixture, json.load(fixture_file))
        if fixture["fixture_id"] == fixture_id:
            return fixture
    raise AssertionError(f"Missing golden fixture: {fixture_id}")


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
