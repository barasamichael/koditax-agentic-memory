"""Verify deterministic computation-bound validation persistence and output shape."""

from __future__ import annotations

import os
import copy
import json
from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from typing import TypedDict
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from services.tax_core.app.engine.executor import execute_computation
import services.tax_core.app.engine.validation as validation_module
from services.tax_core.app.engine.validation import ValidationError
from services.tax_core.app.engine.validation import DEFAULT_VALIDATION_CONTEXT
from services.tax_core.app.engine.validation import validate_persisted_computation
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import PersistedValidationSource
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationResult
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


class _FakeCursor:
    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeConnection:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping PostgreSQL validation hook tests.")

    connection = psycopg.connect(database_url)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_validation_persists_structured_findings_for_persisted_computation(
    db_connection: psycopg.Connection,
) -> None:
    """Verify validation findings persist with explicit unsupported-scope signaling."""

    user_id = _insert_user(db_connection)
    computation_id, input_hash = _materialize_income_tax_computation(db_connection, user_id)

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(computation_id=computation_id),
        validation_context=_build_validation_context(user_id),
        connection=db_connection,
    )

    assert validation_result.status == "ok"
    assert validation_result.computation_id == computation_id
    assert validation_result.validation_context == DEFAULT_VALIDATION_CONTEXT
    assert validation_result.tax_year == 2025
    assert validation_result.rule_version == "v1"
    assert len(validation_result.findings) == 2
    assert validation_result.findings[0].severity == "info"
    assert validation_result.findings[1].severity == "error"

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT computation_id, validation_context, findings
            FROM validations
            WHERE id = %s
            """,
            (validation_result.validation_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert cast(UUID, row[0]) == computation_id
    assert cast(str, row[1]) == DEFAULT_VALIDATION_CONTEXT
    assert cast(dict[str, object], row[2]) == {
        "tax_year": 2025,
        "rule_version": "v1",
        "input_hash": input_hash,
        "findings": [
            {
                "code": "computation_lineage_bound",
                "severity": "info",
                "message": (
                    "Validation findings are bound to persisted deterministic computation lineage."
                ),
                "details": {
                    "tax_type": "income_tax",
                    "regime_type": "income_tax",
                    "tax_year": 2025,
                    "rule_version": "v1",
                    "input_hash": input_hash,
                },
            },
            {
                "code": "income_tax_validation_scope_unsupported",
                "severity": "error",
                "message": (
                    "Income-tax validation catalog does not support "
                    "this persisted computation lane."
                ),
                "details": {
                    "domain_id": "ITD-GOV-SCOPE",
                    "tax_type": "income_tax",
                    "regime_type": "income_tax",
                    "tax_year": 2025,
                    "rule_version": "v1",
                    "input_hash": input_hash,
                    "reason": "non_governed_income_tax_result",
                    "historical_version_id": None,
                    "resident_status": None,
                    "income_category_signature": None,
                },
            },
        ],
    }


def test_validation_persists_structured_health_findings_for_supported_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify supported health computations persist governed validation findings."""

    persisted_source = _build_health_persisted_source(
        fixture_id="health_contribution_transition_boundary_sha_case_001",
        user_id=uuid4(),
    )
    persisted_findings: dict[str, object] = {}
    validation_id = uuid4()

    monkeypatch.setattr(
        validation_module,
        "load_persisted_validation_source",
        lambda cursor, computation_id: (
            persisted_source if computation_id == persisted_source.computation_id else None
        ),
    )
    monkeypatch.setattr(
        validation_module,
        "insert_validation_row",
        lambda cursor, persisted_source, context, validation_context, findings: (
            persisted_findings.update(
                {
                    "tax_year": persisted_source.tax_year,
                    "rule_version": persisted_source.rule_version,
                    "input_hash": persisted_source.input_hash,
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                }
            )
            or validation_id
        ),
    )
    monkeypatch.setattr(
        validation_module,
        "build_validation_result",
        lambda validation_id, persisted_source, context, validation_context, findings: (
            ComputationValidationResult(
                status="ok",
                validation_id=validation_id,
                computation_id=persisted_source.computation_id,
                validation_context=validation_context,
                correlation_id=context.correlation_id,
                idempotency_key=context.idempotency_key,
                tax_year=persisted_source.tax_year,
                rule_version=persisted_source.rule_version,
                findings=findings,
            )
        ),
    )

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(
            computation_id=persisted_source.computation_id
        ),
        validation_context=_build_validation_context(persisted_source.user_id),
        connection=_FakeConnection(),
    )

    assert validation_result.status == "ok"
    assert validation_result.computation_id == persisted_source.computation_id
    assert validation_result.validation_context == DEFAULT_VALIDATION_CONTEXT
    assert [finding.code for finding in validation_result.findings] == [
        "computation_lineage_bound",
        "health_contribution_supported_lane_detected",
        "health_contribution_version_binding_consistent",
        "health_contribution_effective_window_consistent",
        "health_contribution_summary_consistent",
    ]
    findings = cast(list[dict[str, object]], persisted_findings["findings"])
    assert persisted_findings["tax_year"] == 2024
    assert persisted_findings["rule_version"] == "v1"
    assert persisted_findings["input_hash"] == persisted_source.input_hash
    assert [finding["code"] for finding in findings] == [
        "computation_lineage_bound",
        "health_contribution_supported_lane_detected",
        "health_contribution_version_binding_consistent",
        "health_contribution_effective_window_consistent",
        "health_contribution_summary_consistent",
    ]
    assert findings[1]["details"]["request_regime_identifier"] == "transition_boundary"
    assert findings[1]["details"]["resolved_regime_identifier"] == "sha_shif"
    assert findings[3]["details"]["effective_start"] == "2024-10-01"


