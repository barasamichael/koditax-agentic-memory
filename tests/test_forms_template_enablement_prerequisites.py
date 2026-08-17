"""Deterministic prerequisite-evaluation checks for forms template capabilities."""

from __future__ import annotations

import copy
from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.template_registry import FormsTemplateRegistryError
from services.forms.app.template_registry import load_forms_template_capabilities
from services.forms.app.template_registry import load_forms_template_capability_manifest
from services.forms.app.template_registry import evaluate_forms_template_enablement_status
from services.forms.app.template_registry import ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES
from services.forms.app.template_registry import validate_forms_template_capability_manifest


def test_incomplete_prerequisites_remain_blocked_and_disabled_deterministically() -> None:
    capabilities = load_forms_template_capabilities()

    for capability in capabilities:
        assert capability["status"] == "disabled"
        assert capability["enablement_status"] == ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES
        assert (
            evaluate_forms_template_enablement_status(capability)
            == ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES
        )


def test_missing_prerequisite_field_fails_validation_deterministically() -> None:
    manifest = load_forms_template_capability_manifest()
    mutated_manifest = copy.deepcopy(manifest)
    templates = cast(list[dict[str, object]], mutated_manifest["templates"])
    prerequisites = cast(dict[str, bool], templates[0]["enablement_prerequisites"])
    prerequisites.pop("validation_rules_ready")

    with pytest.raises(FormsTemplateRegistryError) as error_info:
        validate_forms_template_capability_manifest(mutated_manifest)

    assert error_info.value.reason == "forms_template_capability_missing_required_field"


def test_enabled_state_with_unresolved_prerequisites_is_rejected_deterministically() -> None:
    manifest = load_forms_template_capability_manifest()
    mutated_manifest = copy.deepcopy(manifest)
    templates = cast(list[dict[str, object]], mutated_manifest["templates"])
    templates[0]["status"] = "enabled"

    with pytest.raises(FormsTemplateRegistryError) as error_info:
        validate_forms_template_capability_manifest(mutated_manifest)

    assert (
        error_info.value.reason == "forms_template_capability_enablement_blocked_by_prerequisites"
    )


def test_prerequisite_evaluation_decision_is_deterministic_for_same_input() -> None:
    first = load_forms_template_capabilities()
    second = load_forms_template_capabilities()
    first_decisions = [
        {
            "template_code": cast(str, item["template_code"]),
            "enablement_status": evaluate_forms_template_enablement_status(item),
        }
        for item in first
    ]
    second_decisions = [
        {
            "template_code": cast(str, item["template_code"]),
            "enablement_status": evaluate_forms_template_enablement_status(item),
        }
        for item in second
    ]

    assert canonical_json_dumps(first_decisions) == canonical_json_dumps(second_decisions)
