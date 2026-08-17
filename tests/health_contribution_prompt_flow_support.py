"""Shared deterministic support for health-contribution prompt-flow tests."""

from __future__ import annotations

import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
import hashlib
from pathlib import Path
from dataclasses import dataclass

from fastapi.testclient import TestClient

from services.forms.app.main import create_app as create_forms_app
from services.reports.app.main import create_app as create_reports_app
from services.orchestration.app.main import create_app as create_orchestration_app
from services.reports.app.repository import ReportsRepository
from services.reports.app.repository import FinalizedLineageReference
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-20T12:00:00+03:00"


@dataclass(frozen=True)
class HealthPromptFixtureBinding:
    """Represent one supported health prompt mapped to one governed fixture lane."""

    fixture_name: str
    supported_lane_id: str
    historical_version_id: str
    regime_identifier: str
    tax_year: int
    report_type: str = "health_contribution_summary"


SUPPORTED_HEALTH_PROMPT_BINDINGS: dict[str, HealthPromptFixtureBinding] = {
    (
        "Compute health contribution for nhif legacy lane in tax year 2012 under "
        "HCH-VER-20100716-A."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_nhif_legacy_2010_case_001.json",
        supported_lane_id="health_contribution_nhif_legacy_v1_2010_07_16",
        historical_version_id="HCH-VER-20100716-A",
        regime_identifier="nhif_legacy",
        tax_year=2012,
    ),
    (
        "Compute health contribution for nhif legacy lane in tax year 2019 under "
        "HCH-VER-20150401-A."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_nhif_legacy_2015_case_001.json",
        supported_lane_id="health_contribution_nhif_legacy_v1_2015_04_01",
        historical_version_id="HCH-VER-20150401-A",
        regime_identifier="nhif_legacy",
        tax_year=2019,
    ),
    (
        "Compute health contribution for nhif legacy lane in tax year 2022 under "
        "HCH-VER-20210528-A."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_nhif_legacy_2021_case_001.json",
        supported_lane_id="health_contribution_nhif_legacy_v1_2021_05_28",
        historical_version_id="HCH-VER-20210528-A",
        regime_identifier="nhif_legacy",
        tax_year=2022,
    ),
    (
        "Compute health contribution for nhif legacy lane in tax year 2023 under "
        "HCH-VER-20221231-REG."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_nhif_legacy_case_001.json",
        supported_lane_id="health_contribution_nhif_legacy_v1_2022_12_31_reg",
        historical_version_id="HCH-VER-20221231-REG",
        regime_identifier="nhif_legacy",
        tax_year=2023,
    ),
    (
        "Compute health contribution for sha/shif salaried lane in tax year 2024 under "
        "HCH-VER-20241001-A."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_sha_shif_case_001.json",
        supported_lane_id="health_contribution_sha_shif_v1_2024_10_01",
        historical_version_id="HCH-VER-20241001-A",
        regime_identifier="sha_shif",
        tax_year=2024,
    ),
}

SUPPORTED_HEALTH_TRANSITION_PROMPT_BINDINGS: dict[str, HealthPromptFixtureBinding] = {
    (
        "Compute health contribution for transition boundary nhif lane in tax year 2023 under "
        "HCH-VER-20221231-REG."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_transition_boundary_nhif_case_001.json",
        supported_lane_id="health_contribution_nhif_legacy_v1_2022_12_31_reg",
        historical_version_id="HCH-VER-20221231-REG",
        regime_identifier="transition_boundary",
        tax_year=2023,
    ),
    (
        "Compute health contribution for transition boundary sha lane in tax year 2024 under "
        "HCH-VER-20241001-A."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_transition_boundary_sha_case_001.json",
        supported_lane_id="health_contribution_sha_shif_v1_2024_10_01",
        historical_version_id="HCH-VER-20241001-A",
        regime_identifier="transition_boundary",
        tax_year=2024,
    ),
    (
        "Compute health contribution for transition boundary sha lane in tax year 2025 under "
        "HCH-VER-20250228-PIT."
    ): HealthPromptFixtureBinding(
        fixture_name="health_contribution_sha_shif_2025_salaried_case_001.json",
        supported_lane_id="health_contribution_sha_shif_v1_2025_02_28_pit",
        historical_version_id="HCH-VER-20250228-PIT",
        regime_identifier="transition_boundary",
        tax_year=2025,
    ),
}

ALL_SUPPORTED_HEALTH_PROMPT_BINDINGS = {
    **SUPPORTED_HEALTH_PROMPT_BINDINGS,
    **SUPPORTED_HEALTH_TRANSITION_PROMPT_BINDINGS,
}


