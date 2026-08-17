"""Immutable correction events and effective-value resolution for Document AI."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class EffectiveValue:
    """The immutable observation plus the current consumer-facing interpretation."""

    target_id: str
    source_observed_value: object
    original_interpreted_value: object
    corrected_value: object | None
    effective_value: object
    correction_state: str


@dataclass(frozen=True)
class CorrectionEvent:
    """An append-only, actor-attributed correction or reversal event."""

    correction_id: UUID
    tenant_id: str
    target_id: str
    corrected_value: object | None
    actor_id: UUID
    reason: str
    created_at: str
    state: str
    supersedes_correction_id: UUID | None = None


@dataclass(frozen=True)
class CorrectionResult:
    correction_id: UUID
    state: str
    invalidated: set[str]


class CorrectionError(ValueError):
    """A deterministic correction-boundary error."""


class EffectiveCorrectionStore:
    """Small authoritative model used by correction consumers and tests.

    Production persistence is supplied by migration 0045; this deterministic store
    deliberately makes reprocessing update observations only when no correction is
    active, so provider retries cannot erase an authorized correction.
    """

    def __init__(self) -> None:
        self._observations: dict[tuple[str, str], tuple[object, object]] = {}
        self._events: list[CorrectionEvent] = []
        self._idempotency: dict[tuple[str, str], CorrectionResult] = {}

    def register_observation(
        self,
        *,
        tenant_id: str,
        target_id: str,
        source_observed_value: object,
        original_interpreted_value: object,
    ) -> None:
        key = (tenant_id, target_id)
        if key not in self._observations:
            self._observations[key] = (source_observed_value, original_interpreted_value)

    def correct(
        self,
        *,
        tenant_id: str,
        target_id: str,
        corrected_value: object,
        actor_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> CorrectionResult:
        previous = self._idempotency.get((tenant_id, idempotency_key))
        if previous is not None:
            return previous
        self._require_target(tenant_id=tenant_id, target_id=target_id)
        event = CorrectionEvent(
            correction_id=uuid4(),
            tenant_id=tenant_id,
            target_id=target_id,
            corrected_value=corrected_value,
            actor_id=actor_id,
            reason=reason,
            created_at=_utc_now_iso(),
            state="active",
            supersedes_correction_id=self._active_id(tenant_id, target_id),
        )
        self._events.append(event)
        result = CorrectionResult(event.correction_id, event.state, _dependency_closure(target_id))
        self._idempotency[(tenant_id, idempotency_key)] = result
        return result

    def reverse(
        self,
        *,
        tenant_id: str,
        correction_id: UUID,
        actor_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> CorrectionResult:
        previous = self._idempotency.get((tenant_id, idempotency_key))
        if previous is not None:
            return previous
        original = next(
            (
                event
                for event in self._events
                if event.correction_id == correction_id and event.tenant_id == tenant_id
            ),
            None,
        )
        if original is None:
            raise CorrectionError("correction_target_not_found")
        event = CorrectionEvent(
            uuid4(),
            tenant_id,
            original.target_id,
            None,
            actor_id,
            reason,
            _utc_now_iso(),
            "reversed",
            correction_id,
        )
        self._events.append(event)
        result = CorrectionResult(
            event.correction_id, event.state, _dependency_closure(original.target_id)
        )
        self._idempotency[(tenant_id, idempotency_key)] = result
        return result

    def resolve(self, *, tenant_id: str, target_id: str) -> EffectiveValue:
        source, original = self._require_target(tenant_id=tenant_id, target_id=target_id)
        active = self._active_event(tenant_id, target_id)
        corrected = active.corrected_value if active is not None else None
        return EffectiveValue(
            target_id,
            source,
            original,
            corrected,
            original if active is None else corrected,
            "corrected" if active else "original",
        )

    def history(self, *, tenant_id: str, target_id: str) -> tuple[CorrectionEvent, ...]:
        return tuple(
            event
            for event in self._events
            if event.tenant_id == tenant_id and event.target_id == target_id
        )

    def reprocess(self, *, tenant_id: str, observations: dict[str, tuple[object, object]]) -> None:
        for target_id, observation in observations.items():
            key = (tenant_id, target_id)
            if key not in self._observations:
                self._observations[key] = observation

    def _require_target(self, *, tenant_id: str, target_id: str) -> tuple[object, object]:
        try:
            return self._observations[(tenant_id, target_id)]
        except KeyError as error:
            raise CorrectionError("correction_target_not_found") from error

    def _active_id(self, tenant_id: str, target_id: str) -> UUID | None:
        active = self._active_event(tenant_id, target_id)
        return None if active is None else active.correction_id

    def _active_event(self, tenant_id: str, target_id: str) -> CorrectionEvent | None:
        events = [
            event
            for event in self._events
            if event.tenant_id == tenant_id and event.target_id == target_id
        ]
        reversed_ids = {
            event.supersedes_correction_id for event in events if event.state == "reversed"
        }
        return next(
            (
                event
                for event in reversed(events)
                if event.state == "active" and event.correction_id not in reversed_ids
            ),
            None,
        )


def _dependency_closure(target_id: str) -> set[str]:
    return {
        f"{kind}:{target_id}"
        for kind in ("effective_canonical", "chunk", "embedding", "evidence", "projection", "cache")
    }


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
