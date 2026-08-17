"""Candidate-based reprocessing controls independent from active authority."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID
from uuid import uuid4
from dataclasses import replace
from dataclasses import dataclass


class CandidateState(StrEnum):
    BUILDING = "building"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class ProcessingPolicy:
    model_policy_version: str
    prompt_version: str
    canonical_schema_version: str
    embedding_version: str


@dataclass(frozen=True)
class CorrectionMapping:
    correction_id: UUID
    prior_stable_key: str
    candidate_stable_key: str | None
    state: str  # preserved, remapped, unresolved


@dataclass(frozen=True)
class ReprocessingCandidate:
    candidate_id: UUID
    document_version_id: UUID
    policy: ProcessingPolicy
    state: CandidateState
    prior_active_representation_id: UUID | None
    vectors_complete: bool = False
    corrections: tuple[CorrectionMapping, ...] = ()
    rejection_reason: str | None = None


class ReprocessingError(ValueError):
    """A candidate would compromise active representation safety."""


def create_candidate(
    *, document_version_id: UUID, policy: ProcessingPolicy, active_representation_id: UUID | None
) -> ReprocessingCandidate:
    """Start a new generation while retaining the existing active representation."""

    if not all(vars(policy).values()):
        raise ReprocessingError("processing_policy_version_required")
    return ReprocessingCandidate(
        uuid4(), document_version_id, policy, CandidateState.BUILDING, active_representation_id
    )


def remap_corrections(
    *, prior_corrections: tuple[CorrectionMapping, ...], candidate_stable_keys: set[str]
) -> tuple[CorrectionMapping, ...]:
    """Preserve matching corrections and explicitly mark unsafe mappings unresolved."""

    mapped: list[CorrectionMapping] = []
    for correction in prior_corrections:
        if correction.prior_stable_key in candidate_stable_keys:
            mapped.append(
                replace(
                    correction,
                    candidate_stable_key=correction.prior_stable_key,
                    state="preserved",
                )
            )
        elif correction.candidate_stable_key in candidate_stable_keys:
            mapped.append(replace(correction, state="remapped"))
        else:
            mapped.append(replace(correction, candidate_stable_key=None, state="unresolved"))
    return tuple(mapped)


def validate_candidate(
    *, candidate: ReprocessingCandidate, valid: bool, reason: str | None = None
) -> ReprocessingCandidate:
    if candidate.state is not CandidateState.BUILDING:
        raise ReprocessingError("candidate_not_building")
    if not valid:
        return replace(
            candidate,
            state=CandidateState.REJECTED,
            rejection_reason=reason or "candidate_rejected",
        )
    if any(item.state == "unresolved" for item in candidate.corrections):
        return replace(
            candidate,
            state=CandidateState.REJECTED,
            rejection_reason="correction_remap_unresolved",
        )
    return replace(candidate, state=CandidateState.VALIDATED)


def mark_candidate_vectors_complete(candidate: ReprocessingCandidate) -> ReprocessingCandidate:
    if candidate.state is not CandidateState.VALIDATED:
        raise ReprocessingError("candidate_not_validated")
    return replace(candidate, vectors_complete=True)


def activate_candidate(candidate: ReprocessingCandidate) -> ReprocessingCandidate:
    """Permit atomic authority switch only after validation and candidate vectors."""

    if candidate.state is not CandidateState.VALIDATED or not candidate.vectors_complete:
        raise ReprocessingError("candidate_not_ready_for_activation")
    return replace(candidate, state=CandidateState.ACTIVE)


def rollback_candidate(candidate: ReprocessingCandidate) -> ReprocessingCandidate:
    """Rollback restores retained prior authority; no deleted data is reconstructed."""

    if (
        candidate.state is not CandidateState.ACTIVE
        or candidate.prior_active_representation_id is None
    ):
        raise ReprocessingError("candidate_rollback_not_available")
    return replace(candidate, state=CandidateState.ROLLED_BACK)
