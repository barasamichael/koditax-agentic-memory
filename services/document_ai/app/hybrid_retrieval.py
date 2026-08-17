"""Deterministic hybrid retrieval over semantic and exact candidate signals."""

from __future__ import annotations

from uuid import UUID
from typing import Literal
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Sequence

from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict

from services.document_ai.app.exact_retrieval import ExactRetrievalRequest
from services.document_ai.app.exact_retrieval import ExactRetrievalCandidate
from services.document_ai.app.exact_retrieval import ExactRetrievalRepository
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRequest
from services.document_ai.app.semantic_retrieval import SemanticRetrievalCandidate
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRepository

_MAX_RETRIEVAL_LIMIT = 100
_HYBRID_BRANCH_POOL_MULTIPLIER = 2
_HYBRID_RRF_K = 60.0
_RETRIEVAL_METHOD_EXACT: Literal["exact"] = "exact"
_RETRIEVAL_METHOD_SEMANTIC: Literal["semantic"] = "semantic"


class HybridRetrievalCandidate(BaseModel):
    """Represent one fused candidate with method provenance and raw signals."""

    model_config = ConfigDict(extra="forbid")

    retrieval_chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    chunk_key: str
    content_hash_sha256: str
    chunking_policy_version: str
    canonical_element_keys: tuple[str, ...]
    source_location: dict[str, object]
    structural_context: dict[str, object]
    source_lineage: dict[str, object] = Field(default_factory=dict)
    source_filename: str
    display_name: str | None = None
    semantic_distance: float | None = None
    semantic_score: float | None = None
    exact_match_rank: int | None = None
    retrieval_methods: list[Literal["exact", "semantic"]] = Field(default_factory=list)
    fusion_rank: int
    fusion_score: float


