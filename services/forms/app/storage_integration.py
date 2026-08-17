"""Deterministic governed storage integration for forms artifact outputs."""

from __future__ import annotations

from typing import cast
from threading import Lock
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps


class FormsStorageIntegrationError(RuntimeError):
    """Represent deterministic storage integration failures."""

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
        """Return stable structured storage error details."""

        return {"reason": self.reason, **self._details}


_STORE_LOCK = Lock()
_failure_mode_enabled = False
_failure_mode_reason = "storage_backend_unavailable"
_STORAGE_METADATA_BY_OBJECT_ID: dict[str, dict[str, object]] = {}


def persist_form_artifact_in_governed_storage(
    *,
    artifact_id: str,
    artifact_hash: str,
    form_type: str,
    artifact_payload: Mapping[str, object],
) -> dict[str, object]:
    """Persist one immutable form artifact payload to governed storage."""

    normalized_artifact_id = artifact_id.strip().lower()
    normalized_artifact_hash = artifact_hash.strip().lower()
    normalized_form_type = form_type.strip()

    if not normalized_artifact_id or not normalized_artifact_hash:
        raise FormsStorageIntegrationError(
            reason="forms_storage_reference_missing",
            message="Forms storage reference is missing required identifiers.",
            details={
                "artifact_id": normalized_artifact_id,
                "artifact_hash": normalized_artifact_hash,
            },
        )
    if normalized_form_type != "income_tax_return":
        raise FormsStorageIntegrationError(
            reason="forms_scope_not_supported",
            message="Requested forms scope is not supported by storage integration.",
            details={"form_type": normalized_form_type},
        )
    payload_json = canonical_json_dumps(_as_object(artifact_payload))
    payload_size_bytes = len(payload_json.encode("utf-8"))
    storage_object_id = f"forms/{normalized_form_type}/artifacts/{normalized_artifact_id}.json"
    storage_metadata = {
        "storage_object_id": storage_object_id,
        "storage_backend": "forms_governed_storage_inmemory",
        "content_type": "application/json",
        "size_bytes": payload_size_bytes,
        "artifact_hash": normalized_artifact_hash,
    }

    with _STORE_LOCK:
        if _failure_mode_enabled:
            raise FormsStorageIntegrationError(
                reason="forms_storage_write_failed",
                message="Forms storage write failed.",
                details={"failure_mode": _failure_mode_reason},
            )

        existing_metadata = _STORAGE_METADATA_BY_OBJECT_ID.get(storage_object_id)
        if existing_metadata is not None:
            if existing_metadata.get("artifact_hash") != normalized_artifact_hash:
                raise FormsStorageIntegrationError(
                    reason="forms_storage_write_failed",
                    message="Forms storage write failed due to deterministic conflict.",
                    details={
                        "storage_object_id": storage_object_id,
                        "existing_artifact_hash": existing_metadata.get("artifact_hash"),
                        "incoming_artifact_hash": normalized_artifact_hash,
                    },
                )
            return _copy_storage_metadata(existing_metadata)

        _STORAGE_METADATA_BY_OBJECT_ID[storage_object_id] = dict(storage_metadata)
        return _copy_storage_metadata(storage_metadata)


def reset_forms_storage_integration_state() -> None:
    """Reset in-memory forms storage integration state for deterministic tests."""

    global _failure_mode_enabled
    global _failure_mode_reason
    with _STORE_LOCK:
        _STORAGE_METADATA_BY_OBJECT_ID.clear()
        _failure_mode_enabled = False
        _failure_mode_reason = "storage_backend_unavailable"


def set_forms_storage_integration_failure_mode(
    *,
    enabled: bool,
    reason: str = "storage_backend_unavailable",
) -> None:
    """Enable deterministic storage-write failure mode for tests."""

    normalized_reason = reason.strip() or "storage_backend_unavailable"
    global _failure_mode_enabled
    global _failure_mode_reason
    with _STORE_LOCK:
        _failure_mode_enabled = enabled
        _failure_mode_reason = normalized_reason


def _as_object(value: Mapping[str, object]) -> dict[str, object]:
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _copy_storage_metadata(storage_metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        "storage_object_id": storage_metadata["storage_object_id"],
        "storage_backend": storage_metadata["storage_backend"],
        "content_type": storage_metadata["content_type"],
        "size_bytes": storage_metadata["size_bytes"],
        "artifact_hash": storage_metadata["artifact_hash"],
    }
