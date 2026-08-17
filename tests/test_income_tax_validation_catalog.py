"""Verify deterministic governed income-tax validation findings for supported lanes."""

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

from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.validation import validate_persisted_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationContext
from services.tax_core.app.engine.execution_contract import ComputationValidationRequest
from services.tax_core.app.persistence.materialization import materialize_execution_result

DATABASE_URL_ENV_VAR = "DATABASE_URL"
GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


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
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL income-tax validation tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("fixture_id", "expected_codes"),
    [
        (
            "income_tax_resident_employment_2023_07_01_case_001",
            [
                "computation_lineage_bound",
                "income_tax_supported_lane_detected",
                "income_tax_version_binding_consistent",
                "income_tax_relief_treatment_consistent",
                "income_tax_liability_summary_consistent",
            ],
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001",
            [
                "computation_lineage_bound",
                "income_tax_supported_lane_detected",
                "income_tax_version_binding_consistent",
                "income_tax_relief_treatment_consistent",
                "income_tax_liability_summary_consistent",
            ],
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
            [
                "computation_lineage_bound",
                "income_tax_supported_lane_detected",
                "income_tax_version_binding_consistent",
                "income_tax_relief_treatment_consistent",
                "income_tax_liability_summary_consistent",
                "income_tax_mixed_income_treatment_consistent",
            ],
        ),
        (
            "income_tax_resident_employment_2021_01_01_case_001",
            [
                "computation_lineage_bound",
                "income_tax_supported_lane_detected",
                "income_tax_version_binding_consistent",
                "income_tax_relief_treatment_consistent",
                "income_tax_liability_summary_consistent",
            ],
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001",
            [
                "computation_lineage_bound",
                "income_tax_supported_lane_detected",
                "income_tax_version_binding_consistent",
                "income_tax_relief_treatment_consistent",
                "income_tax_liability_summary_consistent",
            ],
        ),
    ],
)
def test_supported_income_tax_lanes_emit_governed_validation_findings(
    db_connection: psycopg.Connection,
    fixture_id: str,
    expected_codes: list[str],
) -> None:
    """Verify every supported governed lane returns deterministic catalog findings."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(db_connection, user_id, fixture_id)

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )

    finding_codes = [finding.code for finding in validation_result.findings]
    finding_severities = [finding.severity for finding in validation_result.findings]

    assert finding_codes == expected_codes
    assert finding_severities == ["info"] * len(expected_codes)


def test_validation_detects_resident_relief_inconsistency_deterministically(
    db_connection: psycopg.Connection,
) -> None:
    """Verify tampered resident relief treatment yields a deterministic error finding."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        db_connection,
        user_id,
        "income_tax_resident_employment_2023_07_01_case_001",
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{liability_summary,total_reliefs_kes}',
                '"0.00"'::jsonb
            )
            WHERE computation_id = %s
            """,
            (computation_id,),
        )

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )

    finding_codes = [finding.code for finding in validation_result.findings]

    assert "income_tax_relief_treatment_inconsistent" in finding_codes
    assert any(finding.severity == "error" for finding in validation_result.findings)


def test_validation_detects_mixed_income_final_tax_inconsistency_deterministically(
    db_connection: psycopg.Connection,
) -> None:
    """Verify tampered mixed-income final-tax treatment yields a deterministic error finding."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        db_connection,
        user_id,
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE computation_results
            SET result_payload = jsonb_set(
                result_payload,
                '{liability_summary,final_tax_excluded_income_kes}',
                '"0.00"'::jsonb
            )
            WHERE computation_id = %s
            """,
            (computation_id,),
        )

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )

    assert any(
        finding.code == "income_tax_mixed_income_treatment_inconsistent"
        and finding.severity == "error"
        for finding in validation_result.findings
    )


def test_validation_is_deterministic_for_supported_income_tax_lane(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated validation over same governed lane yields identical findings."""

    user_id = _insert_user(db_connection)
    computation_id = _materialize_fixture_computation(
        db_connection,
        user_id,
        "income_tax_non_resident_employment_2021_01_01_case_001",
    )

    first = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )
    second = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )

    assert [finding.model_dump(mode="json") for finding in first.findings] == [
        finding.model_dump(mode="json") for finding in second.findings
    ]


def _materialize_fixture_computation(
    connection: psycopg.Connection,
    user_id: UUID,
    fixture_id: str,
) -> UUID:
    fixture = _load_fixture(fixture_id)
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


def _build_validation_context(user_id: UUID) -> ComputationValidationContext:
    return ComputationValidationContext(
        user_id=user_id,
        role_at_time="IndividualTaxpayer",
        correlation_id=f"corr-validate-{uuid4()}",
        idempotency_key=f"idem-validate-{uuid4()}",
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
