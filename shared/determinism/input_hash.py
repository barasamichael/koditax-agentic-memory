"""Canonical deterministic serialization and hashing for governed input envelopes."""

from __future__ import annotations

import json
import math
from typing import cast
import hashlib
from dataclasses import dataclass
from collections.abc import Mapping


class InputHashError(ValueError):
    """Represent deterministic canonicalization and hashing failures."""

    def __init__(self, reason: str, message: str, path: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.path = path

    def details(self) -> dict[str, object]:
        """Build deterministic error details for API mapping."""

        return {
            "reason": self.reason,
            "path": self.path,
        }


@dataclass(frozen=True)
class CanonicalHashResult:
    """Represent canonical value, canonical JSON, and SHA-256 digest."""

    canonical_value: object
    canonical_json: str
    sha256_hex: str


def build_computation_hash_envelope(
    tax_type: str,
    regime_type: str,
    regime_identifier: str | None,
    tax_year: int,
    rule_version: str,
    input_payload: object,
) -> dict[str, object]:
    """Build the governed computation hash envelope."""

    return {
        "tax_type": tax_type,
        "regime_type": regime_type,
        "regime_identifier": regime_identifier,
        "tax_year": tax_year,
        "rule_version": rule_version,
        "input_payload": input_payload,
    }


def compute_computation_input_hash(
    tax_type: str,
    regime_type: str,
    regime_identifier: str | None,
    tax_year: int,
    rule_version: str,
    input_payload: object,
) -> CanonicalHashResult:
    """Compute canonical deterministic hash over the governed request envelope."""

    envelope = build_computation_hash_envelope(
        tax_type=tax_type,
        regime_type=regime_type,
        regime_identifier=regime_identifier,
        tax_year=tax_year,
        rule_version=rule_version,
        input_payload=input_payload,
    )
    return compute_canonical_hash(envelope)


def compute_canonical_hash(value: object) -> CanonicalHashResult:
    """Compute canonical SHA-256 hash for a JSON-safe value."""

    canonical_value = canonicalize_for_hash(value)
    canonical_json = canonical_json_dumps(canonical_value)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return CanonicalHashResult(
        canonical_value=canonical_value,
        canonical_json=canonical_json,
        sha256_hex=digest,
    )


def canonicalize_for_hash(value: object, path: str = "$") -> object:
    """Canonicalize value into JSON-safe deterministic structure."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputHashError(
                reason="non_finite_number",
                message="Non-finite numeric values are not supported for canonical hashing.",
                path=path,
            )
        return value
    if isinstance(value, str):
        return value

    if isinstance(value, Mapping):
        mapping_value = cast(Mapping[object, object], value)
        canonical_mapping: dict[str, object] = {}
        seen_keys: set[str] = set()
        for key in sorted(mapping_value.keys(), key=lambda item: str(item)):
            key_str = str(key)
            if key_str in seen_keys:
                raise InputHashError(
                    reason="duplicate_stringified_key",
                    message="Duplicate mapping keys after string conversion are not supported.",
                    path=path,
                )
            seen_keys.add(key_str)
            child_path = f"{path}.{key_str}"
            canonical_mapping[key_str] = canonicalize_for_hash(
                mapping_value[key],
                path=child_path,
            )
        return canonical_mapping

    if isinstance(value, list):
        value_list = cast(list[object], value)
        return [
            canonicalize_for_hash(item, path=f"{path}[{index}]")
            for index, item in enumerate(value_list)
        ]

    if isinstance(value, tuple):
        value_tuple = cast(tuple[object, ...], value)
        return [
            canonicalize_for_hash(item, path=f"{path}[{index}]")
            for index, item in enumerate(value_tuple)
        ]

    raise InputHashError(
        reason="unsupported_value_type",
        message=f"Unsupported value type for canonical hashing: {type(value).__name__}.",
        path=path,
    )


def canonical_json_dumps(value: object) -> str:
    """Serialize value to fixed canonical JSON."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise InputHashError(
            reason="non_json_serializable",
            message="Value is not JSON-serializable under canonical hashing rules.",
            path="$",
        ) from error
