"""End-to-end UAT scenarios for supported forms income-tax scope."""

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

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_e2e_uat_supported_scope_full_workflow_is_operational_and_auditable() -> None:
    app = _create_isolated_forms_app()
    set_forms_retention_policy_now_override(datetime(2026, 4, 6, 9, 0, tzinfo=UTC))

    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T09:05:00+03:00",
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-uat-supported-happy"},
        )
        mapping_payload = _response_json(mapping)
        assert mapping.status_code == 200
        _assert_audit_evidence(
            mapping_payload["audit_evidence"],
            expected_event="forms_mapping_completed",
        )

        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-uat-supported-happy"},
        )
        binding_payload = _response_json(binding)
        assert binding.status_code == 200
        _assert_audit_evidence(
            binding_payload["audit_evidence"], expected_event="forms_version_binding_completed"
        )

        validation = client.post(
            "/v1/forms/income-tax/validations",
            json={
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-uat-supported-happy"},
        )
        validation_payload = _response_json(validation)
        assert validation.status_code == 200
        assert validation_payload["status"] == "ok"
        assert validation_payload["is_valid"] is True

        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        artifact_payload = _response_json(artifact)
        assert artifact.status_code == 201
        _assert_audit_evidence(
            artifact_payload["audit_evidence"], expected_event="forms_artifact_generated"
        )
        artifact_id = cast(str, artifact_payload["artifact_id"])
        form_version_id = cast(str, artifact_payload["form_version_id"])

        versions = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "uat-user",
                "tax_year": artifact_payload["tax_year"],
                "form_type": "income_tax_return",
            },
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        versions_payload = _response_json(versions)
        assert versions.status_code == 200
        assert versions_payload["status"] == "ok"
        assert cast(list[dict[str, object]], versions_payload["versions"])

        metadata_before = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        metadata_before_payload = _response_json(metadata_before)
        assert metadata_before.status_code == 200
        artifact_metadata_before = cast(
            dict[str, object], metadata_before_payload["artifact_metadata"]
        )
        assert artifact_metadata_before["artifact_id"] == artifact_id
        assert artifact_metadata_before["form_version_id"] == form_version_id

        issuance = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        issuance_payload = _response_json(issuance)
        assert issuance.status_code == 200
        assert issuance_payload["status"] == "issued"
        _assert_audit_evidence(
            issuance_payload["audit_evidence"], expected_event="forms_download_link_issued"
        )

        metadata_after = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        metadata_after_payload = _response_json(metadata_after)
        assert metadata_after.status_code == 200
        artifact_metadata_after = cast(
            dict[str, object],
            metadata_after_payload["artifact_metadata"],
        )
        download_metadata = cast(dict[str, object], artifact_metadata_after["download_metadata"])
        assert download_metadata["available"] is True
        assert download_metadata["expires_at"] == issuance_payload["expires_at"]

        checklist = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={"X-User-ID": "uat-user", "X-Correlation-ID": "forms-uat-supported-happy"},
        )
        checklist_payload = _response_json(checklist)
        assert checklist.status_code == 200
        assert checklist_payload["status"] == "ok"
        assert checklist_payload["overall_status"] == "ready"
        checklist_items = cast(list[dict[str, object]], checklist_payload["items"])
        assert checklist_items
        assert any(item["code"] == "download_window_issued" for item in checklist_items)

    reset_forms_retention_policy_now_override()


def test_e2e_uat_supported_scope_repeated_success_query_is_deterministic() -> None:
    app = _create_isolated_forms_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T10:00:00+03:00",
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
            headers={"X-User-ID": "det-uat-user", "X-Correlation-ID": "forms-uat-success-det"},
        )
        artifact_payload = _response_json(artifact)
        assert artifact.status_code == 201

        first = client.get(
            "/v1/forms/income-tax/versions",
            params={"user_id": "det-uat-user", "tax_year": 2021, "form_type": "income_tax_return"},
            headers={"X-User-ID": "det-uat-user", "X-Correlation-ID": "forms-uat-success-det"},
        )
        second = client.get(
            "/v1/forms/income-tax/versions",
            params={"user_id": "det-uat-user", "tax_year": 2021, "form_type": "income_tax_return"},
            headers={"X-User-ID": "det-uat-user", "X-Correlation-ID": "forms-uat-success-det"},
        )

    assert artifact_payload["status"] == "ok"
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def _create_isolated_forms_app() -> Any:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    reset_forms_retention_policy_now_override()
    return create_app()


def _assert_audit_evidence(raw: object, *, expected_event: str) -> None:
    assert isinstance(raw, dict)
    evidence = cast(dict[str, object], raw)
    assert isinstance(evidence.get("audit_event_id"), str) and evidence["audit_event_id"]
    assert evidence.get("event_type") == expected_event
    assert isinstance(evidence.get("event_timestamp"), str) and evidence["event_timestamp"]
    assert isinstance(evidence.get("trace_id"), str) and evidence["trace_id"]
    assert isinstance(evidence.get("correlation_id"), str) and evidence["correlation_id"]
    lineage_reference = cast(dict[str, object], evidence["lineage_reference"])
    assert lineage_reference.get("form_type") == "income_tax_return"
    assert isinstance(lineage_reference.get("tax_year"), int)


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
