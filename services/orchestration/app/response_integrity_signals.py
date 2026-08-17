"""Response-level integrity signal contract for orchestration synthesis."""

from __future__ import annotations

from typing import cast
from typing import Literal

from pydantic import Field
from pydantic import BaseModel
from typing_extensions import TypedDict


class ContradictionFinding(TypedDict):
    claim_topic: str
    source_a_id: str
    source_a_value: str
    source_b_id: str
    source_b_value: str


class FactMismatch(TypedDict):
    field: str
    prior_value: object
    prior_execution_id: str
    current_value: object


class ResponseIntegritySignals(BaseModel):
    verification_is_verified: bool = True
    verification_confidence: float = 1.0
    unsupported_claims: list[str] = Field(default_factory=lambda: cast(list[str], []))
    contradictions_found: list[str] = Field(default_factory=lambda: cast(list[str], []))
    grounding_contradictions: list[ContradictionFinding] = Field(
        default_factory=lambda: cast(list[ContradictionFinding], [])
    )
    unverified_or_contradicting_user_facts: list[FactMismatch | str] = Field(
        default_factory=lambda: cast(list[FactMismatch | str], [])
    )
    synthesis_tool_iterations_used: int = 0
    confidence_flag: Literal["high", "medium", "low"] = "high"
