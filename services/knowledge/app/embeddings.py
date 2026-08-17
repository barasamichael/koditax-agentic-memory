"""OpenAI-backed embedding utilities for governed hybrid knowledge retrieval."""

from __future__ import annotations

from typing import cast
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Sequence

import httpx

from services.knowledge.app.config import get_knowledge_openai_api_key
from services.knowledge.app.config import get_knowledge_openai_base_url
from services.knowledge.app.config import get_knowledge_openai_embedding_model
from services.knowledge.app.config import get_knowledge_openai_embedding_dimensions
from services.knowledge.app.config import get_knowledge_openai_embedding_timeout_seconds


class KnowledgeEmbeddingProviderError(RuntimeError):
    """Represent deterministic embedding-provider failures."""


class KnowledgeEmbeddingProvider(Protocol):
    """Describe the embedding operations required by governed hybrid retrieval."""

    @property
    def model_name(self) -> str:
        ...

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        ...


@dataclass(frozen=True)
class OpenAIKnowledgeEmbeddingProviderConfig:
    """Runtime configuration for the OpenAI embedding provider."""

    api_key: str
    base_url: str
    model_name: str
    timeout_seconds: float
    dimensions: int | None


class OpenAIKnowledgeEmbeddingProvider:
    """Generate embeddings from OpenAI for governed hybrid ranking."""

    def __init__(
        self,
        *,
        config: OpenAIKnowledgeEmbeddingProviderConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_config = _default_openai_embedding_provider_config() if config is None else config
        self._config = resolved_config
        self._client = client

    @property
    def model_name(self) -> str:
        """Return the configured embedding model."""

        return self._config.model_name

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return embeddings for the provided text inputs in stable input order."""

        normalized_texts = tuple(text.strip() for text in texts if text.strip())
        if not normalized_texts:
            return ()

        payload: dict[str, object] = {
            "model": self._config.model_name,
            "input": list(normalized_texts),
            "encoding_format": "float",
        }
        if self._config.dimensions is not None:
            payload["dimensions"] = self._config.dimensions
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

        if self._client is None:
            with httpx.Client(
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
            ) as client:
                response = client.post("/embeddings", headers=headers, json=payload)
        else:
            response = self._client.post("/embeddings", headers=headers, json=payload)

        if response.status_code >= 400:
            raise KnowledgeEmbeddingProviderError(
                "OpenAI embeddings request failed for governed hybrid retrieval."
            )

        try:
            response_payload = response.json()
        except ValueError as error:
            raise KnowledgeEmbeddingProviderError(
                "OpenAI embeddings response was not valid JSON."
            ) from error

        if not isinstance(response_payload, dict):
            raise KnowledgeEmbeddingProviderError("OpenAI embeddings response shape was invalid.")
        response_mapping = cast(dict[object, object], response_payload)
        data = response_mapping.get("data")
        if not isinstance(data, list):
            raise KnowledgeEmbeddingProviderError(
                "OpenAI embeddings response omitted embedding data."
            )

        indexed_vectors: list[tuple[int, tuple[float, ...]]] = []
        for item in cast(list[object], data):
            indexed_vectors.append(_parse_embedding_item(item))
        indexed_vectors.sort(key=lambda item: item[0])
        return tuple(vector for _, vector in indexed_vectors)


def build_default_knowledge_embedding_provider() -> KnowledgeEmbeddingProvider | None:
    """Return the default OpenAI embedding provider when configured."""

    api_key = get_knowledge_openai_api_key()
    if api_key is None:
        return None
    return OpenAIKnowledgeEmbeddingProvider(
        config=OpenAIKnowledgeEmbeddingProviderConfig(
            api_key=api_key,
            base_url=get_knowledge_openai_base_url(),
            model_name=get_knowledge_openai_embedding_model(),
            timeout_seconds=get_knowledge_openai_embedding_timeout_seconds(),
            dimensions=get_knowledge_openai_embedding_dimensions(),
        )
    )


def cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Return cosine similarity for two same-length vectors."""

    if len(left) != len(right) or not left:
        return 0.0
    dot_product = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot_product += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot_product / ((left_norm**0.5) * (right_norm**0.5))


def _default_openai_embedding_provider_config() -> OpenAIKnowledgeEmbeddingProviderConfig:
    api_key = get_knowledge_openai_api_key()
    if api_key is None:
        raise KnowledgeEmbeddingProviderError(
            "OPENAI_API_KEY must be configured before OpenAI embeddings can be used."
        )
    return OpenAIKnowledgeEmbeddingProviderConfig(
        api_key=api_key,
        base_url=get_knowledge_openai_base_url(),
        model_name=get_knowledge_openai_embedding_model(),
        timeout_seconds=get_knowledge_openai_embedding_timeout_seconds(),
        dimensions=get_knowledge_openai_embedding_dimensions(),
    )


def _parse_embedding_item(item: object) -> tuple[int, tuple[float, ...]]:
    if not isinstance(item, dict):
        raise KnowledgeEmbeddingProviderError("OpenAI embeddings response item was invalid.")
    raw_mapping = cast(dict[object, object], item)
    index = raw_mapping.get("index")
    embedding = raw_mapping.get("embedding")
    if not isinstance(index, int) or isinstance(index, bool):
        raise KnowledgeEmbeddingProviderError(
            "OpenAI embeddings response omitted a valid item index."
        )
    if not isinstance(embedding, list):
        raise KnowledgeEmbeddingProviderError(
            "OpenAI embeddings response omitted a valid embedding vector."
        )
    vector: list[float] = []
    for value in cast(list[object], embedding):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            vector.append(float(value))
        else:
            raise KnowledgeEmbeddingProviderError(
                "OpenAI embeddings response contained a non-numeric vector value."
            )
    return (index, tuple(vector))
