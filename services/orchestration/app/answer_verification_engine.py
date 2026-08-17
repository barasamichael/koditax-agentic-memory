"""Answer verification and fact-checking for response synthesis quality assurance."""

from __future__ import annotations

from typing import TypedDict

from openai import OpenAI

from services.orchestration.app.llm_response_contract import UnifiedAnswerCitationModel
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.knowledge_scope_reasoning import analyze_evidence_scope


class VerificationResult(TypedDict):
    """Result of answer verification."""

    is_verified: bool
    confidence_score: float
    issues_found: list[str]
    failed_checks: list[str]
    verification_type: str  # "citations", "consistency", "evidence_alignment"


class AnswerVerificationError(RuntimeError):
    """Represent answer verification failures."""

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


class AnswerVerificationEngine:
    """Verify generated answers against grounded evidence."""

    def __init__(self, *, openai_client: OpenAI | None = None) -> None:
        """Initialize verification engine."""
        self._client = openai_client

    def verify_answer(
        self,
        answer: UnifiedAnswerResponseModel,
        grounded_evidence: list[dict[str, object]],
        original_prompt: str,
        resolved_tax_domain: str | None = None,
        resolved_entity: str | None = None,
    ) -> VerificationResult:
        """Verify answer against grounded evidence and original prompt."""

        # Check 1: Verify citations are valid
        citation_issues = self._verify_citations(
            citations=answer.citations,
            grounded_evidence=grounded_evidence,
        )

        # Check 2: Verify logical consistency with evidence
        consistency_issues = self._verify_consistency(
            answer_text=answer.answer_text,
            grounded_evidence=grounded_evidence,
        )

        # Check 3: Verify evidence alignment (answer uses credible sources)
        alignment_issues = self._verify_evidence_alignment(
            answer_text=answer.answer_text,
            citations=answer.citations,
            grounded_evidence=grounded_evidence,
        )
        domain_issues = self._verify_domain_subject_alignment(
            citations=answer.citations,
            grounded_evidence=grounded_evidence,
            resolved_tax_domain=resolved_tax_domain,
            resolved_entity=resolved_entity,
        )

        all_issues = citation_issues + consistency_issues + alignment_issues + domain_issues
        failed_checks: list[str] = []
        if citation_issues:
            failed_checks.append("citation_validity")
        if consistency_issues:
            failed_checks.append("evidence_consistency")
        if alignment_issues:
            failed_checks.append("authority_currency_alignment")
        if domain_issues:
            failed_checks.append("domain_subject_alignment")

        # Compute confidence score (0.0-1.0)
        confidence = self._compute_confidence_score(
            citation_issues_count=len(citation_issues),
            consistency_issues_count=len(consistency_issues),
            alignment_issues_count=len(alignment_issues),
            total_evidence_count=len(grounded_evidence),
            total_citations_count=len(answer.citations),
        )
        # Domain consistency is a hard safety gate.  A real citation to the
        # wrong section is still the wrong answer, so it can never inherit a
        # high-confidence score from membership/authority checks.
        if domain_issues:
            confidence = min(confidence, 0.4)

        is_verified = confidence >= 0.6 and not domain_issues

        return VerificationResult(
            is_verified=is_verified,
            confidence_score=confidence,
            issues_found=all_issues,
            failed_checks=failed_checks,
            verification_type="composite",
        )

    def _verify_domain_subject_alignment(
        self,
        *,
        citations: list[UnifiedAnswerCitationModel],
        grounded_evidence: list[dict[str, object]],
        resolved_tax_domain: str | None,
        resolved_entity: str | None,
    ) -> list[str]:
        """Require cited passages—not labels—to match the requested subject."""
        if not resolved_tax_domain:
            return []
        cited_ids = {str(citation.source_id) for citation in citations if citation.source_id}
        candidate_evidence = [
            item for item in grounded_evidence
            if not cited_ids or str(item.get("source_id")) in cited_ids
        ]
        if not candidate_evidence:
            return ["No cited evidence is available to verify the requested tax domain"]
        for item in candidate_evidence:
            analysis = analyze_evidence_scope(
                item,
                tax_domain_hint=resolved_tax_domain,
                resolved_entity=resolved_entity,
            )
            if analysis["decision"] != "retained":
                requested_scope = resolved_tax_domain
                if resolved_entity:
                    requested_scope = f"{requested_scope}/{resolved_entity}"
                return [
                    "Cited evidence does not contain a passage matching the requested "
                    f"domain/entity ({requested_scope})"
                ]
        return []

    def _verify_citations(
        self,
        citations: list[UnifiedAnswerCitationModel],
        grounded_evidence: list[dict[str, object]],
    ) -> list[str]:
        """Verify that citations reference valid grounded evidence."""

        issues: list[str] = []

        # Extract evidence source IDs
        evidence_source_ids = {str(evidence.get("source_id")) for evidence in grounded_evidence}

        for citation in citations:
            source_id = str(citation.source_id) if citation.source_id else None
            if source_id and source_id not in evidence_source_ids:
                issues.append(f"Citation references unknown source: {source_id}")

        # Check for orphaned citations (answer references citations not in evidence)
        if not citations and grounded_evidence:
            issues.append("No citations provided despite available grounded evidence")

        return issues

    def _verify_consistency(
        self,
        answer_text: str | None,
        grounded_evidence: list[dict[str, object]],
    ) -> list[str]:
        """Verify answer is logically consistent with evidence."""

        issues: list[str] = []

        if not answer_text:
            issues.append("Answer text is empty or None")
            return issues

        answer_lower = answer_text.lower()

        # Check 1: Answer shouldn't claim certainty on uncertain topics
        uncertain_phrases = ["may", "might", "could", "possibly"]
        certain_phrases = ["definitely", "always", "never", "certainly"]

        has_uncertain_phrasing = any(phrase in answer_lower for phrase in uncertain_phrases)
        has_certain_phrasing = any(phrase in answer_lower for phrase in certain_phrases)

        if has_certain_phrasing and not has_uncertain_phrasing:
            # Check if evidence supports certainty
            if not self._evidence_supports_certainty(grounded_evidence):
                issues.append("Answer uses overly certain language given available evidence")

        # Check 2: Answer shouldn't contradict evidence domains
        for evidence in grounded_evidence:
            evidence_domain = str(evidence.get("tax_domain", "")).lower()
            if evidence_domain and evidence_domain not in answer_lower:
                # Soft check: if evidence is from specific domain, answer should mention it
                pass

        return issues

    def _verify_evidence_alignment(
        self,
        answer_text: str | None,
        citations: list[UnifiedAnswerCitationModel],
        grounded_evidence: list[dict[str, object]],
    ) -> list[str]:
        """Verify answer is well-grounded in authoritative sources."""

        issues: list[str] = []

        if not answer_text or not grounded_evidence:
            return issues

        # Check 1: Authority level of cited evidence
        cited_authority_levels = {citation.authority_level for citation in citations}

        # If only low-authority evidence is used, note it
        if cited_authority_levels and all(
            level in {"guidance", "commentary"} for level in cited_authority_levels
        ):
            issues.append("Answer relies only on guidance/commentary sources, not primary law")

        # Check 2: Currency of cited evidence (use temporal_applicability hint)
        expired_citations = [
            citation
            for citation in citations
            if "superseded" in citation.temporal_applicability.lower()
            or "expired" in citation.temporal_applicability.lower()
        ]

        if expired_citations:
            issues.append(f"Answer cites {len(expired_citations)} superseded or expired source(s)")

        # Check 3: Evidence diversity (avoid single-source answers on complex topics)
        answer_word_count = len(answer_text.split())
        unique_cited_sources = {citation.source_id for citation in citations}

        if answer_word_count > 100 and len(unique_cited_sources) < 2:
            issues.append("Complex answer relies on single source; consider multiple authorities")

        return issues

    def _evidence_supports_certainty(
        self,
        grounded_evidence: list[dict[str, object]],
    ) -> bool:
        """Check if evidence supports certain (vs uncertain) language."""

        if not grounded_evidence:
            return False

        # Count high-authority sources
        high_authority_count = sum(
            1
            for evidence in grounded_evidence
            if str(evidence.get("authority_level")).lower() in {"statute", "regulation"}
        )

        # If majority are high-authority and published, certainty is supported
        published_count = sum(
            1
            for evidence in grounded_evidence
            if str(evidence.get("publication_state")).lower() == "published"
        )

        total = len(grounded_evidence)
        return (high_authority_count >= total * 0.6) and (published_count >= total * 0.8)

    def _compute_confidence_score(
        self,
        citation_issues_count: int,
        consistency_issues_count: int,
        alignment_issues_count: int,
        total_evidence_count: int,
        total_citations_count: int,
    ) -> float:
        """Compute overall confidence score (0.0-1.0)."""

        # Base score starts at 1.0
        score = 1.0

        # Penalty for citation issues (0.3 weight)
        citation_penalty = min(citation_issues_count / max(1, total_citations_count), 1.0) * 0.3
        score -= citation_penalty

        # Penalty for consistency issues (0.4 weight)
        consistency_penalty = min(consistency_issues_count * 0.15, 0.4)
        score -= consistency_penalty

        # Penalty for alignment issues (0.3 weight)
        alignment_penalty = min(alignment_issues_count * 0.10, 0.3)
        score -= alignment_penalty

        # Bonus for high-evidence density (answer using multiple sources)
        if total_evidence_count > 0:
            evidence_bonus = min(total_citations_count / total_evidence_count * 0.1, 0.1)
            score += evidence_bonus

        # Ensure score stays in [0.0, 1.0]
        return max(0.0, min(1.0, score))


class AnswerRefinementRequest(TypedDict):
    """Request to refine an answer based on verification results."""

    original_answer: str
    issues: list[str]
    grounded_evidence: list[dict[str, object]]
    refinement_focus: str  # "citations", "consistency", "evidence"


def build_refinement_prompt(
    request: AnswerRefinementRequest,
) -> str:
    """Build a refinement prompt for LLM to improve verified answer."""

    issues_str = "\n".join(f"- {issue}" for issue in request["issues"])

    return (
        f"Your previous answer had the following verification issues:\n"
        f"{issues_str}\n\n"
        f"Original answer: {request['original_answer']}\n\n"
        f"Please revise the answer to address these issues while maintaining accuracy. "
        f"Focus on {request['refinement_focus']}. "
        f"Only use the provided grounded evidence. "
        f"Do not invent new information."
    )
