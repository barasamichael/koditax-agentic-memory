"""DB-backed deterministic tests for reports repository persistence."""

from __future__ import annotations

import os
from uuid import UUID
from uuid import uuid4
from typing import cast
from pathlib import Path
from collections.abc import Iterator

import pytest
import psycopg

from services.reports.app.authz import ReportAccessContext
from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportGenerationResponseModel
from services.reports.app.repository import ReportsRepository
from services.reports.app.repository import ReportRepositoryError

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create per-test PostgreSQL transaction boundary for repository tests."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping reports repository DB tests.")

    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping reports repository DB tests.")
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_reports_repository_create_and_get_roundtrip(
    db_connection: psycopg.Connection,
) -> None:
    fixtures = _insert_reports_lineage_fixture(db_connection)
    repository = ReportsRepository(database_url=_load_database_url())
    report = _build_report_response(fixtures=fixtures)

    created = repository.create_report_record(
        report=report,
        access_context=ReportAccessContext(
            owner_user_id=str(fixtures["user_id"]),
            tenant_id="tenant-a",
        ),
    )
    fetched = repository.get_report_by_id(report_id=report.report_id)

    assert created.report_payload["report_id"] == report.report_id
    assert fetched is not None
    assert fetched.report_payload["report_id"] == report.report_id
    assert fetched.report_payload["report_version_id"] == report.report_version_id


def test_reports_repository_required_lineage_fields_persist_and_reload(
    db_connection: psycopg.Connection,
) -> None:
    fixtures = _insert_reports_lineage_fixture(db_connection)
    repository = ReportsRepository(database_url=_load_database_url())
    report = _build_report_response(fixtures=fixtures)

    repository.create_report_record(
        report=report,
        access_context=ReportAccessContext(
            owner_user_id=str(fixtures["user_id"]),
            tenant_id="tenant-a",
        ),
    )
    reloaded = repository.get_report_by_id(report_id=report.report_id)
    assert reloaded is not None
    lineage = cast(dict[str, object], reloaded.report_payload["lineage_reference"])
    assert lineage["computation_id"] == str(fixtures["computation_id"])
    assert lineage["form_id"] == str(fixtures["form_id"])
    assert lineage["historical_version_id"] == "KIT-VER-20230701-A"
    assert lineage["supported_lane_id"] == "resident_employment_income_2023_07_01"
    assert lineage["tax_type"] == "income_tax"
    assert lineage["tax_year"] == 2023


def test_reports_repository_unknown_report_returns_none(
    db_connection: psycopg.Connection,
) -> None:
    repository = ReportsRepository(database_url=_load_database_url())
    missing = repository.get_report_by_id(report_id=str(uuid4()))
    assert missing is None


def test_reports_repository_duplicate_create_is_deterministic(
    db_connection: psycopg.Connection,
) -> None:
    fixtures = _insert_reports_lineage_fixture(db_connection)
    repository = ReportsRepository(database_url=_load_database_url())
    report = _build_report_response(fixtures=fixtures)
    context = ReportAccessContext(
        owner_user_id=str(fixtures["user_id"]),
        tenant_id="tenant-a",
    )

    first = repository.create_report_record(report=report, access_context=context)
    second = repository.create_report_record(report=report, access_context=context)
    assert first.report_payload["report_id"] == second.report_payload["report_id"]
    assert first.created_at == second.created_at


def test_reports_repository_rejects_missing_fk_lineage(
    db_connection: psycopg.Connection,
) -> None:
    fixtures = _insert_reports_lineage_fixture(db_connection)
    repository = ReportsRepository(database_url=_load_database_url())
    report = _build_report_response(
        fixtures={**fixtures, "form_id": uuid4()},
    )
    with pytest.raises(ReportRepositoryError) as error_info:
        repository.create_report_record(
            report=report,
            access_context=ReportAccessContext(
                owner_user_id=str(fixtures["user_id"]),
                tenant_id="tenant-a",
            ),
        )
    assert error_info.value.reason_code == "invalid_lineage_reference"


def _insert_reports_lineage_fixture(connection: psycopg.Connection) -> dict[str, UUID]:
    user_id = uuid4()
    computation_id = uuid4()
    form_id = uuid4()
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
                f"reports-phone-{suffix}",
                f"reports-{suffix}@example.com",
                "IndividualTaxpayer",
            ),
        )
        cursor.execute(
            """
            INSERT INTO computations (
                id,
                user_id,
                tax_type,
                regime_type,
                tax_year,
                rule_version,
                input_hash,
                idempotency_key,
                correlation_id,
                retention_expires_at,
                compliance_lock_until
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '30 days', now())
            """,
            (
                computation_id,
                user_id,
                "income_tax",
                "income_tax",
                2023,
                "v1",
                f"reports-input-hash-{suffix}",
                f"reports-idem-{suffix}",
                f"reports-corr-{suffix}",
            ),
        )
        cursor.execute(
            """
            INSERT INTO forms (
                id,
                user_id,
                computation_id,
                form_type,
                form_version,
                retention_expires_at
            )
            VALUES (%s, %s, %s, %s, %s, now() + interval '30 days')
            """,
            (
                form_id,
                user_id,
                computation_id,
                "income_tax_return",
                "income_tax_vertical_slice_v1",
            ),
        )
    return {
        "user_id": user_id,
        "computation_id": computation_id,
        "form_id": form_id,
    }


def _build_report_response(
    *,
    fixtures: dict[str, UUID],
) -> ReportGenerationResponseModel:
    report_id = str(uuid4())
    return ReportGenerationResponseModel(
        status="generated",
        report_id=report_id,
        report_type="income_tax_summary",
        tax_year=2023,
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage_reference=ReportLineageModel(
            computation_id=str(fixtures["computation_id"]),
            form_id=str(fixtures["form_id"]),
            report_id=report_id,
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            historical_version_id="KIT-VER-20230701-A",
            supported_lane_id="resident_employment_income_2023_07_01",
            tax_type="income_tax",
            tax_year=2023,
            policy_anchor_ids=("POL-001",),
            source_anchor_ids=("SRC-001",),
        ),
    )


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    env_values = _read_env_values()
    direct_value = env_values.get(DATABASE_URL_ENV_VAR)
    if direct_value:
        return direct_value

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def _read_env_values() -> dict[str, str]:
    env_file = Path(".env")
    if not env_file.exists():
        return {}
    try:
        raw_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")
    return values
