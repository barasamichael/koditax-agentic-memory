"""Content-aware, deterministic retrieval chunks for validated canonical content."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Iterable
from collections.abc import Mapping

from shared.determinism.input_hash import compute_canonical_hash

from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_assembly import normalize_text

CANONICAL_CHUNKING_POLICY_VERSION = "v2"
CANONICAL_CHUNKING_MAX_CHARACTERS = 3_500
CANONICAL_CHUNKING_MAX_ELEMENTS = 32


@dataclass(frozen=True)
class RetrievalChunk:
    """One traceable retrieval unit; it is never canonical evidence itself."""

    chunk_key: str
    chunk_ordinal: int
    generation_identity: str
    content_hash: str
    embedding_text: str
    canonical_element_keys: tuple[str, ...]
    source_location: dict[str, object]
    source_lineage: dict[str, object]
    structural_context: dict[str, object]
    chunking_policy_version: str = CANONICAL_CHUNKING_POLICY_VERSION


def build_retrieval_chunks(
    *,
    elements: Iterable[CanonicalElement],
    chunking_policy_version: str = CANONICAL_CHUNKING_POLICY_VERSION,
    source_lineage: Mapping[str, object] | None = None,
    max_chunk_characters: int = CANONICAL_CHUNKING_MAX_CHARACTERS,
    max_chunk_elements: int = CANONICAL_CHUNKING_MAX_ELEMENTS,
) -> tuple[RetrievalChunk, ...]:
    """Build deterministic, bounded retrieval chunks from canonical elements.

    The builder preserves the canonical ordering established upstream, keeps
    compatible content together, and carries source provenance forward in the
    chunk payload so the durable chunk store can be replayed exactly.
    """

    normalized_policy = chunking_policy_version.strip()
    if not normalized_policy:
        raise ValueError("chunking_policy_version_required")
    if max_chunk_characters < 1:
        raise ValueError("chunk_max_characters_required")
    if max_chunk_elements < 1:
        raise ValueError("chunk_max_elements_required")

    lineage = dict(source_lineage or {})
    ordered_elements = tuple(
        sorted(elements, key=lambda item: (item.page_number, item.reading_order, item.stable_key))
    )
    current_headings: dict[int, str] = {}
    chunks: list[RetrievalChunk] = []
    current_batch: list[CanonicalElement] = []
    current_text_parts: list[str] = []
    current_page_number: int | None = None
    current_content_kind: str | None = None
    current_source_partition_key: str | None = None

    def finalize_batch() -> None:
        nonlocal current_batch
        nonlocal current_text_parts
        nonlocal current_page_number
        nonlocal current_content_kind
        nonlocal current_source_partition_key

        if not current_batch:
            return
        chunk_ordinal = len(chunks)
        body_text = "\n".join(current_text_parts)
        heading = current_headings.get(current_page_number or 0)
        embedding_text = f"{heading}\n{body_text}" if heading else body_text
        content_hash = sha256(normalize_text(embedding_text).encode("utf-8")).hexdigest()
        source_location = _build_source_location(current_batch=current_batch)
        structural_context = _build_structural_context(
            current_batch=current_batch,
            current_page_number=current_page_number,
            current_content_kind=current_content_kind,
            current_source_partition_key=current_source_partition_key,
            heading=heading,
            source_lineage=lineage,
            chunk_ordinal=chunk_ordinal,
            chunking_policy_version=normalized_policy,
        )
        generation_identity = _generation_identity(
            chunking_policy_version=normalized_policy,
            source_lineage=lineage,
            chunk_ordinal=chunk_ordinal,
            canonical_element_keys=tuple(element.stable_key for element in current_batch),
            content_hash=content_hash,
            structural_context=structural_context,
        )
        chunk_key = sha256(
            f"{normalized_policy}:{generation_identity}".encode("utf-8")
        ).hexdigest()
        chunks.append(
            RetrievalChunk(
                chunk_key=chunk_key,
                chunk_ordinal=chunk_ordinal,
                generation_identity=generation_identity,
                content_hash=content_hash,
                embedding_text=embedding_text,
                canonical_element_keys=tuple(element.stable_key for element in current_batch),
                source_location=source_location,
                source_lineage=dict(lineage),
                structural_context=structural_context,
                chunking_policy_version=normalized_policy,
            )
        )
        current_batch = []
        current_text_parts = []
        current_page_number = None
        current_content_kind = None
        current_source_partition_key = None

    for element in ordered_elements:
        text = _element_text(element)
        if element.element_type == "heading" and text:
            if current_batch:
                finalize_batch()
            current_headings[element.page_number] = text
            continue
        if not text:
            continue

        content_kind = _content_kind_for_element(element)
        source_partition_key = _source_partition_key(element)
        candidate_parts = current_text_parts + [text]
        heading = current_headings.get(element.page_number)
        candidate_text = f"{heading}\n" + "\n".join(candidate_parts) if heading else "\n".join(
            candidate_parts
        )
        should_split = bool(current_batch) and (
            current_page_number != element.page_number
            or current_content_kind != content_kind
            or current_source_partition_key != source_partition_key
            or len(candidate_text) > max_chunk_characters
            or len(current_batch) >= max_chunk_elements
        )
        if should_split:
            finalize_batch()
        if not current_batch:
            current_page_number = element.page_number
            current_content_kind = content_kind
            current_source_partition_key = source_partition_key
        current_batch.append(element)
        current_text_parts.append(text)

    finalize_batch()
    return tuple(chunks)


def _element_text(element: CanonicalElement) -> str:
    value = element.normalized_value or element.observed_value or {}
    text = value.get("text")
    if not isinstance(text, str):
        return ""
    return normalize_text(text)


def _content_kind_for_element(element: CanonicalElement) -> str:
    if element.element_type == "table":
        return "table"
    if element.element_type in {"form_field", "identifier", "date", "money"}:
        return "cell"
    if element.element_type in {"list", "list_item"}:
        return "list"
    if element.element_type in {"section", "paragraph", "caption", "annotation", "footnote"}:
        return "prose"
    return "prose"


def _source_partition_key(element: CanonicalElement) -> str:
    region = dict(element.source_region)
    if "sheet_name" in region or "worksheet" in region:
        return "worksheet:" + sha256(
            compute_canonical_hash(
                {
                    "page_number": region.get("page_number"),
                    "sheet_name": region.get("sheet_name"),
                    "worksheet": region.get("worksheet"),
                    "row_start": region.get("row_start"),
                    "row_end": region.get("row_end"),
                }
            ).canonical_json.encode("utf-8")
        ).hexdigest()
    if "slide_number" in region:
        return "slide:" + sha256(
            compute_canonical_hash(
                {
                    "page_number": region.get("page_number"),
                    "slide_number": region.get("slide_number"),
                }
            ).canonical_json.encode("utf-8")
        ).hexdigest()
    if "line_start" in region or "line_end" in region:
        return "line:" + sha256(
            compute_canonical_hash(
                {
                    "page_number": region.get("page_number"),
                    "line_start": region.get("line_start"),
                    "line_end": region.get("line_end"),
                }
            ).canonical_json.encode("utf-8")
        ).hexdigest()
    if "row_start" in region or "row_end" in region:
        return "row:" + sha256(
            compute_canonical_hash(
                {
                    "page_number": region.get("page_number"),
                    "row_start": region.get("row_start"),
                    "row_end": region.get("row_end"),
                }
            ).canonical_json.encode("utf-8")
        ).hexdigest()
    return f"page:{region.get('page_number')}"


def _build_source_location(*, current_batch: list[CanonicalElement]) -> dict[str, object]:
    if len(current_batch) == 1:
        return dict(current_batch[0].source_region)

    regions = [dict(element.source_region) for element in current_batch]
    source_location: dict[str, object] = {
        "page_number": current_batch[0].page_number,
        "source_regions": regions,
        "canonical_element_keys": [element.stable_key for element in current_batch],
    }
    for key in ("sheet_name", "slide_number", "row_start", "row_end", "line_start", "line_end"):
        values = [region.get(key) for region in regions if key in region]
        if values:
            source_location[key] = values[0] if len(set(map(str, values))) == 1 else values
    return source_location


def _build_structural_context(
    *,
    current_batch: list[CanonicalElement],
    current_page_number: int | None,
    current_content_kind: str | None,
    current_source_partition_key: str | None,
    heading: str | None,
    source_lineage: dict[str, object],
    chunk_ordinal: int,
    chunking_policy_version: str,
) -> dict[str, object]:
    context: dict[str, object] = {
        "chunk_ordinal": chunk_ordinal,
        "chunking_policy_version": chunking_policy_version,
        "content_kind": current_content_kind or "prose",
        "element_count": len(current_batch),
        "source_partition_key": current_source_partition_key,
    }
    if current_page_number is not None:
        context["page_number"] = current_page_number
    if heading:
        context["heading"] = heading
    if source_lineage:
        context["source_lineage"] = dict(source_lineage)
    first_region = dict(current_batch[0].source_region)
    for key in ("sheet_name", "slide_number", "row_start", "row_end", "line_start", "line_end"):
        if key in first_region:
            context[key] = first_region[key]
    return context


def _generation_identity(
    *,
    chunking_policy_version: str,
    source_lineage: dict[str, object],
    chunk_ordinal: int,
    canonical_element_keys: tuple[str, ...],
    content_hash: str,
    structural_context: dict[str, object],
) -> str:
    envelope = {
        "chunk_ordinal": chunk_ordinal,
        "chunking_policy_version": chunking_policy_version,
        "canonical_element_keys": list(canonical_element_keys),
        "content_hash": content_hash,
        "source_lineage": source_lineage,
        "structural_context": structural_context,
    }
    return compute_canonical_hash(envelope).sha256_hex
