"""Load and validate deterministic forms template capability registry entries."""

from __future__ import annotations

import json
from typing import cast
from typing import Final
from pathlib import Path

FORMS_TEMPLATE_CAPABILITY_MANIFEST_PATH: Final[tuple[str, ...]] = (
    "contracts",
    "capabilities",
    "forms_template_capability_manifest.json",
)
REQUIRED_DISABLED_TEMPLATE_CODES: Final[frozenset[str]] = frozenset({"IT2", "VAT3", "P10", "P9"})
REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS: Final[tuple[str, ...]] = (
    "tax_engine_rule_pack_ready",
    "openapi_contract_ready",
    "validation_rules_ready",
    "test_coverage_ready",
    "audit_event_taxonomy_ready",
)
ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES: Final[str] = "blocked_by_prerequisites"
ENABLEMENT_STATUS_PREREQUISITES_SATISFIED: Final[str] = "prerequisites_satisfied"


class FormsTemplateRegistryError(RuntimeError):
    """Represent deterministic forms template-registry validation failures."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured error details."""

        return {"reason": self.reason, **self._details}


def normalize_template_code(template_code: object) -> str | None:
    """Normalize one template code to deterministic uppercase registry key."""

    if not isinstance(template_code, str):
        return None
    normalized = template_code.strip().upper()
    return normalized if normalized else None


def load_forms_template_capability_manifest(repo_root: Path | None = None) -> dict[str, object]:
    """Load and validate the forms extension-template capability manifest."""

    target_root = repo_root if repo_root is not None else Path.cwd()
    manifest_path = target_root.joinpath(*FORMS_TEMPLATE_CAPABILITY_MANIFEST_PATH)
    if not manifest_path.exists():
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_manifest_missing",
            message="Forms template capability manifest file is missing.",
            details={"path": str(manifest_path)},
        )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_manifest_invalid_json",
            message="Forms template capability manifest JSON is invalid.",
            details={"line": error.lineno, "column": error.colno},
        ) from error

    return validate_forms_template_capability_manifest(raw)


def load_forms_template_capabilities(
    repo_root: Path | None = None,
) -> tuple[dict[str, object], ...]:
    """Load template capability entries in deterministic template-code order."""

    manifest = load_forms_template_capability_manifest(repo_root=repo_root)
    templates = manifest.get("templates")
    if not isinstance(templates, list):
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_manifest_missing_templates",
            message="Forms template capability manifest requires templates list.",
        )
    normalized_templates = cast(list[dict[str, object]], templates)
    return tuple(dict(template) for template in normalized_templates)