class HybridRetrievalEnvelope(BaseModel):
    """Expose fused candidates only, never adjudicated evidence."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    candidates: list[HybridRetrievalCandidate]


@dataclass(slots=True)
class _HybridCandidateState:
    """Accumulate exact and semantic signals for one retrieval chunk."""

    retrieval_chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    chunk_key: str
    content_hash_sha256: str
    chunking_policy_version: str
    canonical_element_keys: tuple[str, ...]
    source_location: dict[str, object]
    structural_context: dict[str, object]
    source_lineage: dict[str, object]
    source_filename: str
    display_name: str | None
    semantic_distance: float | None = None
    semantic_score: float | None = None
    exact_match_rank: int | None = None
    retrieval_methods: set[Literal["exact", "semantic"]] = field(default_factory=set)
    semantic_rank: int | None = None
    exact_rank: int | None = None
    fusion_score: float = 0.0

    def merge_semantic(self, candidate: SemanticRetrievalCandidate, *, rank: int) -> None:
        self.retrieval_methods.add(_RETRIEVAL_METHOD_SEMANTIC)
        self.semantic_distance = candidate.semantic_distance
        self.semantic_score = candidate.semantic_score
        self.semantic_rank = rank
        self.fusion_score += _rrf_score(rank)

    def merge_exact(self, candidate: ExactRetrievalCandidate, *, rank: int) -> None:
        self.retrieval_methods.add(_RETRIEVAL_METHOD_EXACT)
        self.exact_match_rank = candidate.exact_match_rank
        self.exact_rank = rank
        self.fusion_score += _rrf_score(rank)

    def to_candidate(self, *, fusion_rank: int) -> HybridRetrievalCandidate:
        return HybridRetrievalCandidate(
            retrieval_chunk_id=self.retrieval_chunk_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            canonical_representation_id=self.canonical_representation_id,
            chunk_key=self.chunk_key,
            content_hash_sha256=self.content_hash_sha256,
            chunking_policy_version=self.chunking_policy_version,
            canonical_element_keys=self.canonical_element_keys,
            source_location=dict(self.source_location),
            structural_context=dict(self.structural_context),
            source_lineage=dict(self.source_lineage),
            source_filename=self.source_filename,
            display_name=self.display_name,
            semantic_distance=self.semantic_distance,
            semantic_score=self.semantic_score,
            exact_match_rank=self.exact_match_rank,
            retrieval_methods=_ordered_retrieval_methods(self.retrieval_methods),
            fusion_rank=fusion_rank,
            fusion_score=self.fusion_score,
        )


class HybridRetrievalRepository:
    """Fuse the semantic and exact retrieval boundaries deterministically."""

    def __init__(
        self,
        *,
        database_url: str,
        exact_retrieval_repository: ExactRetrievalRepository | None = None,
        semantic_retrieval_repository: SemanticRetrievalRepository | None = None,
    ) -> None:
        self._database_url = database_url
        self._exact_retrieval_repository = exact_retrieval_repository or ExactRetrievalRepository(
            database_url=database_url
        )
        self._semantic_retrieval_repository = (
            semantic_retrieval_repository
            or SemanticRetrievalRepository(database_url=database_url)
        )

    def retrieve(
        self,
        *,
        tenant_id: str,
        owner_user_id: UUID,
        request: SemanticRetrievalRequest,
    ) -> HybridRetrievalEnvelope:
        branch_limit = min(request.limit * _HYBRID_BRANCH_POOL_MULTIPLIER, _MAX_RETRIEVAL_LIMIT)
        exact_request = _build_exact_request(request=request, limit=branch_limit)
        exact_candidates = self._exact_retrieval_repository.retrieve(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            request=exact_request,
        )
        semantic_envelope = self._semantic_retrieval_repository.retrieve(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            request=request.model_copy(update={"limit": branch_limit}),
        )
        fused_candidates = _fuse_candidates(
            exact_candidates=exact_candidates,
            semantic_candidates=semantic_envelope.candidates,
        )
        return HybridRetrievalEnvelope(candidates=fused_candidates[: request.limit])


def _build_exact_request(
    *, request: SemanticRetrievalRequest, limit: int
) -> ExactRetrievalRequest:
    """Project semantic query text into the lexical probe used by hybrid fusion."""

    return ExactRetrievalRequest(
        document_ids=list(request.document_ids),
        document_version_id=request.document_version_id,
        conversation_id=request.conversation_id,
        turn_id=request.turn_id,
        full_text=request.query,
        canonical_element_types=list(request.canonical_element_types),
        page_number=request.page_number,
        limit=limit,
    )


def _fuse_candidates(
    *,
    exact_candidates: Sequence[ExactRetrievalCandidate],
    semantic_candidates: Sequence[SemanticRetrievalCandidate],
) -> list[HybridRetrievalCandidate]:
    """Deduplicate by retrieval identity and fuse by reciprocal-rank score."""

    states: dict[UUID, _HybridCandidateState] = {}
    for rank, candidate in enumerate(semantic_candidates, start=1):
        state = states.get(candidate.retrieval_chunk_id)
        if state is None:
            state = states[candidate.retrieval_chunk_id] = _state_from_semantic(candidate)
        state.merge_semantic(candidate, rank=rank)
    for rank, candidate in enumerate(exact_candidates, start=1):
        state = states.get(candidate.retrieval_chunk_id)
        if state is None:
            state = states[candidate.retrieval_chunk_id] = _state_from_exact(candidate)
        state.merge_exact(candidate, rank=rank)

    ordered_states = sorted(
        states.values(),
        key=lambda state: (
            -state.fusion_score,
            -len(state.retrieval_methods),
            state.exact_rank if state.exact_rank is not None else 10**9,
            state.semantic_rank if state.semantic_rank is not None else 10**9,
            state.document_id,
            state.chunk_key,
            state.retrieval_chunk_id,
        ),
    )
    return [
        state.to_candidate(fusion_rank=fusion_rank)
        for fusion_rank, state in enumerate(ordered_states, start=1)
    ]


def _state_from_semantic(candidate: SemanticRetrievalCandidate) -> _HybridCandidateState:
    source_lineage = _extract_source_lineage(candidate.structural_context)
    return _HybridCandidateState(
        retrieval_chunk_id=candidate.retrieval_chunk_id,
        document_id=candidate.document_id,
        document_version_id=candidate.document_version_id,
        canonical_representation_id=candidate.canonical_representation_id,
        chunk_key=candidate.chunk_key,
        content_hash_sha256=candidate.content_hash_sha256,
        chunking_policy_version=candidate.chunking_policy_version,
        canonical_element_keys=tuple(candidate.canonical_element_keys),
        source_location=dict(candidate.source_location),
        structural_context=dict(candidate.structural_context),
        source_lineage=source_lineage,
        source_filename=candidate.source_filename,
        display_name=candidate.display_name,
    )


def _state_from_exact(candidate: ExactRetrievalCandidate) -> _HybridCandidateState:
    return _HybridCandidateState(
        retrieval_chunk_id=candidate.retrieval_chunk_id,
        document_id=candidate.document_id,
        document_version_id=candidate.document_version_id,
        canonical_representation_id=candidate.canonical_representation_id,
        chunk_key=candidate.chunk_key,
        content_hash_sha256=candidate.content_hash_sha256,
        chunking_policy_version=candidate.chunking_policy_version,
        canonical_element_keys=tuple(candidate.canonical_element_keys),
        source_location=dict(candidate.source_location),
        structural_context=dict(candidate.structural_context),
        source_lineage=dict(candidate.source_lineage),
        source_filename=candidate.source_filename,
        display_name=candidate.display_name,
    )


def _extract_source_lineage(structural_context: dict[str, object]) -> dict[str, object]:
    source_lineage = structural_context.get("source_lineage")
    if isinstance(source_lineage, dict):
        return dict(source_lineage)
    return {}


def _rrf_score(rank: int) -> float:
    return 1.0 / (_HYBRID_RRF_K + float(rank))


def _ordered_retrieval_methods(
    methods: set[Literal["exact", "semantic"]]
) -> list[Literal["exact", "semantic"]]:
    ordered_methods: list[Literal["exact", "semantic"]] = []
    if _RETRIEVAL_METHOD_EXACT in methods:
        ordered_methods.append(_RETRIEVAL_METHOD_EXACT)
    if _RETRIEVAL_METHOD_SEMANTIC in methods:
        ordered_methods.append(_RETRIEVAL_METHOD_SEMANTIC)
    return ordered_methods


HybridRetrievalRequest = SemanticRetrievalRequest
CanonicalDiscoveryCandidate = HybridRetrievalCandidate
