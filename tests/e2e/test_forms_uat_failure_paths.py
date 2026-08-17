"""End-to-end UAT critical failure-path scenarios for forms supported scope."""

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

import pytest
from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
DISABLED_TEMPLATE_CODES = ("IT2", "VAT3", "P10", "P9")


def test_e2e_uat_validation_blocked_generation_is_deterministic() -> None:
    app = _create_isolated_forms_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T11:00:00+03:00",
    )
    mapped_output = map_finalized_income_tax_output_to_form_ready(copy.deepcopy(finalized_output))
    bound_output = bind_income_tax_form_version(copy.deepcopy(mapped_output))
    invalid_binding = copy.deepcopy(bound_output)
    binding_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    binding_lineage["computation_id"] = "mismatch-computation-id"
    invalid_binding["binding_lineage"] = binding_lineage

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapped_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-uat-failure-validation-blocked"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapped_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-uat-failure-validation-blocked"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first_payload["status"] == "blocked"
    assert first_payload["reason"] == "forms_generation_blocked_by_validation"
    assert second_payload["status"] == "blocked"
    assert second_payload["reason"] == "forms_generation_blocked_by_validation"
    first_validation = cast(dict[str, object], first_payload["validation"])
    second_validation = cast(dict[str, object], second_payload["validation"])
    first_codes = [
        cast(str, finding["code"])
        for finding in cast(list[dict[str, object]], first_validation["findings"])
    ]
    second_codes = [
        cast(str, finding["code"])
        for finding in cast(list[dict[str, object]], second_validation["findings"])
    ]
    assert first_codes == second_codes


def test_e2e_uat_unauthorized_access_denials_are_canonical_and_deterministic() -> None:
    app = _create_isolated_forms_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T11:30:00+03:00",
    )
    mapped_output = map_finalized_income_tax_output_to_form_ready(copy.deepcopy(finalized_output))
    bound_output = bind_income_tax_form_version(copy.deepcopy(mapped_output))

    with TestClient(app) as client:
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapped_output,
                "form_version_binding": bound_output,
            },
            headers={
                "X-User-ID": "owner-user",
                "X-Correlation-ID": "forms-uat-failure-unauthorized",
            },
        )
        artifact_payload = _response_json(artifact)
        assert artifact.status_code == 201
        artifact_id = cast(str, artifact_payload["artifact_id"])
        form_version_id = cast(str, artifact_payload["form_version_id"])

        unauthorized_metadata = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-uat-failure-unauthorized",
            },
        )
        unauthorized_download_first = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-uat-failure-unauthorized",
            },
        )
        unauthorized_download_second = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-uat-failure-unauthorized",
            },
        )

    _assert_canonical_error(
        unauthorized_metadata, expected_status=403, expected_reason="forms_unauthorized_access"
    )
    first_error = _assert_canonical_error(
        unauthorized_download_first,
        expected_status=403,
        expected_reason="forms_download_not_authorized",
    )
    second_error = _assert_canonical_error(
        unauthorized_download_second,
        expected_status=403,
        expected_reason="forms_download_not_authorized",
    )
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_e2e_uat_expired_download_denial_is_canonical_and_deterministic() -> None:
    app = _create_isolated_forms_app()
    set_forms_retention_policy_now_override(datetime(2026, 4, 6, 12, 0, tzinfo=UTC))
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T12:05:00+03:00",
    )
    mapped_output = map_finalized_income_tax_output_to_form_ready(copy.deepcopy(finalized_output))
    bound_output = bind_income_tax_form_version(copy.deepcopy(mapped_output))

    try:
        with TestClient(app) as client:
            artifact = client.post(
                "/v1/forms/income-tax/artifacts",
                json={
                    "finalized_output": finalized_output,
                    "form_ready_output": mapped_output,
                    "form_version_binding": bound_output,
                },
                headers={
                    "X-User-ID": "expiry-user",
                    "X-Correlation-ID": "forms-uat-failure-expired",
                },
            )
            artifact_payload = _response_json(artifact)
            assert artifact.status_code == 201
            artifact_id = cast(str, artifact_payload["artifact_id"])
            form_version_id = cast(str, artifact_payload["form_version_id"])

            first_issuance = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "expiry-user",
                    "X-Correlation-ID": "forms-uat-failure-expired",
                },
            )
            first_issuance_payload = _response_json(first_issuance)
            assert first_issuance.status_code == 200
            expires_at = datetime.fromisoformat(
                cast(str, first_issuance_payload["expires_at"])
            ).astimezone(UTC)
            set_forms_retention_policy_now_override(expires_at + timedelta(seconds=1))

            second = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "expiry-user",
                    "X-Correlation-ID": "forms-uat-failure-expired",
                },
            )
            third = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "expiry-user",
                    "X-Correlation-ID": "forms-uat-failure-expired",
                },
            )
    finally:
        reset_forms_retention_policy_now_override()

    second_error = _assert_canonical_error(
        second, expected_status=403, expected_reason="forms_download_link_expired"
    )
    third_error = _assert_canonical_error(
        third, expected_status=403, expected_reason="forms_download_link_expired"
    )
    assert second_error["error_code"] == third_error["error_code"]
    assert second_error["reason"] == third_error["reason"]


@pytest.mark.parametrize("template_code", DISABLED_TEMPLATE_CODES)
def test_e2e_uat_disabled_template_requests_are_rejected(template_code: str) -> None:
    app = _create_isolated_forms_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T13:00:00+03:00",
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"template_code": template_code, "finalized_output": finalized_output},
            headers={"X-Correlation-ID": f"forms-uat-failure-template-{template_code}"},
        )

    error = _assert_canonical_error(
        response, expected_status=409, expected_reason="forms_template_capability_disabled"
    )
    details = error.get("details")
    assert isinstance(details, dict)
    details_map = cast(dict[str, object], details)
    assert details_map.get("template_code") == template_code


def test_e2e_uat_recognized_tax_domain_mapping_remains_fail_closed() -> None:
    app = _create_isolated_forms_app()
    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/vat/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-uat-failure-unsupported-scope"},
        )
        second = client.post(
            "/v1/forms/vat/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-uat-failure-unsupported-scope"},
        )

    first_error = _assert_canonical_error(
        first, expected_status=501, expected_reason="unimplemented_tax_domain_mapping"
    )
    second_error = _assert_canonical_error(
        second, expected_status=501, expected_reason="unimplemented_tax_domain_mapping"
    )
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def _create_isolated_forms_app() -> Any:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    reset_forms_retention_policy_now_override()
    return create_app()


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


def _assert_canonical_error(
    response: Any,
    *,
    expected_status: int,
    expected_reason: str,
) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert response.status_code == expected_status
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert detail["error_code"] == expected_reason
    assert detail["reason"] == expected_reason
    return cast(dict[str, object], detail)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
