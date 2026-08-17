"""Forms metrics-baseline regression tests for generation and download paths."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from services.forms.app.main import list_forms_metric_events
from services.forms.app.main import reset_forms_metric_events
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.observability import FORMS_GENERATION_LATENCY_MS
from services.forms.app.observability import FORMS_GENERATION_FAILURE_TOTAL
from services.forms.app.observability import FORMS_GENERATION_SUCCESS_TOTAL
from services.forms.app.observability import FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL
from services.forms.app.observability import FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS
from services.forms.app.observability import FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_generation_and_download_success_emit_metrics() -> None:
    app = _create_isolated_forms_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T12:00:00+03:00",
    )

    with TestClient(app) as client:
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-User-ID": "metrics-owner", "X-Correlation-ID": "forms-metrics-success"},
        )
        artifact_payload = _response_json(artifact)
        download = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_payload['artifact_id']}/versions/{artifact_payload['form_version_id']}"
                "/download-links"
            ),
            headers={"X-User-ID": "metrics-owner", "X-Correlation-ID": "forms-metrics-success"},
        )

    assert artifact.status_code == 201
    assert download.status_code == 200

    events = list_forms_metric_events(app_instance=app)
    metric_ids = {event.metric_id for event in events}
    assert FORMS_GENERATION_SUCCESS_TOTAL in metric_ids
    assert FORMS_GENERATION_LATENCY_MS in metric_ids
    assert FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL in metric_ids
    assert FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS in metric_ids


def test_download_access_denied_metrics_cover_auth_expiry_and_retention() -> None:
    app = _create_isolated_forms_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T13:00:00+03:00",
    )

    with TestClient(app) as client:
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-User-ID": "owner-user", "X-Correlation-ID": "forms-metrics-denied"},
        )
        artifact_payload = _response_json(artifact)
        artifact_id = cast(str, artifact_payload["artifact_id"])
        form_version_id = cast(str, artifact_payload["form_version_id"])
        created_at = datetime.fromisoformat(cast(str, artifact_payload["created_at"])).astimezone(
            UTC
        )

        unauthorized = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "wrong-user", "X-Correlation-ID": "forms-metrics-denied"},
        )
        _assert_error_reason(
            unauthorized,
            expected_status=403,
            expected_reason="forms_download_not_authorized",
        )

        issued = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "owner-user", "X-Correlation-ID": "forms-metrics-denied"},
        )
        issued_payload = _response_json(issued)
        assert issued.status_code == 200

        set_forms_retention_policy_now_override(
            datetime.fromisoformat(cast(str, issued_payload["expires_at"])).astimezone(UTC)
            + timedelta(seconds=1)
        )
        expired = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "owner-user", "X-Correlation-ID": "forms-metrics-denied"},
        )
        _assert_error_reason(
            expired,
            expected_status=403,
            expected_reason="forms_download_link_expired",
        )

        set_forms_retention_policy_now_override(created_at + timedelta(days=370))
        retention_denied = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "owner-user", "X-Correlation-ID": "forms-metrics-denied"},
        )
        _assert_error_reason(
            retention_denied,
            expected_status=403,
            expected_reason="forms_artifact_retention_expired",
        )

    denied_events = [
        event
        for event in list_forms_metric_events(app_instance=app)
        if event.metric_id == FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL
    ]
    denial_classes = {event.dimensions["denial_class"] for event in denied_events}
    assert denial_classes == {"auth", "expiry", "retention"}


def test_generation_failure_emits_failure_metric_without_contract_drift() -> None:
    app = _create_isolated_forms_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={"form_ready_output": {}, "form_version_binding": {}},
            headers={"X-Correlation-ID": "forms-metrics-generation-failure"},
        )

    _assert_error_reason(
        response,
        expected_status=400,
        expected_reason="forms_generation_precondition_missing",
    )
    events = list_forms_metric_events(app_instance=app)
    failure_events = [
        event for event in events if event.metric_id == FORMS_GENERATION_FAILURE_TOTAL
    ]
    assert failure_events
    assert failure_events[0].dimensions["reason_code"] == "forms_generation_precondition_missing"


def _create_isolated_forms_app() -> Any:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    reset_forms_retention_policy_now_override()
    app = create_app()
    reset_forms_metric_events(app_instance=app)
    return app


def _assert_error_reason(response: Any, *, expected_status: int, expected_reason: str) -> None:
    payload = _response_json(response)
    assert response.status_code == expected_status
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_map = cast(dict[str, object], detail)
    assert set(detail_map).issuperset({"error_code", "message", "reason"})
    assert detail_map["error_code"] == expected_reason
    assert detail_map["reason"] == expected_reason


def _build_generation_inputs(
    *,
    fixture_name: str,
    finalized_at: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name=fixture_name, finalized_at=finalized_at)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return finalized_output, form_ready_output, form_version_binding


def _build_finalized_output(*, fixture_name: str, finalized_at: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])
    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": finalized_at,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:finalized-audit")),
        "tax_type": expected_output["tax_type"],
        "regime_type": expected_output["regime_type"],
        "tax_year": expected_output["tax_year"],
        "rule_version": expected_output["rule_version"],
        "input_hash": expected_output["input_hash"],
        "result_payload": expected_output["result_payload"],
    }


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