def test_validation_persists_canonical_health_unsupported_scope_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify malformed health payloads persist one canonical unsupported-scope finding."""

    persisted_source = _build_health_persisted_source(
        fixture_id="health_contribution_nhif_legacy_case_001",
        user_id=uuid4(),
    )
    persisted_source.stored_result_payload.pop("version_identity")
    _install_fake_health_validation_flow(monkeypatch, persisted_source)

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(
            computation_id=persisted_source.computation_id
        ),
        validation_context=_build_validation_context(persisted_source.user_id),
        connection=_FakeConnection(),
    )

    assert [finding.code for finding in validation_result.findings] == [
        "computation_lineage_bound",
        "health_contribution_validation_scope_unsupported",
    ]
    assert validation_result.findings[1].details["reason"] == "malformed_health_contribution_result"


def test_validation_persists_health_error_finding_for_non_ready_window_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify non-ready health version claims persist canonical error findings."""

    persisted_source = _build_health_persisted_source(
        fixture_id="health_contribution_nhif_legacy_case_001",
        user_id=uuid4(),
    )
    persisted_source.stored_result_payload["version_identity"][
        "historical_version_id"
    ] = "HCH-VER-20031205-A"
    _install_fake_health_validation_flow(monkeypatch, persisted_source)

    validation_result = validate_persisted_computation(
        validation_request=ComputationValidationRequest(
            computation_id=persisted_source.computation_id
        ),
        validation_context=_build_validation_context(persisted_source.user_id),
        connection=_FakeConnection(),
    )

    assert [finding.code for finding in validation_result.findings] == [
        "computation_lineage_bound",
        "health_contribution_version_window_unsupported",
    ]
    assert validation_result.findings[1].severity == "error"
    assert validation_result.findings[1].details["governed_window_status"] == "partially_specified"


def test_validation_endpoint_rejects_missing_computation_id_deterministically() -> None:
    """Verify validation boundary rejects malformed request shape with stable error code."""

    client = TestClient(create_app())
    response = client.post(
        "/computations/validate",
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
    assert cast(str, detail["error_code"]) == "invalid_validation_request"


def test_validation_fails_deterministically_for_missing_persisted_computation(
    db_connection: psycopg.Connection,
) -> None:
    """Verify validation requires an existing persisted computation identifier."""

    user_id = _insert_user(db_connection)

    with pytest.raises(ValidationError) as error_info:
        validate_persisted_computation(
            validation_request=ComputationValidationRequest(computation_id=uuid4()),
            validation_context=_build_validation_context(user_id),
            connection=db_connection,
        )

    assert error_info.value.reason == "computation_not_found"
    assert error_info.value.status_code == 404


def test_validation_cannot_exist_without_computation_lineage(
    db_connection: psycopg.Connection,
) -> None:
    """Verify free-floating validation insertion is blocked by computation lineage enforcement."""

    user_id = _insert_user(db_connection)

    with pytest.raises(psycopg.Error, match="validations lineage requires existing computation"):
        with db_connection.cursor() as cursor:
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
                    uuid4(),
                    user_id,
                    DEFAULT_VALIDATION_CONTEXT,
                    '{"findings":[],"tax_year":2025,"rule_version":"v1","input_hash":"x"}',
                ),
            )


def test_validation_is_deterministic_for_repeated_runs(
    db_connection: psycopg.Connection,
) -> None:
    """Verify repeated validation over same computation yields identical findings shape."""

    user_id = _insert_user(db_connection)
    computation_id, _ = _materialize_income_tax_computation(db_connection, user_id)

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

    assert first.computation_id == second.computation_id
    assert first.validation_context == second.validation_context
    assert [finding.model_dump(mode="json") for finding in first.findings] == [
        finding.model_dump(mode="json") for finding in second.findings
    ]


def _materialize_income_tax_computation(
    connection: psycopg.Connection,
    user_id: UUID,
) -> tuple[UUID, str]:
    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1600, "deductions": {"housing": 20}},
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
    return materialized.computation_id, materialized.input_hash


def _build_health_persisted_source(
    fixture_id: str,
    user_id: UUID,
) -> PersistedValidationSource:
    fixture = _load_fixture(fixture_id)
    request = copy.deepcopy(fixture["request"])
    expected_output = cast(dict[str, object], fixture["expected_output"])
    result_payload = cast(dict[str, object], copy.deepcopy(expected_output["result_payload"]))
    result_payload["_kodi_replay_context"] = {
        "normalized_input": cast(dict[str, object], request)["input_payload"]
    }

    request_payload = cast(dict[str, object], request)
    return PersistedValidationSource(
        computation_id=uuid4(),
        user_id=user_id,
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_hash=cast(str, expected_output["input_hash"]),
        stored_result_payload=result_payload,
    )


def _install_fake_health_validation_flow(
    monkeypatch: pytest.MonkeyPatch,
    persisted_source: PersistedValidationSource,
) -> None:
    monkeypatch.setattr(
        validation_module,
        "load_persisted_validation_source",
        lambda cursor, computation_id: (
            persisted_source if computation_id == persisted_source.computation_id else None
        ),
    )
    monkeypatch.setattr(
        validation_module,
        "insert_validation_row",
        lambda cursor, persisted_source, context, validation_context, findings: uuid4(),
    )
    monkeypatch.setattr(
        validation_module,
        "build_validation_result",
        lambda validation_id, persisted_source, context, validation_context, findings: (
            ComputationValidationResult(
                status="ok",
                validation_id=validation_id,
                computation_id=persisted_source.computation_id,
                validation_context=validation_context,
                correlation_id=context.correlation_id,
                idempotency_key=context.idempotency_key,
                tax_year=persisted_source.tax_year,
                rule_version=persisted_source.rule_version,
                findings=findings,
            )
        ),
    )


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


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
