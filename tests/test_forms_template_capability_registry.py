"""Deterministic checks for forms extension-template capability registry baseline."""

from __future__ import annotations

import copy
from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.template_registry import FormsTemplateRegistryError
from services.forms.app.template_registry import load_forms_template_capabilities
from services.forms.app.template_registry import REQUIRED_DISABLED_TEMPLATE_CODES
from services.forms.app.template_registry import get_forms_template_capability_entry
from services.forms.app.template_registry import build_forms_template_capability_index
from services.forms.app.template_registry import load_forms_template_capability_manifest
from services.forms.app.template_registry import REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS
from services.forms.app.template_registry import ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES
from services.forms.app.template_registry import validate_forms_template_capability_manifest

EXPECTED_TEMPLATE_CODES = ("IT2", "P10", "P9", "VAT3")


def test_forms_template_registry_manifest_loads_with_required_governance_fields() -> None:
    manifest = load_forms_template_capability_manifest()

    assert manifest["manifest_version"] == "1.0.0"
    assert manifest["capability_scope"] == "forms_extended_template_registry"
    assert manifest["generated_at"] == "2026-04-03T00:00:00+03:00"
    supported_runtime_scope = manifest["supported_runtime_scope"]
    assert isinstance(supported_runtime_scope, list)
    assert supported_runtime_scope == ["income_tax_return_it1"]
    assert isinstance(manifest["notes"], str) and manifest["notes"]


def test_forms_template_registry_required_entries_exist_and_are_disabled() -> None:
    capabilities = load_forms_template_capabilities()
    template_codes = [item["template_code"] for item in capabilities]

    assert template_codes == list(EXPECTED_TEMPLATE_CODES)
    assert set(template_codes) == REQUIRED_DISABLED_TEMPLATE_CODES
    for capability in capabilities:
        assert capability["status"] == "disabled"
        assert capability["enablement_status"] == ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES
        assert isinstance(capability["domain"], str) and capability["domain"]
        assert isinstance(capability["effective_scope"], str) and capability["effective_scope"]
        assert isinstance(capability["notes"], str) and capability["notes"]
        prerequisites = cast(dict[str, bool], capability["enablement_prerequisites"])
        assert list(prerequisites.keys()) == list(REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS)
        assert all(value is False for value in prerequisites.values())


def test_forms_template_registry_rejects_accidental_enabled_required_template() -> None:
    manifest = load_forms_template_capability_manifest()
    mutated = copy.deepcopy(manifest)
    templates = cast(list[dict[str, object]], mutated["templates"])
    templates[0]["status"] = "enabled"

    with pytest.raises(FormsTemplateRegistryError) as error_info:
        validate_forms_template_capability_manifest(mutated)

    assert (
        error_info.value.reason == "forms_template_capability_enablement_blocked_by_prerequisites"
    )


def test_forms_template_registry_rejects_missing_required_fields() -> None:
    manifest = load_forms_template_capability_manifest()
    mutated = copy.deepcopy(manifest)
    templates = cast(list[dict[str, object]], mutated["templates"])
    templates[0].pop("notes")

    with pytest.raises(FormsTemplateRegistryError) as error_info:
        validate_forms_template_capability_manifest(mutated)

    assert error_info.value.reason == "forms_template_capability_missing_required_field"


def test_forms_template_registry_load_is_deterministic_for_same_manifest_content() -> None:
    first = load_forms_template_capabilities()
    second = load_forms_template_capabilities()

    assert canonical_json_dumps(first) == canonical_json_dumps(second)


def test_forms_template_registry_index_lookup_is_deterministic() -> None:
    capability_index = build_forms_template_capability_index()
    first = get_forms_template_capability_entry("it2", capability_index=capability_index)
    second = get_forms_template_capability_entry("IT2", capability_index=capability_index)
    unknown = get_forms_template_capability_entry("UNKNOWN", capability_index=capability_index)

    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["template_code"] == "IT2"
    assert second["template_code"] == "IT2"
    assert first["status"] == "disabled"
    assert canonical_json_dumps(first) == canonical_json_dumps(second)
    assert unknown is None
