"""Environment-controlled structured debug tracing for orchestration runtime."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportGeneralTypeIssues=false, reportRedeclaration=false
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
_DEBUG_ENV_FLAG = "ORCHESTRATION_DEBUG_TRACE"
_MAX_TEXT_LENGTH = 400
_MAX_SEQUENCE_ITEMS = 8
_MAX_MAPPING_ITEMS = 24


def orchestration_debug_enabled() -> bool:
    """Return whether structured orchestration debug tracing is enabled."""

    return os.getenv(_DEBUG_ENV_FLAG) == "1"


def bounded_preview(value: object, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    """Return one bounded text preview for safe diagnostics."""

    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def emit_orchestration_debug(channel: str, event: str, **payload: object) -> None:
    """Emit one structured debug line when debug tracing is enabled."""

    if not orchestration_debug_enabled():
        return
    body = {"event": event, **{key: _sanitize(value) for key, value in payload.items()}}
    print(
        f"[ORCH_DEBUG][{channel}] {json.dumps(body, default=str, sort_keys=True)}",
        flush=True,
    )


def _sanitize(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return bounded_preview(value)
        return value
    if isinstance(value, Mapping):
        mapping_items: list[tuple[object, object]] = list(value.items())[:_MAX_MAPPING_ITEMS]
        return {str(key): _sanitize(item) for key, item in mapping_items}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items: list[object] = list(value)[:_MAX_SEQUENCE_ITEMS]
        return [_sanitize(item) for item in items]
    return bounded_preview(value)
