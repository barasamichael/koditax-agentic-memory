"""
Governed synthesis-context builder for orchestration LLM answer generation.
"""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from typing import NotRequired
from collections.abc import Mapping
from collections.abc import Sequence

from services.knowledge.app.repository import KnowledgeSearchRecord
from services.orchestration.app.canonical_claim_ledger import ClaimLedgerError
from services.orchestration.app.canonical_claim_ledger import build_canonical_claims_from_evidence
from services.orchestration.app.canonical_claim_ledger import claim_pair_to_finding
from services.orchestration.app.canonical_claim_ledger import generate_candidate_pairs
from services.orchestration.app.canonical_claim_ledger import judge_candidate_pair
from services.orchestration.app.llm_response_contract import AnswerMode
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts
from services.orchestration.app.response_integrity_signals import FactMismatch
from services.orchestration.app.response_integrity_signals import ContradictionFinding
from services.orchestration.app.grounded_explanation_renderer import GroundedExplanationError
from services.orchestration.app.grounded_explanation_renderer import render_grounded_explanation
from services.orchestration.app.request_timer import timed_print


class SynthesisContextError(RuntimeError):
    """Represent fail-closed synthesis context construction failures."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason_code: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason_code = reason_code
        self.context = context


class GovernedSynthesisCitation(TypedDict):
    """Represent one governed citation allowed in synthesized answers."""

    citation_index: int
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    authority_level: str
    temporal_applicability: str


class GovernedSynthesisContext(TypedDict):
    """
    Represent one strict synthesis context projected from governed execution
        output.
    """

    answer_mode: AnswerMode
    prompt_text: str
    tax_domain_hint: str
    intent_class: str
    plan_summary: dict[str, object]
    computation_summary: dict[str, object] | None
    service_result_summary: dict[str, object] | None
    grounded_evidence: list[dict[str, object]]
    explanation_items: list[dict[str, object]]
    citations: list[GovernedSynthesisCitation]
    source_references: list[dict[str, object]]
    authority_summary: dict[str, object] | None
    temporal_applicability: dict[str, object] | None
    conversation_context_summary: dict[str, object] | None
    assumptions: list[str]
    warnings: list[str]
    grounding_contradictions: list[ContradictionFinding]
    fact_mismatches: list[FactMismatch]
    taxpayer_fact_instructions: list[str]
    synthesis_tool_runtime: NotRequired[dict[str, object]]


def detect_grounding_contradictions(
    evidence_records: Sequence[Mapping[str, object] | KnowledgeSearchRecord],
) -> list[ContradictionFinding]:
    """Detect conflicting grounded claims using the canonical claim ledger."""

    timed_print(
        "[FACTS] About to detect grounding contradictions "
        f"evidence_count={len(evidence_records)}"
    )
    claims = build_canonical_claims_from_evidence(evidence_records)
    candidate_pairs = generate_candidate_pairs(claims)
    findings: list[ContradictionFinding] = []
    for pair in candidate_pairs:
        decision = judge_candidate_pair(pair)
        finding = claim_pair_to_finding(pair, decision)
        if finding is not None:
            findings.append(cast(ContradictionFinding, finding))
    timed_print(
        "[FACTS] Detected grounding contradictions "
        f"finding_count={len(findings)}"
    )
    return findings


def requires_grounded_legal_basis_synthesis(answer_mode: AnswerMode) -> bool:
    """
    Return whether one answer mode depends on grounded legal-basis synthesis.
    """

    return answer_mode in {"grounded_knowledge", "compute_plus_grounding"}


def inject_verification_failure_reasons(
    context: GovernedSynthesisContext,
    verification_result: Mapping[str, object],
) -> GovernedSynthesisContext:
    """Return synthesis context augmented with specific verification failures."""

    failed_checks = verification_result.get("failed_checks")
    issues_found = verification_result.get("issues_found")
    normalized_checks = (
        [item for item in cast(list[object], failed_checks) if isinstance(item, str) and item]
        if isinstance(failed_checks, list)
        else []
    )
    normalized_issues = (
        [item for item in cast(list[object], issues_found) if isinstance(item, str) and item]
        if isinstance(issues_found, list)
        else []
    )
    reasons = ", ".join(normalized_checks) or "verification checks"
    details = "; ".join(normalized_issues) or "No further detail was available."
    retry_context = dict(context)
    retry_context["warnings"] = [
        *context["warnings"],
        (
            "Verification retry required. Correct these failed checks: "
            f"{reasons}. Relevant verification details: {details}"
        ),
    ]
    return cast(GovernedSynthesisContext, retry_context)


def requires_service_artifact_context(answer_mode: AnswerMode) -> bool:
    """
    Return whether one answer mode depends on governed service artifact
        context.
    """

    return answer_mode in {
        "forms_execution",
        "reports_execution",
        "document_extraction",
    }


def build_governed_synthesis_context(
    *,
    prompt_text: str,
    tax_domain_hint: str,
    intent_class: str,
    plan: Mapping[str, object],
    mapped_result: Mapping[str, object],
    final_outcome: Mapping[str, object],
    selected_route: Mapping[str, object] | None,
    adapter_response: Mapping[str, object] | None,
    step_results: Sequence[Mapping[str, object]] | None,
    step_summary: Mapping[str, object] | None,
    grounded_evidence: Sequence[Mapping[str, object]] | None,
    explanation_items: Sequence[Mapping[str, object]] | None,
    citations: Sequence[Mapping[str, object]] | None,
    authority_summary: Mapping[str, object] | None,
    temporal_applicability: Mapping[str, object] | None,
    conversation_context_summary: Mapping[str, object] | None = None,
    prior_stated_facts: ExtractedTaxpayerFacts | None = None,
    prior_execution_id: str | None = None,
    fact_mismatches: Sequence[FactMismatch] = (),
) -> GovernedSynthesisContext:
    """Project a governed execution envelope into strict LLM synthesis input."""

    answer_mode = _resolve_answer_mode(
        intent_class=intent_class,
        plan=plan,
        selected_route=selected_route,
    )
    normalized_grounded_evidence = _normalize_records(grounded_evidence)
    normalized_explanation_items = _normalize_records(explanation_items)
    normalized_citations = _normalize_records(citations)
    service_result_summary_payload = _build_service_result_summary(
        answer_mode=answer_mode,
        mapped_result=mapped_result,
        final_outcome=final_outcome,
        adapter_response=adapter_response,
    )
    try:
        grounding_contradictions = detect_grounding_contradictions(normalized_grounded_evidence)
    except ClaimLedgerError as error:
        raise SynthesisContextError(
            error_code="response_synthesis_failed",
            message=error.message,
            reason_code=error.reason_code,
            context=error.context,
        ) from error
    authority_summary_payload = dict(authority_summary) if authority_summary is not None else None
    temporal_applicability_payload = (
        dict(temporal_applicability) if temporal_applicability is not None else None
    )
    if answer_mode in {"grounded_knowledge", "compute_plus_grounding"}:
        if not normalized_grounded_evidence:
            raise SynthesisContextError(
                error_code="response_synthesis_failed",
                message=(
                    "Grounded answer synthesis requires governed evidence and none was available."
                ),
                reason_code="insufficient_grounding_for_synthesis",
                context={
                    "intent_class": intent_class,
                    "tax_domain_hint": tax_domain_hint,
                },
            )
        if not normalized_citations:
            try:
                timed_print("[GROUNDING] About to render grounded explanation")
                rendered = render_grounded_explanation(
                    grounded_evidence=normalized_grounded_evidence,
                )
                timed_print(
                    "[GROUNDING] Rendered grounded explanation "
                    f"citation_count={len(rendered['citations'])}"
                )
            except GroundedExplanationError as error:
                raise SynthesisContextError(
                    error_code="response_synthesis_failed",
                    message=error.message,
                    reason_code="insufficient_grounding_for_synthesis",
                    context={
                        "intent_class": intent_class,
                        "tax_domain_hint": tax_domain_hint,
                    },
                ) from error
            normalized_explanation_items = _normalize_records(rendered["explanation_items"])
            normalized_citations = _normalize_records(rendered["citations"])
            authority_summary_payload = dict(rendered["authority_summary"])
            temporal_applicability_payload = dict(rendered["temporal_applicability"])

    assumptions, warnings = _build_assumptions_and_warnings(
        answer_mode=answer_mode,
        mapped_result=mapped_result,
        normalized_grounded_evidence=normalized_grounded_evidence,
        temporal_applicability=temporal_applicability_payload,
        conversation_context_summary=conversation_context_summary,
    )
    normalized_fact_mismatches = list(fact_mismatches)
    return {
        "answer_mode": answer_mode,
        "prompt_text": prompt_text,
        "tax_domain_hint": tax_domain_hint,
        "intent_class": intent_class,
        "plan_summary": _build_plan_summary(
            plan=plan,
            selected_route=selected_route,
            step_summary=step_summary,
        ),
        "computation_summary": _build_computation_summary(
            answer_mode=answer_mode,
            mapped_result=mapped_result,
            final_outcome=final_outcome,
            adapter_response=adapter_response,
            step_results=step_results,
        ),
        "service_result_summary": service_result_summary_payload,
        "grounded_evidence": normalized_grounded_evidence,
        "explanation_items": normalized_explanation_items,
        "citations": _project_citations(normalized_citations),
        "source_references": _build_source_references(
            answer_mode=answer_mode,
            grounded_evidence=normalized_grounded_evidence,
            service_result_summary=service_result_summary_payload,
        ),
        "authority_summary": authority_summary_payload,
        "temporal_applicability": temporal_applicability_payload,
        "conversation_context_summary": (
            dict(conversation_context_summary) if conversation_context_summary is not None else None
        ),
        "assumptions": assumptions,
        "warnings": warnings,
        "grounding_contradictions": grounding_contradictions,
        "fact_mismatches": normalized_fact_mismatches,
        "taxpayer_fact_instructions": _build_taxpayer_fact_instructions(
            prior_stated_facts=prior_stated_facts,
            prior_execution_id=prior_execution_id,
            fact_mismatches=normalized_fact_mismatches,
        ),
    }


def _build_taxpayer_fact_instructions(
    *,
    prior_stated_facts: ExtractedTaxpayerFacts | None,
    prior_execution_id: str | None,
    fact_mismatches: Sequence[FactMismatch],
) -> list[str]:
    if prior_stated_facts is None or prior_execution_id is None:
        return []
    labels = {
        "income_amount_kes": "income amount in KES",
        "income_frequency": "income frequency",
        "turnover_amount_kes": "turnover amount in KES",
        "residency_status": "residency status",
        "filing_status": "filing status",
    }
    mismatched_fields = {finding["field"] for finding in fact_mismatches}
    instructions: list[str] = []
    for field_name, label in labels.items():
        value = prior_stated_facts.get(field_name)
        if value is None or field_name in mismatched_fields:
            continue
        instructions.append(
            f"The user explicitly stated {label} as {value} in execution "
            f"{prior_execution_id}. Use it only if the current turn does not replace it."
        )
    for finding in fact_mismatches:
        instructions.append(
            f"The user previously stated {finding['field']} as {finding['prior_value']} "
            f"in execution {finding['prior_execution_id']} and now states "
            f"{finding['current_value']}. Address this discrepancy explicitly—ask which "
            "is correct, or clearly state both and flag the conflict. Do not silently use "
            "one value."
        )
    return instructions


def _resolve_answer_mode(
    *,
    intent_class: str,
    plan: Mapping[str, object],
    selected_route: Mapping[str, object] | None,
) -> AnswerMode:
    planning_mode = str(plan.get("planning_mode", "single_step"))
    if intent_class == "compute_plus_grounding" or planning_mode == "multi_step":
        return "compute_plus_grounding"
    if intent_class in {
        "lookup_grounded_knowledge",
        "retrieve_grounded_knowledge",
    }:
        return "grounded_knowledge"
    if selected_route is not None and selected_route.get("target_service") == "forms":
        return "forms_execution"
    if selected_route is not None and selected_route.get("target_service") == "reports":
        return "reports_execution"
    if selected_route is not None and selected_route.get("target_service") == "document_ai":
        return "document_extraction"
    if selected_route is not None and selected_route.get("target_service") == "tax_core":
        return "compute_execution"
    raise SynthesisContextError(
        error_code="response_synthesis_not_supported",
        message="Response synthesis is not supported for this execution context.",
        reason_code="unsupported_response_synthesis_context",
        context={
            "intent_class": intent_class,
            "planning_mode": planning_mode,
            "target_service": (
                selected_route.get("target_service") if selected_route is not None else None
            ),
        },
    )


def _normalize_records(
    payload: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    if payload is None:
        return []
    return [dict(item) for item in payload]


def _build_source_references(
    *,
    answer_mode: AnswerMode,
    grounded_evidence: Sequence[Mapping[str, object]],
    service_result_summary: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if answer_mode != "document_extraction":
        return []

    candidate_items: list[Mapping[str, object]] = [
        item for item in grounded_evidence if isinstance(item.get("document_id"), str)
    ]
    if service_result_summary is not None:
        for field_name in ("evidence", "candidates"):
            field_value = service_result_summary.get(field_name)
            if isinstance(field_value, list):
                candidate_items.extend(
                    item for item in cast(list[object], field_value) if isinstance(item, Mapping)
                )

    references: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for item in candidate_items:
        reference = _build_source_reference(item)
        if reference is None:
            continue
        dedupe_key = (
            reference["document_id"],
            reference["source_location"]["location_kind"],
            reference["source_location"]["location_label"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        references.append(reference)

    references.sort(
        key=lambda item: (
            str(item["document_label"]).casefold(),
            str(item["source_location"]["location_label"]).casefold(),
            str(item["document_id"]),
        )
    )
    return references


def _build_source_reference(item: Mapping[str, object]) -> dict[str, object] | None:
    document_id = _optional_string(item.get("document_id"))
    if document_id is None:
        return None
    source_region_value = item.get("source_region")
    if not isinstance(source_region_value, Mapping):
        return None

    document_label = (
        _optional_string(item.get("display_name"))
        or _optional_string(item.get("source_filename"))
        or "Document"
    )
    lifecycle_status = _optional_string(item.get("lifecycle_status"))
    publication_state = _optional_string(item.get("publication_state"))
    if lifecycle_status in {"purged", "trashed"}:
        document_status = "unavailable"
    elif lifecycle_status in {"uploaded", "processing", "queued", "pending"}:
        document_status = "partial"
    elif publication_state in {"archived", "superseded"}:
        document_status = "partial"
    else:
        document_status = "available"

    location = _build_source_location(
        source_region=cast(Mapping[str, object], source_region_value),
        element_type=item.get("element_type"),
    )
    accessibility_label = (
        "Source available"
        if document_status == "available"
        else "Source partially available"
        if document_status == "partial"
        else "Source unavailable"
    )
    return {
        "document_id": document_id,
        "document_label": document_label,
        "document_status": document_status,
        "source_location": location,
        "openable": document_status != "unavailable",
        "accessibility_label": accessibility_label,
    }


def _build_source_location(
    *,
    source_region: Mapping[str, object],
    element_type: object,
) -> dict[str, object]:
    page_number = _optional_int(source_region.get("page_number"))
    slide_number = _optional_int(source_region.get("slide_number"))
    sheet_name = _optional_string(source_region.get("sheet_name"))
    line_start = _optional_int(source_region.get("line_start"))
    line_end = _optional_int(source_region.get("line_end"))
    cell_reference = _optional_string(source_region.get("cell_reference"))
    section_name = (
        _optional_string(source_region.get("section_name"))
        or _optional_string(source_region.get("heading"))
    )

    if isinstance(element_type, str) and element_type in {"heading", "section"} and section_name is None:
        section_name = "Section"

    if cell_reference is not None:
        label = f"Sheet {sheet_name}, cell {cell_reference}" if sheet_name else f"Cell {cell_reference}"
        return {
            "location_kind": "cell",
            "location_label": label,
            "location_status": "exact",
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    if line_start is not None or line_end is not None:
        if line_start is not None and line_end is not None:
            label = f"Lines {line_start}-{line_end}"
            status = "exact"
        elif line_start is not None:
            label = f"Line {line_start}"
            status = "partial"
        else:
            label = f"Line {line_end}"
            status = "partial"
        if page_number is not None:
            label = f"Page {page_number}, {label.lower()}"
        return {
            "location_kind": "line",
            "location_label": label,
            "location_status": status,
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    if sheet_name is not None:
        label = f"Sheet {sheet_name}"
        if section_name is not None:
            label = f"{label}, section {section_name}"
        status = "exact" if page_number is not None and section_name is not None else "partial"
        return {
            "location_kind": "sheet",
            "location_label": label,
            "location_status": status,
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    if section_name is not None:
        return {
            "location_kind": "section",
            "location_label": f"Section {section_name}",
            "location_status": "approximate",
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    if page_number is not None:
        if isinstance(element_type, str) and element_type == "image":
            return {
                "location_kind": "image",
                "location_label": f"Image on page {page_number}",
                "location_status": "partial",
                "page_number": page_number,
                "slide_number": slide_number,
                "sheet_name": sheet_name,
                "line_start": line_start,
                "line_end": line_end,
                "cell_reference": cell_reference,
                "section_name": section_name,
            }
        return {
            "location_kind": "page",
            "location_label": f"Page {page_number}",
            "location_status": "partial",
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    if slide_number is not None:
        return {
            "location_kind": "slide",
            "location_label": f"Slide {slide_number}",
            "location_status": "partial",
            "page_number": page_number,
            "slide_number": slide_number,
            "sheet_name": sheet_name,
            "line_start": line_start,
            "line_end": line_end,
            "cell_reference": cell_reference,
            "section_name": section_name,
        }

    return {
        "location_kind": "unknown",
        "location_label": "Location unavailable",
        "location_status": "unavailable",
        "page_number": page_number,
        "slide_number": slide_number,
        "sheet_name": sheet_name,
        "line_start": line_start,
        "line_end": line_end,
        "cell_reference": cell_reference,
        "section_name": section_name,
    }


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _project_citations(
    citations: Sequence[Mapping[str, object]],
) -> list[GovernedSynthesisCitation]:
    projected: list[GovernedSynthesisCitation] = []
    for item in citations:
        citation_index = item.get("citation_index")
        if not isinstance(citation_index, int):
            raise SynthesisContextError(
                error_code="response_synthesis_failed",
                message="Governed citation payload is missing a valid citation index.",
                reason_code="invalid_synthesis_citation_payload",
            )
        projected.append(
            {
                "citation_index": citation_index,
                "source_id": str(item["source_id"]),
                "source_version_id": str(item["source_version_id"]),
                "anchor_id": str(item["anchor_id"]),
                "title": str(item["title"]),
                "url": str(item["url"]),
                "authority_level": str(item["authority_level"]),
                "temporal_applicability": str(item["temporal_applicability"]),
            }
        )
    return projected


def _build_plan_summary(
    *,
    plan: Mapping[str, object],
    selected_route: Mapping[str, object] | None,
    step_summary: Mapping[str, object] | None,
) -> dict[str, object]:
    steps_value = plan.get("steps")
    step_count = len(cast(list[object], steps_value)) if isinstance(steps_value, list) else 0
    return {
        "plan_id": plan.get("plan_id"),
        "plan_status": plan.get("plan_status"),
        "planning_mode": plan.get("planning_mode"),
        "execution_ready": plan.get("execution_ready"),
        "step_count": step_count,
        "selected_route": (dict(selected_route) if selected_route is not None else None),
        "step_summary": (dict(step_summary) if step_summary is not None else None),
    }


def _build_computation_summary(
    *,
    answer_mode: AnswerMode,
    mapped_result: Mapping[str, object],
    final_outcome: Mapping[str, object],
    adapter_response: Mapping[str, object] | None,
    step_results: Sequence[Mapping[str, object]] | None,
) -> dict[str, object] | None:
    if answer_mode not in {"compute_execution", "compute_plus_grounding"}:
        return None
    if answer_mode == "compute_plus_grounding":
        compute_step = _find_step_result(step_results, target_service="tax_core")
        if compute_step is None:
            return {
                "action_status": mapped_result.get("action_status"),
                "reason_code": mapped_result.get("reason_code"),
                "message": final_outcome.get("message"),
            }
        compute_mapped_result = compute_step.get("mapped_result")
        compute_adapter_response = compute_step.get("adapter_response")
        compute_mapped_result_map = (
            cast(Mapping[str, object], compute_mapped_result)
            if isinstance(compute_mapped_result, Mapping)
            else None
        )
        compute_adapter_response_map = (
            cast(Mapping[str, object], compute_adapter_response)
            if isinstance(compute_adapter_response, Mapping)
            else None
        )
        return {
            "action_status": (
                compute_mapped_result_map.get("action_status")
                if compute_mapped_result_map is not None
                else None
            ),
            "reason_code": (
                compute_mapped_result_map.get("reason_code")
                if compute_mapped_result_map is not None
                else None
            ),
            "message": (
                compute_adapter_response_map.get("message")
                if compute_adapter_response_map is not None
                else final_outcome.get("message")
            ),
        }
    return {
        "action_status": mapped_result.get("action_status"),
        "reason_code": mapped_result.get("reason_code"),
        "message": (
            adapter_response.get("message")
            if isinstance(adapter_response, Mapping)
            else final_outcome.get("message")
        ),
    }


def _find_step_result(
    step_results: Sequence[Mapping[str, object]] | None,
    *,
    target_service: str,
) -> Mapping[str, object] | None:
    if step_results is None:
        return None
    for step in step_results:
        if step.get("target_service") == target_service:
            return step
    return None


def _build_assumptions_and_warnings(
    *,
    answer_mode: AnswerMode,
    mapped_result: Mapping[str, object],
    normalized_grounded_evidence: Sequence[Mapping[str, object]],
    temporal_applicability: Mapping[str, object] | None,
    conversation_context_summary: Mapping[str, object] | None,
) -> tuple[list[str], list[str]]:
    assumptions: list[str] = []
    warnings: list[str] = []
    action_status = str(mapped_result.get("action_status", "unknown"))
    if action_status == "pending":
        warnings.append(
            "The downstream tool result is pending and not yet a final settled outcome."
        )
    if answer_mode == "grounded_knowledge":
        warnings.append("This answer is limited to the cited governed knowledge evidence.")
    if answer_mode == "compute_plus_grounding":
        warnings.append(
            "Any legal basis in this answer is limited to the cited governed knowledge evidence."
        )
    if answer_mode == "forms_execution":
        assumptions.append("Form artifact details are limited to governed orchestration output.")
    if answer_mode == "reports_execution":
        assumptions.append("Report artifact details are limited to governed orchestration output.")
    if answer_mode == "document_extraction":
        warnings.append(
            "Document extraction details are limited to the queued governed extraction payload."
        )
    if answer_mode in {"grounded_knowledge", "compute_plus_grounding"} and temporal_applicability:
        disclosure_text = temporal_applicability.get("disclosure_text")
        if isinstance(disclosure_text, str) and disclosure_text:
            assumptions.append(disclosure_text)
    if (
        answer_mode in {"grounded_knowledge", "compute_plus_grounding"}
        and normalized_grounded_evidence
    ):
        assumptions.append(
            "Citations are restricted to published governed evidence returned by orchestration."
        )
    if conversation_context_summary is not None:
        assumptions.append(
            "This answer reuses governed context from a prior execution in the same conversation."
        )
    return assumptions, warnings


def _build_service_result_summary(
    *,
    answer_mode: AnswerMode,
    mapped_result: Mapping[str, object],
    final_outcome: Mapping[str, object],
    adapter_response: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if answer_mode not in {
        "forms_execution",
        "reports_execution",
        "document_extraction",
    }:
        return None
    if not isinstance(adapter_response, Mapping):
        raise SynthesisContextError(
            error_code="response_synthesis_failed",
            message="Governed service answer synthesis requires adapter result payload context.",
            reason_code="insufficient_service_payload_for_synthesis",
            context={"answer_mode": answer_mode},
        )
    raw_result_payload = adapter_response.get("result_payload")
    if not isinstance(raw_result_payload, Mapping):
        raise SynthesisContextError(
            error_code="response_synthesis_failed",
            message="Governed service answer synthesis requires a structured result payload.",
            reason_code="insufficient_service_payload_for_synthesis",
            context={"answer_mode": answer_mode},
        )
    result_payload = cast(Mapping[str, object], raw_result_payload)
    action_status = mapped_result.get("action_status")
    if answer_mode == "forms_execution":
        if request_missing_all(result_payload, "artifact_id", "form_ready_reference"):
            raise SynthesisContextError(
                error_code="response_synthesis_failed",
                message="Forms answer synthesis requires an artifact or form-ready reference.",
                reason_code="missing_service_artifact_reference",
                context={"answer_mode": answer_mode},
            )
        return {
            "status": result_payload.get("status", result_payload.get("mapping_status")),
            "generation_status": result_payload.get(
                "generation_status",
                result_payload.get("mapping_status"),
            ),
            "artifact_id": result_payload.get("artifact_id"),
            "form_ready_reference": result_payload.get("form_ready_reference"),
            "form_type": result_payload.get("form_type"),
            "form_version_id": result_payload.get(
                "form_version_id",
                result_payload.get("form_version"),
            ),
            "tax_year": result_payload.get("tax_year"),
            "supported_lane_id": result_payload.get("supported_lane_id"),
            "historical_version_id": result_payload.get("historical_version_id"),
            "action_status": action_status,
        }
    if answer_mode == "reports_execution":
        if request_missing_all(result_payload, "report_id"):
            raise SynthesisContextError(
                error_code="response_synthesis_failed",
                message="Reports answer synthesis requires a governed report identifier.",
                reason_code="missing_service_artifact_reference",
                context={"answer_mode": answer_mode},
            )
        return {
            "status": result_payload.get("status"),
            "report_id": result_payload.get("report_id"),
            "report_type": result_payload.get("report_type"),
            "report_version_id": result_payload.get("report_version_id"),
            "tax_year": result_payload.get("tax_year"),
            "artifact_metadata": (
                dict(
                    cast(
                        Mapping[str, object],
                        result_payload.get("artifact_metadata"),
                    )
                )
                if isinstance(result_payload.get("artifact_metadata"), Mapping)
                else None
            ),
            "action_status": action_status,
        }
    if request_missing_all(result_payload, "document_id", "evidence", "candidates"):
        raise SynthesisContextError(
            error_code="response_synthesis_failed",
            message=(
                "Document evidence answer synthesis requires a document reference or evidence result."
            ),
            reason_code="missing_service_artifact_reference",
            context={"answer_mode": answer_mode},
        )
    return {
        "status": result_payload.get("status"),
        "document_id": result_payload.get("document_id"),
        "lifecycle_status": result_payload.get("lifecycle_status"),
        "operation": result_payload.get("operation"),
        "evidence": result_payload.get("evidence", result_payload.get("candidates")),
        "evidence_limitations": result_payload.get("evidence_limitations", []),
        "action_status": action_status,
        "message": final_outcome.get("message"),
    }


def request_missing_all(result_payload: Mapping[str, object], *field_names: str) -> bool:
    return all(result_payload.get(field_name) in (None, "") for field_name in field_names)
