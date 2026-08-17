"""Taxonomy and immutable-evidence drift guard tests for forms audit events."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
import logging
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from services.forms.app.audit_events import REQUIRED_AUDIT_EVENT_TYPES
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_ACCESS_DENIED
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_ARTIFACT_GENERATED
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_VALIDATION_EXECUTED
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_DOWNLOAD_LINK_ISSUED
from services.forms.app.audit_events import FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
TAXONOMY_DOC_PATH = Path("docs/governance/phase-10-forms-audit-event-taxonomy.md")


def test_required_audit_event_types_are_governed_in_taxonomy_doc() -> None:
    taxonomy_text = TAXONOMY_DOC_PATH.read_text(encoding="utf-8")
    for event_type in sorted(REQUIRED_AUDIT_EVENT_TYPES):
        assert event_type in taxonomy_text


def test_forms_pipeline_audit_evidence_envelope_contains_required_fields() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T09:00:00+03:00",
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-pipeline"},
        )
        mapping_payload = _response_json(mapping)
        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-pipeline"},
        )
        binding_payload = _response_json(binding)
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-audit-taxonomy-pipeline"},
        )
        artifact_payload = _response_json(artifact)

    assert mapping.status_code == 200
    assert binding.status_code == 200
    assert artifact.status_code == 201

    _assert_audit_evidence_envelope(mapping_payload["audit_evidence"])
    _assert_audit_evidence_envelope(binding_payload["audit_evidence"])
    _assert_audit_evidence_envelope(artifact_payload["audit_evidence"])
    artifact_audit = cast(dict[str, object], artifact_payload["audit_evidence"])
    assert artifact_audit["event_type"] == FORMS_AUDIT_EVENT_ARTIFACT_GENERATED


def test_validation_endpoint_emits_canonical_audit_log_event(caplog: Any) -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T09:15:00+03:00",
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-validation"},
        )
        mapping_payload = _response_json(mapping)
        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-validation"},
        )
        binding_payload = _response_json(binding)

        with caplog.at_level(logging.INFO, logger="kodi.forms.audit"):
            response = client.post(
                "/v1/forms/income-tax/validations",
                json={
                    "form_ready_output": mapping_payload["mapping_output"],
                    "form_version_binding": binding_payload["binding_output"],
                },
                headers={"X-Correlation-ID": "forms-audit-taxonomy-validation"},
            )

    assert response.status_code == 200
    audit_events = _extract_audit_events_from_caplog(caplog)
    matching = [
        event
        for event in audit_events
        if event.get("event_type") == FORMS_AUDIT_EVENT_VALIDATION_EXECUTED
    ]
    assert matching
    _assert_audit_log_payload(matching[0])


def test_history_persistence_emits_canonical_audit_log_event(caplog: Any) -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    finalized_output = _build_finalized_output(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-06T09:30:00+03:00",
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-history"},
        )
        mapping_payload = _response_json(mapping)
        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-history"},
        )
        binding_payload = _response_json(binding)

        with caplog.at_level(logging.INFO, logger="kodi.forms.audit"):
            artifact = client.post(
                "/v1/forms/income-tax/artifacts",
                json={
                    "finalized_output": finalized_output,
                    "form_ready_output": mapping_payload["mapping_output"],
                    "form_version_binding": binding_payload["binding_output"],
                },
                headers={"X-Correlation-ID": "forms-audit-taxonomy-history"},
            )

    assert artifact.status_code == 201
    audit_events = _extract_audit_events_from_caplog(caplog)
    matching = [
        event
        for event in audit_events
        if event.get("event_type") == FORMS_AUDIT_EVENT_HISTORY_RECORD_PERSISTED
    ]
    assert matching
    _assert_audit_log_payload(matching[0])


def test_retention_access_denial_emits_canonical_audit_log_event(caplog: Any) -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    reset_forms_retention_policy_now_override()
    app = create_app()

    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-06T10:00:00+03:00",
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-access-denied"},
        )
        mapping_payload = _response_json(mapping)
        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-audit-taxonomy-access-denied"},
        )
        binding_payload = _response_json(binding)
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-audit-taxonomy-access-denied"},
        )
        artifact_payload = _response_json(artifact)

        artifact_id = cast(str, artifact_payload["artifact_id"])
        form_version_id = cast(str, artifact_payload["form_version_id"])
        created_at = datetime.fromisoformat(cast(str, artifact_payload["created_at"]))
        set_forms_retention_policy_now_override(created_at.astimezone(UTC) + timedelta(days=370))

        with caplog.at_level(logging.INFO, logger="kodi.forms.audit"):
            denied = client.get(
                (
                    "/v1/forms/income-tax/artifacts/"
                    f"{artifact_id}/versions/{form_version_id}/metadata"
                ),
                headers={"X-Correlation-ID": "forms-audit-taxonomy-access-denied"},
            )

    reset_forms_retention_policy_now_override()
    assert denied.status_code == 403
    audit_events = _extract_audit_events_from_caplog(caplog)
    matching = [
        event
        for event in audit_events
        if event.get("event_type") == FORMS_AUDIT_EVENT_ACCESS_DENIED
    ]
    assert matching
    _assert_audit_log_payload(matching[0])


def test_download_link_issuance_audit_evidence_includes_required_lineage_keys() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-06T10:30:00+03:00",
            user_id="download-owner",
        )
        response = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-audit-taxonomy-download",
            },
        )

    payload = _response_json(response)
    assert response.status_code == 200
    audit_evidence = cast(dict[str, object], payload["audit_evidence"])
    _assert_audit_evidence_envelope(audit_evidence)
    assert audit_evidence["event_type"] == FORMS_AUDIT_EVENT_DOWNLOAD_LINK_ISSUED

    lineage = cast(dict[str, object], audit_evidence["lineage_reference"])
    assert lineage["artifact_id"] == artifact_id
    assert lineage["form_version_id"] == form_version_id
    assert lineage["form_type"] == "income_tax_return"
    assert isinstance(lineage.get("tax_year"), int)


def _assert_audit_evidence_envelope(raw: object) -> None:
    assert isinstance(raw, dict)
    envelope = cast(dict[str, object], raw)
    assert set(envelope.keys()) == {
        "audit_event_id",
        "event_type",
        "event_timestamp",
        "trace_id",
        "correlation_id",
        "lineage_reference",
        "actor_context",
    }
    assert isinstance(envelope["audit_event_id"], str) and envelope["audit_event_id"]
    assert isinstance(envelope["event_type"], str) and envelope["event_type"]
    assert isinstance(envelope["event_timestamp"], str) and envelope["event_timestamp"]
    assert isinstance(envelope["trace_id"], str) and envelope["trace_id"]
    assert isinstance(envelope["correlation_id"], str) and envelope["correlation_id"]
    assert isinstance(envelope["lineage_reference"], dict)
    assert isinstance(envelope["actor_context"], dict)

    lineage = cast(dict[str, object], envelope["lineage_reference"])
    assert lineage.get("form_type") == "income_tax_return"
    assert isinstance(lineage.get("tax_year"), int)

    actor = cast(dict[str, object], envelope["actor_context"])
    assert actor.get("actor_type") == "user"


def _assert_audit_log_payload(event: dict[str, object]) -> None:
    assert set(event.keys()) == {
        "audit_event_id",
        "event_type",
        "event_timestamp",
        "trace_id",
        "correlation_id",
        "lineage_reference",
        "actor_context",
    }
    assert isinstance(event["audit_event_id"], str) and event["audit_event_id"]
    assert isinstance(event["event_type"], str)
    assert event["event_type"] in REQUIRED_AUDIT_EVENT_TYPES
    assert isinstance(event["event_timestamp"], str) and event["event_timestamp"]
    assert isinstance(event["trace_id"], str) and event["trace_id"]
    assert isinstance(event["correlation_id"], str) and event["correlation_id"]
    assert isinstance(event["lineage_reference"], dict)
    assert isinstance(event["actor_context"], dict)
    actor = cast(dict[str, object], event["actor_context"])
    assert actor.get("actor_type") == "user"


def _extract_audit_events_from_caplog(caplog: Any) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in caplog.records:
        message = record.getMessage()
        if not isinstance(message, str):
            continue
        if not message.startswith("forms_audit_event "):
            continue
        raw_json = message[len("forms_audit_event ") :]
        payload = json.loads(raw_json)
        if isinstance(payload, dict):
            events.append(cast(dict[str, object], payload))
    return events


def _generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> tuple[str, str]:
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    response = client.post(
        "/v1/forms/income-tax/artifacts",
        json={
            "finalized_output": finalized_output,
            "form_ready_output": form_ready_output,
            "form_version_binding": form_version_binding,
        },
        headers={"X-User-ID": user_id, "X-Correlation-ID": f"{user_id}-{fixture_name}"},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return cast(str, payload["artifact_id"]), cast(str, payload["form_version_id"])


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
