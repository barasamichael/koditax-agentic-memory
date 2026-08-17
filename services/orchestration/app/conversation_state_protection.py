"""Encrypt sensitive taxpayer facts before conversation-state persistence."""

from __future__ import annotations

import os
import json
import base64
from typing import cast
from typing import Literal
from typing import Protocol
from collections.abc import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts

_ENCRYPTION_KEY_ENV_VAR = "ORCHESTRATION_CONVERSATION_STATE_AES256_KEY"
_AAD = b"kodi-orchestration-conversation-stated-facts-v1"


class ConversationStateProtectionError(RuntimeError):
    """Represent an unavailable or failed sensitive-state protection operation."""


class ConversationStateProtector(Protocol):
    """Describe protection for sensitive fields persisted in conversation state."""

    def protect(self, stated_facts: ExtractedTaxpayerFacts) -> dict[str, str]:
        """Encrypt stated taxpayer facts for persistence."""
        ...

    def unprotect(self, protected_stated_facts: Mapping[str, object]) -> ExtractedTaxpayerFacts:
        """Decrypt one validated stated-facts envelope."""
        ...


class LocalAesGcmConversationStateProtector:
    """Protect stated facts with an injected AES-256-GCM key."""

    def __init__(self, *, key: bytes) -> None:
        if len(key) != 32:
            raise ConversationStateProtectionError("Conversation-state AES key must be 32 bytes.")
        self._cipher = AESGCM(key)

    def protect(self, stated_facts: ExtractedTaxpayerFacts) -> dict[str, str]:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            stated_facts,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = self._cipher.encrypt(nonce, plaintext, _AAD)
        return {
            "algorithm": "AES-256-GCM",
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }

    def unprotect(self, protected_stated_facts: Mapping[str, object]) -> ExtractedTaxpayerFacts:
        algorithm = protected_stated_facts.get("algorithm")
        nonce = protected_stated_facts.get("nonce")
        ciphertext = protected_stated_facts.get("ciphertext")
        if (
            algorithm != "AES-256-GCM"
            or not isinstance(nonce, str)
            or not isinstance(ciphertext, str)
        ):
            raise ConversationStateProtectionError("Conversation-state facts envelope is invalid.")
        try:
            plaintext = self._cipher.decrypt(_decode(nonce), _decode(ciphertext), _AAD)
            payload: object = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConversationStateProtectionError(
                "Conversation-state facts envelope cannot be decrypted."
            ) from error
        return _validated_stated_facts(payload)


def build_default_conversation_state_protector() -> ConversationStateProtector | None:
    """Build local AES protection only when valid runtime key material is configured."""

    encoded_key = os.getenv(_ENCRYPTION_KEY_ENV_VAR)
    if not encoded_key:
        return None
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except ValueError as error:
        raise ConversationStateProtectionError(
            "Conversation-state AES key must be valid base64."
        ) from error
    return LocalAesGcmConversationStateProtector(key=key)


def protect_stated_facts(
    *,
    stated_facts: ExtractedTaxpayerFacts,
    protector: ConversationStateProtector | None,
) -> dict[str, object]:
    """Return an encrypted envelope or no value when no facts were captured."""

    if not _contains_sensitive_fact(stated_facts):
        return {}
    if protector is None:
        raise ConversationStateProtectionError(
            "Sensitive conversation state cannot be persisted without AES-256 protection."
        )
    return cast(dict[str, object], protector.protect(stated_facts))


def unprotect_stated_facts(
    *,
    protected_stated_facts: object,
    protector: ConversationStateProtector | None,
) -> ExtractedTaxpayerFacts | None:
    """Return validated facts from protected state, or no facts when unavailable."""

    if not isinstance(protected_stated_facts, Mapping) or not protected_stated_facts:
        return None
    if protector is None:
        return None
    try:
        return protector.unprotect(cast(Mapping[str, object], protected_stated_facts))
    except ConversationStateProtectionError:
        return None


def _contains_sensitive_fact(stated_facts: ExtractedTaxpayerFacts) -> bool:
    return any(
        stated_facts.get(field) is not None
        for field in (
            "income_amount_kes",
            "income_frequency",
            "turnover_amount_kes",
            "residency_status",
            "filing_status",
        )
    )


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ConversationStateProtectionError(
            "Conversation-state facts envelope contains invalid base64."
        ) from error


def _validated_stated_facts(payload: object) -> ExtractedTaxpayerFacts:
    if not isinstance(payload, Mapping):
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    normalized_payload = cast(Mapping[str, object], payload)
    facts: ExtractedTaxpayerFacts = {}
    for field_name in ("income_amount_kes", "turnover_amount_kes"):
        value = normalized_payload.get(field_name)
        if value is not None and not isinstance(value, int | float):
            raise ConversationStateProtectionError(
                "Decrypted conversation-state facts are invalid."
            )
        facts[field_name] = float(value) if isinstance(value, int | float) else None
    income_frequency = normalized_payload.get("income_frequency")
    if income_frequency not in {None, "monthly", "annual"}:
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    facts["income_frequency"] = cast(Literal["monthly", "annual"] | None, income_frequency)
    residency_status = normalized_payload.get("residency_status")
    if residency_status not in {None, "resident", "non_resident"}:
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    facts["residency_status"] = cast(
        Literal["resident", "non_resident"] | None,
        residency_status,
    )
    filing_status = normalized_payload.get("filing_status")
    if filing_status is not None and not isinstance(filing_status, str):
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    facts["filing_status"] = filing_status
    confidence = normalized_payload.get("confidence_per_field")
    if not isinstance(confidence, Mapping):
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    confidence_mapping = cast(Mapping[object, object], confidence)
    if any(
        not isinstance(key, str) or not isinstance(value, int | float)
        for key, value in confidence_mapping.items()
    ):
        raise ConversationStateProtectionError("Decrypted conversation-state facts are invalid.")
    facts["confidence_per_field"] = {
        str(key): float(cast(int | float, value)) for key, value in confidence_mapping.items()
    }
    return facts
