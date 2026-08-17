"""Canonical user-facing response contract for orchestration answer synthesis."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
from typing import NotRequired

from pydantic import Field
from pydantic import BaseModel

from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals

AnswerMode = Literal[
    "compute_execution",
    "grounded_knowledge",
    "compute_plus_grounding",
    "forms_execution",
    "reports_execution",
    "document_extraction",
    "unsupported",
]
ResponseSynthesisStatus = Literal["generated", "failed"]


class UnifiedAnswerCitationModel(BaseModel):
    """Represent one governed citation reference included in user-facing answers."""

    citation_index: int
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    authority_level: str
    temporal_applicability: str


class UnifiedAnswerSourceLocationModel(BaseModel):
    """Represent one user-facing source location derived from governed document evidence."""

    location_kind: str
    location_label: str
    location_status: str
    page_number: int | None = None
    slide_number: int | None = None
    sheet_name: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    cell_reference: str | None = None
    section_name: str | None = None


class UnifiedAnswerSourceReferenceModel(BaseModel):
    """Represent one secure document source reference for conversational answers."""

    document_id: str
    document_label: str
    document_status: str
    source_location: UnifiedAnswerSourceLocationModel
    openable: bool = True
    accessibility_label: str | None = None


class UnifiedAnswerResponseModel(BaseModel):
    """Represent additive user-facing answer section for orchestration responses."""

    status: ResponseSynthesisStatus
    answer_text: str | None = None
    answer_mode: AnswerMode
    citations: list[UnifiedAnswerCitationModel] = Field(
        default_factory=lambda: cast(list[UnifiedAnswerCitationModel], [])
    )
    source_references: list[UnifiedAnswerSourceReferenceModel] = Field(
        default_factory=lambda: cast(list[UnifiedAnswerSourceReferenceModel], [])
    )
    assumptions: list[str] = Field(default_factory=lambda: cast(list[str], []))
    warnings: list[str] = Field(default_factory=lambda: cast(list[str], []))
    integrity_signals: ResponseIntegritySignals = Field(default_factory=ResponseIntegritySignals)


class StructuredAnswerDraft(TypedDict):
    """Represent structured LLM draft before governed citation mapping."""

    answer_text: str
    cited_indices: list[int]
    unverified_or_contradicting_user_facts: list[str]
    unsupported_claims_unresolved: NotRequired[list[str]]
    contradictions_found: NotRequired[list[str]]