class HealthContributionPromptFlowError(RuntimeError):
    """Represent deterministic health prompt-flow failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured details for deterministic failures."""

        return {"reason": self.reason, **self._details}


def execute_health_contribution_prompt_flow(prompt_text: str) -> dict[str, object]:
    """Execute deterministic supported health-contribution prompt flow end-to-end."""

    binding = ALL_SUPPORTED_HEALTH_PROMPT_BINDINGS.get(prompt_text)
    if binding is None:
        raise HealthContributionPromptFlowError(
            reason="unsupported_prompt_scope",
            message="Prompt scope is not supported by governed health-contribution flow tests.",
            details={"prompt_text": prompt_text},
        )

    headers = _headers_for_prompt(prompt_text)
    orchestration_payload = _orchestration_payload_for_prompt(prompt_text)
    orchestration_app = create_orchestration_app()
    with TestClient(orchestration_app) as orchestration_client:
        ingestion = orchestration_client.post(
            "/v1/orchestration/prompt/ingest",
            json={
                "tenant_id": orchestration_payload["tenant_id"],
                "conversation_id": orchestration_payload["conversation_id"],
                "channel": orchestration_payload["channel"],
                "prompt": orchestration_payload["prompt"],
            },
            headers=headers,
        )
        decision = orchestration_client.post(
            "/v1/orchestration/prompt/decide",
            json={
                "tenant_id": orchestration_payload["tenant_id"],
                "conversation_id": orchestration_payload["conversation_id"],
                "channel": orchestration_payload["channel"],
                "prompt": orchestration_payload["prompt"],
            },
            headers=headers,
        )

    ingestion_payload = _response_json(ingestion)
    decision_payload = _response_json(decision)
    if ingestion.status_code != 200:
        raise HealthContributionPromptFlowError(
            reason="prompt_ingestion_failed",
            message="Prompt ingestion did not accept supported health prompt.",
            details=ingestion_payload,
        )
    if decision.status_code != 200:
        raise HealthContributionPromptFlowError(
            reason="prompt_decision_failed",
            message="Prompt decision did not resolve supported health prompt.",
            details=decision_payload,
        )

    execution_request = {
        **orchestration_payload,
        "intent_class": cast(str, decision_payload["intent_class"]),
        "tax_domain_hint": cast(str, decision_payload["tax_domain_hint"]),
        "decision_id": cast(str, decision_payload["decision_id"]),
        "selected_route": cast(dict[str, object], decision_payload["selected_route"]),
    }
    with TestClient(orchestration_app) as orchestration_client:
        execution = orchestration_client.post(
            "/v1/orchestration/prompt/execute",
            json=execution_request,
            headers=headers,
        )
    execution_payload = _response_json(execution)
    if execution.status_code != 200:
        raise HealthContributionPromptFlowError(
            reason="prompt_execution_failed",
            message="Prompt execution did not resolve supported health prompt.",
            details=execution_payload,
        )

    fixture = _load_fixture(binding.fixture_name)
    request_model = ComputationExecutionRequest.model_validate(fixture["request"])
    computation_output = execute_computation(request_model).model_dump(mode="json")
    finalized_output = _build_finalized_output(
        prompt_id=cast(str, ingestion_payload["ingestion_id"]),
        computation_output=computation_output,
    )

    forms_app = create_forms_app()
    with TestClient(forms_app) as forms_client:
        forms_mapping = forms_client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": headers["X-Correlation-ID"]},
        )
    forms_mapping_payload = _response_json(forms_mapping)
    if forms_mapping.status_code != 200:
        raise HealthContributionPromptFlowError(
            reason="forms_mapping_failed",
            message="Health downstream form mapping did not accept supported finalized output.",
            details=forms_mapping_payload,
        )

    reports_app = create_reports_app()
    reports_app.state.reports_repository = ReportsRepository(database_url="")
    reports_repository = cast(ReportsRepository, reports_app.state.reports_repository)
    reports_repository.reset()
    report_request = _register_and_build_report_request(
        reports_repository=reports_repository,
        binding=binding,
        finalized_output=finalized_output,
        forms_mapping_payload=forms_mapping_payload,
    )
    with TestClient(reports_app) as reports_client:
        reports_generation = reports_client.post(
            "/v1/reports/health-contribution/artifacts",
            json=report_request,
            headers={"X-Correlation-ID": headers["X-Correlation-ID"]},
        )
    reports_generation_payload = _response_json(reports_generation)
    if reports_generation.status_code != 201:
        raise HealthContributionPromptFlowError(
            reason="report_generation_failed",
            message="Health downstream report generation did not accept supported lineage.",
            details=reports_generation_payload,
        )

    return {
        "prompt_text": prompt_text,
        "ingestion": ingestion_payload,
        "decision": decision_payload,
        "execution": execution_payload,
        "computation_output": computation_output,
        "finalized_output": finalized_output,
        "forms_mapping": forms_mapping_payload,
        "report_generation": reports_generation_payload,
    }


