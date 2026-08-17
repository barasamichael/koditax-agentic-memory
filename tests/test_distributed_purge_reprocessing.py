"""Milestone 26 lifecycle acceptance tests at the provider-independent boundary."""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.document_ai.app.reprocessing import create_candidate
from services.document_ai.app.reprocessing import ProcessingPolicy
from services.document_ai.app.reprocessing import CorrectionMapping
from services.document_ai.app.reprocessing import remap_corrections
from services.document_ai.app.reprocessing import activate_candidate
from services.document_ai.app.reprocessing import rollback_candidate
from services.document_ai.app.reprocessing import validate_candidate
from services.document_ai.app.reprocessing import mark_candidate_vectors_complete
from services.document_ai.app.distributed_purge import PurgeError
from services.document_ai.app.distributed_purge import PurgeTarget
from services.document_ai.app.distributed_purge import PurgeOperation
from services.document_ai.app.distributed_purge import PurgeTargetKind
from services.document_ai.app.distributed_purge import reconcile_purge
from services.document_ai.app.distributed_purge import create_purge_operation
from services.document_ai.app.distributed_purge import may_mark_document_purged


class _Executor:
    def __init__(self, failing: PurgeTargetKind | None = None) -> None:
        self.failing = failing
        self.deleted: set[tuple[PurgeTargetKind, str]] = set()

    def delete(self, *, tenant_id: str, document_id: object, target: PurgeTarget) -> None:
        del tenant_id, document_id
        if target.kind == self.failing:
            raise RuntimeError("provider_failure")
        self.deleted.add((target.kind, target.reference))

    def is_resolved(self, *, tenant_id: str, document_id: object, target: PurgeTarget) -> bool:
        del tenant_id, document_id
        return (target.kind, target.reference) in self.deleted


def _operation() -> PurgeOperation:
    return create_purge_operation(
        tenant_id="tenant-a", document_id=uuid4(),
        targets=tuple(PurgeTarget(kind, kind.value) for kind in PurgeTargetKind),
    )


def test_purge_requires_every_target_and_retries_provider_failure() -> None:
    operation = _operation()
    failed = reconcile_purge(operation=operation, executor=_Executor(PurgeTargetKind.R2_ORIGINAL))
    assert not may_mark_document_purged(failed)
    completed = reconcile_purge(operation=failed, executor=_Executor())
    assert may_mark_document_purged(completed)


def test_purge_manifest_cannot_omit_required_target() -> None:
    with pytest.raises(PurgeError, match="missing_required"):
        create_purge_operation(tenant_id="tenant-a", document_id=uuid4(), targets=())


def test_rejected_candidate_preserves_active_and_activation_requires_vectors() -> None:
    prior = uuid4()
    candidate = create_candidate(
        document_version_id=uuid4(), active_representation_id=prior,
        policy=ProcessingPolicy("model-v2", "prompt-v2", "schema-v2", "embedding-v2"),
    )
    rejected = validate_candidate(candidate=candidate, valid=False)
    assert rejected.prior_active_representation_id == prior
    valid = validate_candidate(candidate=candidate, valid=True)
    with pytest.raises(Exception, match="not_ready"):
        activate_candidate(valid)
    activated = activate_candidate(mark_candidate_vectors_complete(valid))
    assert activated.prior_active_representation_id == prior
    assert rollback_candidate(activated).state == "rolled_back"


def test_unmapped_correction_rejects_candidate_without_touching_active() -> None:
    mappings = remap_corrections(
        prior_corrections=(CorrectionMapping(uuid4(), "old-key", None, "preserved"),),
        candidate_stable_keys={"new-key"},
    )
    candidate = create_candidate(
        document_version_id=uuid4(),
        active_representation_id=uuid4(),
        policy=ProcessingPolicy("model-v2", "prompt-v2", "schema-v2", "embedding-v2"),
    )
    candidate = candidate.__class__(
        candidate.candidate_id,
        candidate.document_version_id,
        candidate.policy,
        candidate.state,
        candidate.prior_active_representation_id,
        corrections=mappings,
    )
    assert validate_candidate(candidate=candidate, valid=True).state == "rejected"
