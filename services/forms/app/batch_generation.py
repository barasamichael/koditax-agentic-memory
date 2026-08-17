"""Deterministic helpers for forms batch-generation orchestration responses."""

from __future__ import annotations

from hashlib import sha256
from collections.abc import Mapping
from typing import cast

from shared.determinism.input_hash import canonical_json_dumps


def build_forms_batch_id(*, items: list[dict[str, object]]) -> str:
    """Build deterministic batch identifier from canonicalized input items."""

    canonical_items = canonical_json_dumps(items)
    return sha256(f"forms-batch:{canonical_items}".encode()).hexdigest()


def build_forms_batch_summary(
    *,
    results: list[dict[str, object]],
) -> dict[str, int]:
    """Build deterministic summary counts from ordered batch results."""

    total = len(results)
    succeeded = sum(
        1
        for result in results
        if isinstance(result.get("status"), str) and result.get("status") == "succeeded"
    )
    failed = total - succeeded
    return {"total": total, "succeeded": succeeded, "failed": failed}


def build_canonical_batch_item_error(
    *,
    error_code: str,
    message: str,
    reason: str,
) -> dict[str, str]:
    """Build deterministic per-item canonical error payload."""

    normalized_error_code = error_code.strip() or "forms_contract_violation"
    normalized_reason = reason.strip() or "forms_contract_violation"
    normalized_message = message.strip() or "Forms batch item failed."
    return {
        "error_code": normalized_error_code,
        "message": normalized_message,
        "reason": normalized_reason,
    }


def extract_batch_item_error_from_http_exception_detail(
    detail: object,
) -> dict[str, str]:
    """Extract deterministic per-item canonical error fields from HTTPException detail."""

    if isinstance(detail, Mapping):
        typed_detail = cast(dict[str, object], detail)
        error_code = typed_detail.get("error_code")
        message = typed_detail.get("message")
        reason = typed_detail.get("reason")
        if (
            isinstance(error_code, str)
            and error_code.strip()
            and isinstance(message, str)
            and message.strip()
            and isinstance(reason, str)
            and reason.strip()
        ):
            return build_canonical_batch_item_error(
                error_code=error_code,
                message=message,
                reason=reason,
            )
    return build_canonical_batch_item_error(
        error_code="forms_artifact_generation_failed",
        message="Forms artifact generation failed.",
        reason="forms_artifact_generation_failed",
    )