def decide_health_contribution_prompt(prompt_text: str) -> tuple[int, dict[str, object]]:
    """Resolve one health prompt through the orchestration decision boundary."""

    payload = _orchestration_payload_for_prompt(prompt_text)
    headers = _headers_for_prompt(prompt_text)
    app = create_orchestration_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/orchestration/prompt/decide",
            json={
                "tenant_id": payload["tenant_id"],
                "conversation_id": payload["conversation_id"],
                "channel": payload["channel"],
                "prompt": payload["prompt"],
            },
            headers=headers,
        )
    return response.status_code, _response_json(response)


def _register_and_build_report_request(
    *,
    reports_repository: ReportsRepository,
    binding: HealthPromptFixtureBinding,
    finalized_output: dict[str, object],
    forms_mapping_payload: dict[str, object],
) -> dict[str, object]:
    mapping_output = _require_object(forms_mapping_payload, "mapping_output")
    version_identity = _require_object(mapping_output, "version_identity")
    lineage = _require_object(mapping_output, "lineage")
    form_id = str(uuid5(NAMESPACE_URL, f"{binding.fixture_name}:form"))
    reports_repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id=_require_string(finalized_output, "computation_id"),
            form_id=form_id,
            historical_version_id=_require_string(version_identity, "historical_version_id"),
            supported_lane_id=_require_string(mapping_output, "supported_lane_id"),
            tax_year=_require_int(finalized_output, "tax_year"),
            tax_type="health_contribution",
            policy_anchor_ids=tuple(_list_of_strings(lineage, "applied_policy_ids")),
            source_anchor_ids=tuple(_list_of_strings(lineage, "source_anchor_ids")),
        )
    )
    return {
        "computation_id": _require_string(finalized_output, "computation_id"),
        "form_id": form_id,
        "report_type": binding.report_type,
        "tax_year": _require_int(finalized_output, "tax_year"),
        "historical_version_id": _require_string(version_identity, "historical_version_id"),
        "supported_lane_id": _require_string(mapping_output, "supported_lane_id"),
    }


def _orchestration_payload_for_prompt(prompt_text: str) -> dict[str, object]:
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]
    return {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": f"user-health-{prompt_hash}",
        "conversation_id": f"conv-health-{prompt_hash}",
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
        "idempotency_key": f"idem-health-{prompt_hash}",
    }


def _headers_for_prompt(prompt_text: str) -> dict[str, str]:
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    return {
        "X-Correlation-ID": f"corr-health-{prompt_hash[:16]}",
        "X-Trace-ID": f"trace-health-{prompt_hash[16:32]}",
    }


def _build_finalized_output(
    *,
    prompt_id: str,
    computation_output: dict[str, object],
) -> dict[str, object]:
    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{prompt_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{prompt_id}:finalized-audit")),
        "tax_type": _require_string(computation_output, "tax_type"),
        "regime_type": _require_string(computation_output, "regime_type"),
        "tax_year": _require_int(computation_output, "tax_year"),
        "rule_version": _require_string(computation_output, "rule_version"),
        "input_hash": _require_string(computation_output, "input_hash"),
        "result_payload": _require_object(computation_output, "result_payload"),
    }


def _load_fixture(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    return cast(dict[str, object], json.loads(fixture_path.read_text(encoding="utf-8")))


def _require_object(source: dict[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, dict):
        raise HealthContributionPromptFlowError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing in health prompt flow.",
            details={"field_name": field_name},
        )
    return cast(dict[str, object], value)


def _require_string(source: dict[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise HealthContributionPromptFlowError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing in health prompt flow.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: dict[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise HealthContributionPromptFlowError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing in health prompt flow.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: dict[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise HealthContributionPromptFlowError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing in health prompt flow.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HealthContributionPromptFlowError(
                reason="missing_required_field",
                message=(
                    f"Required string list field '{field_name}' is malformed in health prompt flow."
                ),
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise HealthContributionPromptFlowError(
            reason="invalid_response_shape",
            message="Prompt flow response payload must be a JSON object.",
        )
    return cast(dict[str, object], payload)
