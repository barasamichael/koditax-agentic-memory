"""Manage deterministic runtime feature flags and kill-switch controls for pilot safety."""

from __future__ import annotations

import json
from typing import cast
from typing import TypedDict
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_CAPABILITY_KEYS: tuple[str, ...] = (
    "resident_employment_income_2021_01_01",
    "non_resident_employment_income_2021_01_01",
    "resident_employment_income_2023_07_01",
    "non_resident_employment_income_2023_07_01",
    "resident_employment_plus_qualifying_interest_2023_07_01",
)
SUPPORTED_ACTION_KEYS: tuple[str, ...] = (
    "read_only_review",
    "submission_execute",
)
SUPPORTED_ORCHESTRATION_FEATURE_KEYS: tuple[str, ...] = (
    "response_synthesis",
    "conversation_continuity",
    "compute_plus_grounding_execution",
    "grounded_legal_basis_synthesis",
    "artifact_detail_followup_reuse",
)
SUPPORTED_ORCHESTRATION_ROLLOUT_STATES: tuple[str, ...] = (
    "canary",
    "general_availability",
    "blocked",
)
SUPPORTED_ORCHESTRATION_RELEASE_GATE_STATES: tuple[str, ...] = (
    "go",
    "go_degraded_safe",
    "no_go",
)


class RuntimeSafetyControlConfig(TypedDict):
    """Represent deterministic runtime safety-control configuration."""

    config_version: str
    default_fail_closed: bool
    capability_flags: dict[str, bool]
    action_flags: dict[str, bool]
    orchestration_flags: dict[str, bool]
    kill_switches: dict[str, bool]


def get_runtime_safety_control_config() -> RuntimeSafetyControlConfig:
    """Return current deterministic runtime safety-control configuration snapshot."""

    return _clone_config(cast(Mapping[str, object], _runtime_safety_control_config))


def reset_runtime_safety_control_config() -> None:
    """Reset runtime safety-control configuration to default governed safe state."""

    _runtime_safety_control_config.clear()
    _runtime_safety_control_config.update(_clone_config(_DEFAULT_RUNTIME_SAFETY_CONTROL_CONFIG))


def set_runtime_safety_control_config(config: Mapping[str, object]) -> None:
    """Replace runtime safety-control configuration with deterministic validated payload."""

    parsed = _parse_runtime_safety_control_config(config)
    _runtime_safety_control_config.clear()
    _runtime_safety_control_config.update(_clone_config(parsed))


def set_capability_flag(*, capability_key: str, enabled: bool) -> None:
    """Set one capability-level runtime feature flag deterministically."""

    capability_flags = cast(dict[str, bool], _runtime_safety_control_config["capability_flags"])
    capability_flags[capability_key] = enabled


def set_action_flag(*, action_key: str, enabled: bool) -> None:
    """Set one action-level runtime feature flag deterministically."""

    action_flags = cast(dict[str, bool], _runtime_safety_control_config["action_flags"])
    action_flags[action_key] = enabled


def set_orchestration_flag(*, feature_key: str, enabled: bool) -> None:
    """Set one orchestration-level runtime feature flag deterministically."""

    orchestration_flags = cast(
        dict[str, bool],
        _runtime_safety_control_config["orchestration_flags"],
    )
    orchestration_flags[feature_key] = enabled


def set_kill_switch(*, switch_key: str, enabled: bool) -> None:
    """Set one kill-switch runtime control deterministically."""

    kill_switches = cast(dict[str, bool], _runtime_safety_control_config["kill_switches"])
    kill_switches[switch_key] = enabled


def _parse_runtime_safety_control_config(
    config: Mapping[str, object],
) -> RuntimeSafetyControlConfig:
    config_version = _require_string(config, "config_version")
    default_fail_closed = _require_bool(config, "default_fail_closed")
    capability_flags = _require_bool_mapping(config, "capability_flags")
    action_flags = _require_bool_mapping(config, "action_flags")
    orchestration_flags = _require_bool_mapping(config, "orchestration_flags")
    kill_switches = _require_bool_mapping(config, "kill_switches")
    return {
        "config_version": config_version,
        "default_fail_closed": default_fail_closed,
        "capability_flags": capability_flags,
        "action_flags": action_flags,
        "orchestration_flags": orchestration_flags,
        "kill_switches": kill_switches,
    }


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(f"runtime_safety_control_config.{field_name} must be non-empty string")


def _require_bool(source: Mapping[str, object], field_name: str) -> bool:
    value = source.get(field_name)
    if isinstance(value, bool):
        return value
    raise ValueError(f"runtime_safety_control_config.{field_name} must be boolean")


def _require_bool_mapping(source: Mapping[str, object], field_name: str) -> dict[str, bool]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime_safety_control_config.{field_name} must be object mapping")
    typed_value = cast(Mapping[str, object], value)
    parsed: dict[str, bool] = {}
    for key, raw_value in typed_value.items():
        if not isinstance(raw_value, bool):
            raise ValueError(f"runtime_safety_control_config.{field_name}['{key}'] must be boolean")
        parsed[key] = raw_value
    return parsed


def _clone_config(config: Mapping[str, object]) -> RuntimeSafetyControlConfig:
    return cast(RuntimeSafetyControlConfig, json.loads(canonical_json_dumps(config)))


def _build_default_runtime_safety_control_config() -> RuntimeSafetyControlConfig:
    capability_flags = {capability_key: True for capability_key in SUPPORTED_CAPABILITY_KEYS}
    action_flags = {action_key: True for action_key in SUPPORTED_ACTION_KEYS}
    orchestration_flags = {
        feature_key: True for feature_key in SUPPORTED_ORCHESTRATION_FEATURE_KEYS
    }
    kill_switches: dict[str, bool] = {
        "global_capability": False,
        "global_action": False,
        "global_orchestration": False,
    }
    for capability_key in SUPPORTED_CAPABILITY_KEYS:
        kill_switches[f"capability:{capability_key}"] = False
    for action_key in SUPPORTED_ACTION_KEYS:
        kill_switches[f"action:{action_key}"] = False
    for feature_key in SUPPORTED_ORCHESTRATION_FEATURE_KEYS:
        kill_switches[f"orchestration:{feature_key}"] = False
    return {
        "config_version": "1.0.0",
        "default_fail_closed": True,
        "capability_flags": capability_flags,
        "action_flags": action_flags,
        "orchestration_flags": orchestration_flags,
        "kill_switches": kill_switches,
    }


_DEFAULT_RUNTIME_SAFETY_CONTROL_CONFIG = _build_default_runtime_safety_control_config()
_runtime_safety_control_config: dict[str, object] = cast(
    dict[str, object], _clone_config(_DEFAULT_RUNTIME_SAFETY_CONTROL_CONFIG)
)
