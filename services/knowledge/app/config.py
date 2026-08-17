"""Knowledge runtime configuration constants."""

from __future__ import annotations

import os

KNOWLEDGE_SERVICE_NAME = "knowledge"
KNOWLEDGE_SERVICE_VERSION = "0.1.0"
KNOWLEDGE_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
KNOWLEDGE_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
KNOWLEDGE_OPENAI_EMBEDDING_MODEL_ENV_VAR = "KNOWLEDGE_OPENAI_EMBEDDING_MODEL"
KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS_ENV_VAR = "KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS"
KNOWLEDGE_OPENAI_EMBEDDING_DIMENSIONS_ENV_VAR = "KNOWLEDGE_OPENAI_EMBEDDING_DIMENSIONS"
KNOWLEDGE_HYBRID_VECTOR_WEIGHT_ENV_VAR = "KNOWLEDGE_HYBRID_VECTOR_WEIGHT"
KNOWLEDGE_HYBRID_LEXICAL_WEIGHT_ENV_VAR = "KNOWLEDGE_HYBRID_LEXICAL_WEIGHT"
KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY_ENV_VAR = "KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY"
DEFAULT_KNOWLEDGE_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS = 15.0
DEFAULT_KNOWLEDGE_HYBRID_VECTOR_WEIGHT = 0.55
DEFAULT_KNOWLEDGE_HYBRID_LEXICAL_WEIGHT = 0.45
DEFAULT_KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY = 0.2


def get_knowledge_openai_api_key() -> str | None:
    """Return the configured OpenAI API key for governed hybrid retrieval."""

    value = os.getenv(KNOWLEDGE_OPENAI_API_KEY_ENV_VAR)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def get_knowledge_openai_base_url() -> str:
    """Return the configured OpenAI base URL for embeddings."""

    value = os.getenv(KNOWLEDGE_OPENAI_BASE_URL_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_OPENAI_BASE_URL
    return value.strip().rstrip("/")


def get_knowledge_openai_embedding_model() -> str:
    """Return the configured OpenAI embedding model name."""

    value = os.getenv(KNOWLEDGE_OPENAI_EMBEDDING_MODEL_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_MODEL
    return value.strip()


def get_knowledge_openai_embedding_timeout_seconds() -> float:
    """Return the configured timeout for OpenAI embedding requests."""

    value = os.getenv(KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS
    try:
        timeout = float(value.strip())
    except ValueError:
        return DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS
    if timeout > 0:
        return timeout
    return DEFAULT_KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS


def get_knowledge_openai_embedding_dimensions() -> int | None:
    """Return the configured embedding dimensionality override when supported."""

    value = os.getenv(KNOWLEDGE_OPENAI_EMBEDDING_DIMENSIONS_ENV_VAR)
    if value is None or not value.strip():
        return None
    try:
        dimensions = int(value.strip())
    except ValueError:
        return None
    if dimensions > 0:
        return dimensions
    return None


def get_knowledge_hybrid_vector_weight() -> float:
    """Return the configured hybrid vector-scoring weight."""

    value = os.getenv(KNOWLEDGE_HYBRID_VECTOR_WEIGHT_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_HYBRID_VECTOR_WEIGHT
    try:
        weight = float(value.strip())
    except ValueError:
        return DEFAULT_KNOWLEDGE_HYBRID_VECTOR_WEIGHT
    if weight >= 0:
        return weight
    return DEFAULT_KNOWLEDGE_HYBRID_VECTOR_WEIGHT


def get_knowledge_hybrid_lexical_weight() -> float:
    """Return the configured hybrid lexical-scoring weight."""

    value = os.getenv(KNOWLEDGE_HYBRID_LEXICAL_WEIGHT_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_HYBRID_LEXICAL_WEIGHT
    try:
        weight = float(value.strip())
    except ValueError:
        return DEFAULT_KNOWLEDGE_HYBRID_LEXICAL_WEIGHT
    if weight >= 0:
        return weight
    return DEFAULT_KNOWLEDGE_HYBRID_LEXICAL_WEIGHT


def get_knowledge_hybrid_min_vector_similarity() -> float:
    """Return the minimum vector similarity that can surface a lexical miss."""

    value = os.getenv(KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY_ENV_VAR)
    if value is None or not value.strip():
        return DEFAULT_KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY
    try:
        similarity = float(value.strip())
    except ValueError:
        return DEFAULT_KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY
    if 0 <= similarity <= 1:
        return similarity
    return DEFAULT_KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY
