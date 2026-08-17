"""Deterministically assemble validated understanding into canonical content.

This is intentionally a provider-result consumer, rather than another model
call.  Its output is stable across retries and retains the source observation
alongside the platform-normalized value.
"""

from __future__ import annotations

import re
from uuid import UUID
from typing import cast
from hashlib import sha256
from dataclasses import field
from dataclasses import dataclass
import unicodedata
from collections.abc import Mapping

from shared.determinism.input_hash import compute_canonical_hash

CANONICAL_SCHEMA_VERSION = "v1"
CANONICAL_ASSEMBLY_POLICY_VERSION = "v1"

_TYPE_MAP = {
    "form": "form_field",
    "handwriting": "handwritten_note",
    "amount": "money",
    "list_item": "list_item",
    "heading": "heading",
    "paragraph": "paragraph",
    "section": "section",
    "list": "list",
    "table": "table",
    "image": "image",
    "chart": "chart",
    "caption": "caption",
    "header": "header",
    "footer": "footer",
    "footnote": "footnote",
    "annotation": "annotation",
    "identifier": "identifier",
    "date": "date",
    "relationship": "relationship",
    "unknown": "unknown",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CanonicalAssemblyError(ValueError):
    """A provider artifact cannot form a complete canonical graph."""


@dataclass(frozen=True)
class CanonicalElement:
    stable_key: str
    element_type: str
    page_number: int
    reading_order: int
    observed_value: dict[str, object] | None
    normalized_value: dict[str, object] | None
    uncertainty: dict[str, object]
    source_region: dict[str, object]
    lineage: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalGraph:
    content_hash: str
    payload: dict[str, object]
    elements: tuple[CanonicalElement, ...]
    source_lineage: dict[str, object] = field(default_factory=dict)


def normalize_text(value: str) -> str:
    """Apply the documented deterministic Unicode and whitespace policy."""

    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARACTERS.sub("", text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()


def assemble_canonical_graph(
    *,
    provider_result_id: UUID | str,
    source_artifact_id: UUID | str,
    validated_result: Mapping[str, object],
    source_lineage: Mapping[str, object] | None = None,
    element_lineage_by_observation_id: Mapping[str, Mapping[str, object]] | None = None,
) -> CanonicalGraph:
    """Transform one validated OpenAI result without provider-shaped leakage."""

    result = validated_result.get("result")
    if not isinstance(result, Mapping):
        raise CanonicalAssemblyError("canonical_assembly_invalid_provider_result")
    result_map = cast(Mapping[str, object], result)
    if result_map.get("schema_version") != "v1":
        raise CanonicalAssemblyError("canonical_assembly_invalid_provider_result")
    pages = result_map.get("pages")
    warnings = result_map.get("warnings")
    if not isinstance(pages, list) or not isinstance(warnings, list):
        raise CanonicalAssemblyError("canonical_assembly_invalid_provider_result")
    page_values = cast(list[object], pages)
    warning_values = cast(list[object], warnings)
    seen_pages: set[int] = set()
    elements: list[CanonicalElement] = []
    for page in sorted(page_values, key=_page_number):
        page_number = _page_number(page)
        if page_number in seen_pages:
            raise CanonicalAssemblyError("canonical_assembly_duplicate_page")
        seen_pages.add(page_number)
        page_map = cast(Mapping[str, object], page)
        observations = page_map.get("observations")
        if not isinstance(observations, list):
            raise CanonicalAssemblyError("canonical_assembly_invalid_page")
        observation_values = cast(list[object], observations)
        seen_observations: set[str] = set()
        ordered = sorted(
            observation_values, key=lambda item: (_observation_order(item), _observation_id(item))
        )
        for reading_order, observation in enumerate(ordered):
            observation_id = _observation_id(observation)
            if observation_id in seen_observations:
                raise CanonicalAssemblyError("canonical_assembly_duplicate_observation")
            seen_observations.add(observation_id)
            if not isinstance(observation, Mapping):
                raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
            observation_map = cast(Mapping[str, object], observation)
            kind = observation_map.get("kind")
            state = observation_map.get("state")
            raw_text = observation_map.get("text")
            if not isinstance(kind, str) or kind not in _TYPE_MAP or not isinstance(state, str):
                raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
            if raw_text is not None and not isinstance(raw_text, str):
                raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
            location = observation_map.get("source_location")
            if location is not None and not isinstance(location, Mapping):
                raise CanonicalAssemblyError("canonical_assembly_invalid_source_location")
            source_region = _source_region(
                page_number=page_number,
                location=None if location is None else cast(Mapping[str, object], location),
            )
            lineage = {}
            if element_lineage_by_observation_id is not None:
                lineage = dict(element_lineage_by_observation_id.get(observation_id, {}))
            observed: dict[str, object] | None = None if raw_text is None else {"text": raw_text}
            normalized: dict[str, object] | None = (
                None if raw_text is None else {"text": normalize_text(raw_text)}
            )
            stable_key = sha256(
                f"{source_artifact_id}:{provider_result_id}:{page_number}:{observation_id}".encode()
            ).hexdigest()
            elements.append(
                CanonicalElement(
                    stable_key=stable_key,
                    element_type=_TYPE_MAP[kind],
                    page_number=page_number,
                    reading_order=reading_order,
                    observed_value=observed,
                    normalized_value=normalized,
                    uncertainty={"state": state},
                    source_region=source_region,
                    lineage=lineage,
                )
            )
    payload: dict[str, object] = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "assembly_policy_version": CANONICAL_ASSEMBLY_POLICY_VERSION,
        "structural_units": [{"kind": "page", "page_number": page} for page in sorted(seen_pages)],
        "warnings": [
            normalize_text(warning) for warning in warning_values if isinstance(warning, str)
        ],
        "element_count": len(elements),
    }
    if source_lineage is not None:
        payload["source_lineage"] = dict(source_lineage)
    hash_elements = [
        {
            "stable_key": item.stable_key,
            "element_type": item.element_type,
            "page_number": item.page_number,
            "reading_order": item.reading_order,
            "observed_value": item.observed_value,
            "normalized_value": item.normalized_value,
            "uncertainty": item.uncertainty,
            "source_region": item.source_region,
            **({"lineage": item.lineage} if item.lineage else {}),
        }
        for item in elements
    ]
    graph_source_lineage = dict(source_lineage or {})
    return CanonicalGraph(
        content_hash=compute_canonical_hash(
            {"payload": payload, "elements": hash_elements}
        ).sha256_hex,
        payload=payload,
        elements=tuple(elements),
        source_lineage=graph_source_lineage,
    )


def _page_number(value: object) -> int:
    if not isinstance(value, Mapping):
        raise CanonicalAssemblyError("canonical_assembly_invalid_page")
    page = cast(Mapping[str, object], value).get("page_number")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise CanonicalAssemblyError("canonical_assembly_invalid_page")
    return page


def _observation_order(value: object) -> int:
    if not isinstance(value, Mapping):
        raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
    order = cast(Mapping[str, object], value).get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
    return order


def _observation_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
    observation_id = cast(Mapping[str, object], value).get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise CanonicalAssemblyError("canonical_assembly_invalid_observation")
    return observation_id


def _source_region(*, page_number: int, location: Mapping[str, object] | None) -> dict[str, object]:
    # A page is always source provenance even when the provider cannot identify a box/span.
    region: dict[str, object] = {"page_number": page_number}
    if location is not None:
        for key in ("bounding_box", "start_offset", "end_offset"):
            if location.get(key) is not None:
                region[key] = location[key]
    return region
