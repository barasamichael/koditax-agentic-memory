from __future__ import annotations

from uuid import UUID

import pytest

from services.document_ai.app.effective_corrections import CorrectionError
from services.document_ai.app.effective_corrections import EffectiveCorrectionStore

TENANT = "tenant-a"
ACTOR = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ACTOR = UUID("22222222-2222-2222-2222-222222222222")


def _store() -> EffectiveCorrectionStore:
    store = EffectiveCorrectionStore()
    store.register_observation(
        tenant_id=TENANT,
        target_id="amount",
        source_observed_value="KSh 12O,000",
        original_interpreted_value=12000,
    )
    store.register_observation(
        tenant_id=TENANT,
        target_id="identifier",
        source_observed_value="P01Z",
        original_interpreted_value="P012345678X",
    )
    return store


def test_corrected_amount_preserves_observations_and_invalidates_only_closure() -> None:
    store = _store()
    result = store.correct(
        tenant_id=TENANT,
        target_id="amount",
        corrected_value=120000,
        actor_id=ACTOR,
        reason="OCR zero",
        idempotency_key="a",
    )
    effective = store.resolve(tenant_id=TENANT, target_id="amount")
    assert effective.source_observed_value == "KSh 12O,000"
    assert effective.original_interpreted_value == 12000
    assert effective.corrected_value == effective.effective_value == 120000
    assert result.invalidated == {
        "effective_canonical:amount",
        "chunk:amount",
        "embedding:amount",
        "evidence:amount",
        "projection:amount",
        "cache:amount",
    }


def test_corrected_identifier_and_reversal_are_non_destructive() -> None:
    store = _store()
    correction = store.correct(
        tenant_id=TENANT,
        target_id="identifier",
        corrected_value="P0123456789",
        actor_id=ACTOR,
        reason="source check",
        idempotency_key="id",
    )
    reversal = store.reverse(
        tenant_id=TENANT,
        correction_id=correction.correction_id,
        actor_id=ACTOR,
        reason="recheck",
        idempotency_key="undo",
    )
    effective = store.resolve(tenant_id=TENANT, target_id="identifier")
    assert reversal.state == "reversed"
    assert effective.effective_value == "P012345678X"
    assert len(store.history(tenant_id=TENANT, target_id="identifier")) == 2


def test_reprocessing_keeps_active_correction_and_unaffected_evidence() -> None:
    store = _store()
    store.correct(
        tenant_id=TENANT,
        target_id="amount",
        corrected_value=120000,
        actor_id=ACTOR,
        reason="OCR zero",
        idempotency_key="a",
    )
    store.reprocess(
        tenant_id=TENANT,
        observations={"amount": ("KSh 12O,000", 12000), "identifier": ("P01Z", "P012345678X")},
    )
    assert store.resolve(tenant_id=TENANT, target_id="amount").effective_value == 120000
    assert store.resolve(tenant_id=TENANT, target_id="identifier").effective_value == "P012345678X"


def test_correction_target_not_found_and_tenant_isolation() -> None:
    store = _store()
    with pytest.raises(CorrectionError, match="correction_target_not_found"):
        store.correct(
            tenant_id=TENANT,
            target_id="missing",
            corrected_value=1,
            actor_id=ACTOR,
            reason="x",
            idempotency_key="x",
        )
    with pytest.raises(CorrectionError, match="correction_target_not_found"):
        store.correct(
            tenant_id="tenant-b",
            target_id="amount",
            corrected_value=1,
            actor_id=OTHER_ACTOR,
            reason="x",
            idempotency_key="x",
        )
