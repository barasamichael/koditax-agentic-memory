"""Compare explicitly stated taxpayer facts across governed conversation turns."""

from __future__ import annotations

from collections.abc import Mapping

from services.orchestration.app.canonical_fact_ledger import compare_fact_ledgers
from services.orchestration.app.canonical_fact_ledger import build_canonical_fact_ledger
from services.orchestration.app.response_integrity_signals import FactMismatch
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts


def compare_stated_facts(
    current: ExtractedTaxpayerFacts | Mapping[str, object],
    prior: ExtractedTaxpayerFacts | Mapping[str, object],
    prior_execution_id: str,
) -> list[FactMismatch]:
    """Return differences only where both conversation turns stated a fact."""

    current_ledger = build_canonical_fact_ledger(
        stated_facts=current,
        origin_execution_id=None,
        origin_record_id=None,
        source_status="explicit",
        turn_sequence=1,
    )
    prior_ledger = build_canonical_fact_ledger(
        stated_facts=prior,
        origin_execution_id=prior_execution_id,
        origin_record_id=None,
        source_status="reused",
        turn_sequence=0,
    )
    return compare_fact_ledgers(
        current=current_ledger,
        prior=prior_ledger,
        prior_execution_id=prior_execution_id,
    )