def build_forms_template_capability_index(
    repo_root: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Build deterministic template capability lookup index by template code."""

    capabilities = load_forms_template_capabilities(repo_root=repo_root)
    return {cast(str, capability["template_code"]): dict(capability) for capability in capabilities}


def get_forms_template_capability_entry(
    template_code: object,
    *,
    capability_index: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    """Resolve one template capability entry by normalized template code."""

    normalized_template_code = normalize_template_code(template_code)
    if normalized_template_code is None:
        return None
    capability = capability_index.get(normalized_template_code)
    return dict(capability) if isinstance(capability, dict) else None


def derive_forms_template_enablement_status(
    prerequisite_state: dict[str, bool],
) -> str:
    """Derive deterministic enablement status from prerequisite readiness state."""

    if all(prerequisite_state[field] for field in REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS):
        return ENABLEMENT_STATUS_PREREQUISITES_SATISFIED
    return ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES


def evaluate_forms_template_enablement_status(
    capability_entry: dict[str, object],
) -> str:
    """Evaluate deterministic enablement status from one capability entry."""

    prerequisite_state = _require_enablement_prerequisites(
        capability_entry,
        template_code=capability_entry.get("template_code"),
    )
    return derive_forms_template_enablement_status(prerequisite_state)


def validate_forms_template_capability_manifest(manifest: object) -> dict[str, object]:
    """Validate and normalize one forms template capability manifest object."""

    if not isinstance(manifest, dict):
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_manifest_invalid_shape",
            message="Forms template capability manifest must be a JSON object.",
        )
    raw_manifest = cast(dict[str, object], manifest)
    normalized_manifest: dict[str, object] = {
        "manifest_version": _require_string(raw_manifest, "manifest_version"),
        "capability_scope": _require_string(raw_manifest, "capability_scope"),
        "generated_at": _require_string(raw_manifest, "generated_at"),
    }

    supported_runtime_scope = _require_list_of_strings(raw_manifest, "supported_runtime_scope")
    normalized_manifest["supported_runtime_scope"] = list(supported_runtime_scope)
    normalized_manifest["notes"] = _require_string(raw_manifest, "notes")

    raw_templates = raw_manifest.get("templates")
    if not isinstance(raw_templates, list):
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_manifest_missing_templates",
            message="Forms template capability manifest requires templates list.",
            details={"field_name": "templates"},
        )

    normalized_templates: list[dict[str, object]] = []
    seen_template_codes: set[str] = set()
    for item in cast(list[object], raw_templates):
        if not isinstance(item, dict):
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_entry_invalid_shape",
                message="Each forms template capability entry must be a JSON object.",
            )
        entry = cast(dict[str, object], item)
        template_code = _require_string(entry, "template_code").upper()
        if template_code in seen_template_codes:
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_duplicate_entry",
                message="Forms template capability manifest contains duplicate template entry.",
                details={"template_code": template_code},
            )
        seen_template_codes.add(template_code)

        status = _require_string(entry, "status")
        if status not in {"disabled", "enabled"}:
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_invalid_status",
                message="Forms template capability status must be disabled or enabled.",
                details={"template_code": template_code, "status": status},
            )
        prerequisite_state = _require_enablement_prerequisites(
            entry,
            template_code=template_code,
        )
        derived_enablement_status = derive_forms_template_enablement_status(prerequisite_state)
        declared_enablement_status = _require_string(entry, "enablement_status")
        if declared_enablement_status not in {
            ENABLEMENT_STATUS_BLOCKED_BY_PREREQUISITES,
            ENABLEMENT_STATUS_PREREQUISITES_SATISFIED,
        }:
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_invalid_enablement_status",
                message=(
                    "Forms template capability enablement_status must be "
                    "blocked_by_prerequisites or prerequisites_satisfied."
                ),
                details={
                    "template_code": template_code,
                    "enablement_status": declared_enablement_status,
                },
            )
        if declared_enablement_status != derived_enablement_status:
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_enablement_status_mismatch",
                message=(
                    "Forms template capability enablement_status must match derived "
                    "prerequisite readiness decision."
                ),
                details={
                    "template_code": template_code,
                    "declared_enablement_status": declared_enablement_status,
                    "derived_enablement_status": derived_enablement_status,
                },
            )
        if status == "enabled" and derived_enablement_status != (
            ENABLEMENT_STATUS_PREREQUISITES_SATISFIED
        ):
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_enablement_blocked_by_prerequisites",
                message=(
                    "Forms template capability cannot be enabled while prerequisite "
                    "readiness is unresolved."
                ),
                details={
                    "template_code": template_code,
                    "enablement_status": derived_enablement_status,
                },
            )
        if template_code in REQUIRED_DISABLED_TEMPLATE_CODES and status != "disabled":
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_must_be_disabled",
                message="Required extension templates must remain disabled by default.",
                details={"template_code": template_code, "status": status},
            )

        normalized_templates.append(
            {
                "template_code": template_code,
                "status": status,
                "enablement_status": derived_enablement_status,
                "domain": _require_string(entry, "domain"),
                "effective_scope": _require_string(entry, "effective_scope"),
                "enablement_prerequisites": prerequisite_state,
                "notes": _require_string(entry, "notes"),
            }
        )

    missing_templates = REQUIRED_DISABLED_TEMPLATE_CODES - seen_template_codes
    if missing_templates:
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_required_entries_missing",
            message="Forms template capability manifest is missing required template entries.",
            details={"missing_templates": sorted(missing_templates)},
        )

    normalized_templates.sort(key=lambda item: cast(str, item["template_code"]))
    normalized_manifest["templates"] = normalized_templates
    return normalized_manifest


def _require_string(source: dict[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_missing_required_field",
            message=(
                f"Forms template capability field '{field_name}' is required and must be string."
            ),
            details={"field_name": field_name},
        )
    return value


def _require_list_of_strings(source: dict[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_missing_required_field",
            message=f"Forms template capability field '{field_name}' is required and must be list.",
            details={"field_name": field_name},
        )

    items: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item.strip():
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_invalid_list_item",
                message=(
                    f"Forms template capability field '{field_name}' must contain strings only."
                ),
                details={"field_name": field_name},
            )
        items.append(item)
    if not items:
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_empty_list",
            message=f"Forms template capability field '{field_name}' must be non-empty.",
            details={"field_name": field_name},
        )
    return items


def _require_enablement_prerequisites(
    source: dict[str, object],
    *,
    template_code: object,
) -> dict[str, bool]:
    raw_value = source.get("enablement_prerequisites")
    if not isinstance(raw_value, dict):
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_missing_required_field",
            message=(
                "Forms template capability field 'enablement_prerequisites' is "
                "required and must be object."
            ),
            details={"field_name": "enablement_prerequisites", "template_code": template_code},
        )

    raw_prerequisites = cast(dict[str, object], raw_value)
    unknown_fields = sorted(
        field_name
        for field_name in raw_prerequisites
        if field_name not in REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS
    )
    if unknown_fields:
        raise FormsTemplateRegistryError(
            reason="forms_template_capability_unknown_prerequisite_field",
            message="Forms template capability contains unknown prerequisite field.",
            details={"template_code": template_code, "unknown_fields": unknown_fields},
        )

    normalized_prerequisites: dict[str, bool] = {}
    for field_name in REQUIRED_ENABLEMENT_PREREQUISITE_FIELDS:
        if field_name not in raw_prerequisites:
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_missing_required_field",
                message=(
                    "Forms template capability prerequisite field is required and must be boolean."
                ),
                details={
                    "field_name": f"enablement_prerequisites.{field_name}",
                    "template_code": template_code,
                },
            )
        field_value = raw_prerequisites[field_name]
        if not isinstance(field_value, bool):
            raise FormsTemplateRegistryError(
                reason="forms_template_capability_invalid_prerequisite_value",
                message="Forms template capability prerequisite value must be boolean.",
                details={
                    "field_name": f"enablement_prerequisites.{field_name}",
                    "template_code": template_code,
                },
            )
        normalized_prerequisites[field_name] = field_value

    return normalized_prerequisites
