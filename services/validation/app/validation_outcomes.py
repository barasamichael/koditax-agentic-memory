"""Deterministic validation outcome models."""

from __future__ import annotations

from typing import Literal
from dataclasses import dataclass

ValidationMode = Literal["draft", "pre_submission", "post_submission_integrity"]
ValidationStatus = Literal["accepted", "rejected"]
IssueSeverity = Literal["ERROR", "WARNING", "INFO"]
RuleOutcome = Literal["passed", "failed", "not_applicable"]


@dataclass(frozen=True)
class ValidationIssue:
    """Represent one canonical validation issue."""

    severity: IssueSeverity
    code: str
    message: str
    field: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True)
class ValidationRuleResult:
    """Represent one deterministic rule evaluation result."""

    rule_code: str
    outcome: RuleOutcome
    severity: IssueSeverity
    message: str
    field: str | None
    linked_issue_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_code": self.rule_code,
            "outcome": self.outcome,
            "severity": self.severity,
            "message": self.message,
            "field": self.field,
            "linked_issue_codes": list(self.linked_issue_codes),
        }


@dataclass(frozen=True)
class ValidationSummary:
    """Represent a canonical summary of validation findings."""

    error_count: int
    warning_count: int
    info_count: int
    total_issues: int

    def to_dict(self) -> dict[str, int]:
        return {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "total_issues": self.total_issues,
        }


@dataclass(frozen=True)
class GovernedValidationEnvelope:
    """Represent one downstream-governed validation envelope."""

    workflow: str
    tax_domain: str
    validation_status: ValidationStatus
    summary: ValidationSummary
    issues: tuple[ValidationIssue, ...]
    rule_results: tuple[ValidationRuleResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "tax_domain": self.tax_domain,
            "validation_status": self.validation_status,
            "summary": self.summary.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "rule_results": [rule_result.to_dict() for rule_result in self.rule_results],
        }


@dataclass(frozen=True)
class ValidationAuditEvidence:
    """Represent one machine-consumable validation audit evidence envelope."""

    audit_event_id: str
    event_type: str
    event_time: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "audit_event_id": self.audit_event_id,
            "event_type": self.event_type,
            "event_time": self.event_time,
            "status": self.status,
        }


def build_summary(issues: tuple[ValidationIssue, ...]) -> ValidationSummary:
    """Build a deterministic issue summary."""

    return ValidationSummary(
        error_count=sum(1 for issue in issues if issue.severity == "ERROR"),
        warning_count=sum(1 for issue in issues if issue.severity == "WARNING"),
        info_count=sum(1 for issue in issues if issue.severity == "INFO"),
        total_issues=len(issues),
    )


def build_governed_validation_envelope(
    *,
    workflow: str,
    tax_domain: str,
    validation_status: ValidationStatus,
    issues: tuple[ValidationIssue, ...],
    rule_results: tuple[ValidationRuleResult, ...],
) -> GovernedValidationEnvelope:
    """Build one deterministic downstream-governed validation envelope."""

    return GovernedValidationEnvelope(
        workflow=workflow,
        tax_domain=tax_domain,
        validation_status=validation_status,
        summary=build_summary(issues),
        issues=issues,
        rule_results=rule_results,
    )
