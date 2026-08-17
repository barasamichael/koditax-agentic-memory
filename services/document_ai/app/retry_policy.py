"""Deterministic bounded retry policy for document_ai transient failures."""

from __future__ import annotations

from typing import cast
from typing import Generic
from typing import Literal
from typing import TypeVar
from dataclasses import dataclass
from collections.abc import Callable

from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.governed_openai import OpenAIProviderError
from services.document_ai.app.storage_adapter import StorageAdapterError
from services.document_ai.app.storage_adapter import StorageAdapterTransientError
from services.document_ai.app.source_inspection import SourceInspectionError

RetryOutcomeClass = Literal["success", "success_after_retry", "retry_exhausted", "hard_failure"]
RetryClassification = Literal["transient", "non_retryable"]
T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicyConfig:
    """Represent deterministic retry policy configuration."""

    max_attempts: int
    base_delay_ms: int
    backoff_multiplier: int
    retryable_reason_codes: frozenset[str]
    max_delay_ms: int = 60_000
    max_elapsed_ms: int = 15 * 60_000
    jitter_ratio: float = 0.20

    def delay_for_attempt(self, *, attempt_count: int) -> int:
        """Return deterministic backoff delay for the attempt that just failed."""

        if attempt_count < 1:
            return 0
        exponent = attempt_count - 1
        return min(self.max_delay_ms, self.base_delay_ms * (self.backoff_multiplier**exponent))

    def scheduled_delay_ms(
        self, *, attempt_count: int, retry_after_ms: int | None = None, jitter: float = 0.0
    ) -> int:
        """Return bounded deterministic retry delay; callers inject jitter for testability."""

        delay = self.delay_for_attempt(attempt_count=attempt_count)
        if retry_after_ms is not None and retry_after_ms > 0:
            delay = max(delay, min(retry_after_ms, self.max_delay_ms))
        bounded_jitter = max(-self.jitter_ratio, min(self.jitter_ratio, jitter))
        return min(self.max_delay_ms, max(1, round(delay * (1 + bounded_jitter))))


DEFAULT_DOCUMENT_AI_RETRY_POLICY = RetryPolicyConfig(
    max_attempts=3,
    base_delay_ms=100,
    backoff_multiplier=2,
    retryable_reason_codes=frozenset(
        {
            "upstream_timeout",
            "upstream_rate_limited",
            "upstream_unavailable",
            "storage_timeout",
            "storage_service_unavailable",
            "storage_retryable_failure",
        }
    ),
)


@dataclass(frozen=True)
class RetryClassifiedFailure:
    """Represent canonical deterministic failure classification."""

    classification: RetryClassification
    error_code: str
    message: str
    reason: str
    retryable: bool
    details: dict[str, object]

    @property
    def retry_after_ms(self) -> int | None:
        """Use a provider delay only when it is a positive integer and policy permits it."""

        value = self.details.get("retry_after_ms")
        return value if isinstance(value, int) and value > 0 else None


@dataclass(frozen=True)
class RetryExecutionResult(Generic[T]):
    """Represent deterministic retry execution outcome."""

    outcome_class: RetryOutcomeClass
    attempt_count: int
    value: T | None
    failure: RetryClassifiedFailure | None
    retry_schedule_ms: tuple[int, ...]


def classify_document_ai_failure(
    *,
    error: Exception,
    retry_policy: RetryPolicyConfig = DEFAULT_DOCUMENT_AI_RETRY_POLICY,
) -> RetryClassifiedFailure:
    """Classify one document_ai failure as retryable transient or non-retryable."""

    if isinstance(error, StorageAdapterError):
        return _classify_adapter_error(
            error=error,
            transient_type=StorageAdapterTransientError,
            retry_policy=retry_policy,
            transient_error_code="storage_retryable_failure",
            non_retryable_error_code="storage_non_retryable_failure",
        )
    if isinstance(error, SourceInspectionError):
        return RetryClassifiedFailure(
            classification="non_retryable",
            error_code="source_inspection_non_retryable_failure",
            message="Source inspection could not complete safely.",
            reason=str(error),
            retryable=False,
            details={},
        )
    if isinstance(error, OpenAIProviderError):
        return RetryClassifiedFailure(
            classification="transient" if error.retryable else "non_retryable",
            error_code=(
                "openai_retryable_failure" if error.retryable else "openai_non_retryable_failure"
            ),
            message=error.message,
            reason=error.reason,
            retryable=error.retryable,
            details={},
        )
    return RetryClassifiedFailure(
        classification="non_retryable",
        error_code="document_ai_non_retryable_failure",
        message=str(error),
        reason="unclassified_non_retryable_failure",
        retryable=False,
        details={},
    )


def execute_with_bounded_retry(
    *,
    operation: Callable[[], T],
    retry_policy: RetryPolicyConfig = DEFAULT_DOCUMENT_AI_RETRY_POLICY,
    classify_failure: Callable[[Exception], RetryClassifiedFailure] | None = None,
) -> RetryExecutionResult[T]:
    """Execute deterministic bounded retry for transient failures only."""

    def _default_classifier(error: Exception) -> RetryClassifiedFailure:
        return classify_document_ai_failure(error=error, retry_policy=retry_policy)

    classifier: Callable[[Exception], RetryClassifiedFailure] = (
        classify_failure if classify_failure is not None else _default_classifier
    )
    schedule: list[int] = []
    for attempt_count in range(1, retry_policy.max_attempts + 1):
        try:
            value = operation()
            return RetryExecutionResult(
                outcome_class="success" if attempt_count == 1 else "success_after_retry",
                attempt_count=attempt_count,
                value=value,
                failure=None,
                retry_schedule_ms=tuple(schedule),
            )
        except Exception as error:  # noqa: BLE001
            classified = classifier(error)
            if not classified.retryable:
                return RetryExecutionResult(
                    outcome_class="hard_failure",
                    attempt_count=attempt_count,
                    value=None,
                    failure=classified,
                    retry_schedule_ms=tuple(schedule),
                )
            if attempt_count >= retry_policy.max_attempts:
                return RetryExecutionResult(
                    outcome_class="retry_exhausted",
                    attempt_count=attempt_count,
                    value=None,
                    failure=classified,
                    retry_schedule_ms=tuple(schedule),
                )
            schedule.append(retry_policy.delay_for_attempt(attempt_count=attempt_count))
    raise RuntimeError("deterministic_retry_policy_unreachable")


def _classify_adapter_error(
    *,
    error: StorageAdapterError,
    transient_type: type[StorageAdapterTransientError],
    retry_policy: RetryPolicyConfig,
    transient_error_code: str,
    non_retryable_error_code: str,
) -> RetryClassifiedFailure:
    reason = error.reason
    is_transient_class = isinstance(error, transient_type)
    is_retryable_reason = reason in retry_policy.retryable_reason_codes
    retryable = is_transient_class and is_retryable_reason
    return RetryClassifiedFailure(
        classification="transient" if retryable else "non_retryable",
        error_code=transient_error_code if retryable else non_retryable_error_code,
        message=error.message,
        reason=reason,
        retryable=retryable,
        details=_normalize_details(error.details),
    )


def _normalize_details(details: object) -> dict[str, object]:
    redacted = redact_sensitive_fields(details)
    if isinstance(redacted, dict):
        redacted_map = cast(dict[str, object], redacted)
        normalized: dict[str, object] = {}
        for key in sorted(redacted_map):
            normalized[key] = redacted_map[key]
        return normalized
    return {}
