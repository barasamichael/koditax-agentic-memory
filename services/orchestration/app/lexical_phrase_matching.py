"""Boundary-safe lexical phrase matching for deterministic orchestration heuristics."""

from __future__ import annotations

import re
import unicodedata
from typing import TypedDict


class PhraseMatch(TypedDict):
    """Represent one matched lexical phrase span in normalized text."""

    matched_phrase: str
    normalized_span: str
    start_index: int
    end_index: int


def normalize_lexical_text(text: str) -> str:
    """Normalize text for boundary-safe phrase matching."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def tokenize_lexical_text(text: str) -> list[tuple[str, int, int]]:
    """Return tokens and character spans from normalized lexical text."""

    return [
        (match.group(0), match.start(), match.end())
        for match in re.finditer(r"[a-z0-9]+", text)
    ]


def find_phrase_match(
    text: str,
    phrases: tuple[str, ...] | list[str],
) -> PhraseMatch | None:
    """Return the first boundary-safe phrase match found in text."""

    normalized_text = normalize_lexical_text(text)
    tokens = tokenize_lexical_text(normalized_text)
    if not tokens:
        return None

    normalized_phrases = [
        (phrase, tuple(token[0] for token in tokenize_lexical_text(normalize_lexical_text(phrase))))
        for phrase in phrases
    ]
    normalized_phrases.sort(key=lambda item: len(item[1]), reverse=True)

    token_values = [token for token, _, _ in tokens]
    for original_phrase, phrase_tokens in normalized_phrases:
        if not phrase_tokens:
            continue
        phrase_len = len(phrase_tokens)
        for start_index in range(0, len(token_values) - phrase_len + 1):
            if tuple(token_values[start_index : start_index + phrase_len]) != phrase_tokens:
                continue
            start_char = tokens[start_index][1]
            end_char = tokens[start_index + phrase_len - 1][2]
            return {
                "matched_phrase": original_phrase,
                "normalized_span": normalized_text[start_char:end_char],
                "start_index": start_char,
                "end_index": end_char,
            }
    return None


def contains_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    """Return whether any phrase matches with token-boundary safety."""

    return find_phrase_match(text, phrases) is not None
