"""Configuration helpers for orchestration runtime integrations."""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OPENAI_TIMEOUT_SECONDS = 20.0
_DEFAULT_SEMANTIC_EXTRACTION_TIMEOUT_SECONDS = 15.0
_DEFAULT_FOLLOWUP_CLASSIFICATION_TIMEOUT_SECONDS = 10.0
_DEFAULT_SELF_CRITIQUE_TIMEOUT_SECONDS = 15.0
_DEFAULT_TAVILY_TIMEOUT_SECONDS = 10.0
_DEFAULT_CONVERSATION_CONTEXT_CANDIDATE_LIMIT = 8
_DEFAULT_SYNTHESIS_ENABLED = True
_DEFAULT_CONVERSATION_CONTINUITY_ENABLED = True
_DEFAULT_ALLOW_DEGRADED_SAFE_RELEASE = True
_DEFAULT_REQUIRE_EXPLICIT_CANARY = True
_DEFAULT_SEMANTIC_EXTRACTION_ENABLED = False
_DEFAULT_WEB_SEARCH_FALLBACK_ENABLED = True
_DEFAULT_SELF_CRITIQUE_ENABLED = False


@dataclass(frozen=True)
class OrchestrationOpenAIResponseSynthesisConfig:
    """Represent bounded configuration for OpenAI-backed response synthesis."""

    api_key: str | None
    model: str | None
    base_url: str
    timeout_seconds: float
    max_retries: int
    reasoning_effort: str | None = None
    service_tier: str | None = None
    prompt_cache_retention: str | None = None
    prompt_cache_key_prefix: str = "orchestration-response-synthesis"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass(frozen=True)
class OrchestrationConversationContextConfig:
    """Represent bounded configuration for conversation-context reuse."""

    candidate_limit: int


@dataclass(frozen=True)
class OrchestrationRuntimeRolloutConfig:
    """Represent bounded orchestration rollout controls for degraded-safe operation."""

    response_synthesis_enabled: bool
    conversation_continuity_enabled: bool


@dataclass(frozen=True)
class OrchestrationReleaseControlConfig:
    """Represent deterministic release-gate controls for orchestration rollout."""

    allow_degraded_safe_release: bool
    require_explicit_canary: bool


@dataclass(frozen=True)
class SemanticPromptExtractionConfig:
    """Represent configuration for LLM-powered semantic context extraction."""

    api_key: str | None
    model: str | None
    base_url: str
    timeout_seconds: float
    max_retries: int
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.enabled)


@dataclass(frozen=True)
class FollowupClassificationConfig:
    """Represent configuration for LLM-powered follow-up conversation classification."""

    api_key: str | None
    model: str | None
    base_url: str
    timeout_seconds: float
    max_retries: int
    enabled: bool

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.enabled)


@dataclass(frozen=True)
class SelfCritiqueConfig:
    """Represent configuration for the non-blocking LLM self-critique pass."""

    api_key: str | None
    model: str | None
    base_url: str
    timeout_seconds: float
    max_retries: int
    enabled: bool

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.enabled)


@dataclass(frozen=True)
class TavilyWebSearchConfig:
    """Represent configuration for Tavily web search fallback."""

    api_key: str | None
    timeout_seconds: float
    max_results: int
    enabled: bool

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.enabled)


def load_orchestration_openai_response_synthesis_config() -> (
    OrchestrationOpenAIResponseSynthesisConfig
):
    """Load orchestration-local OpenAI synthesis configuration from environment."""

    api_key = _read_env(
        "ORCHESTRATION_OPENAI_API_KEY",
        fallback_key="OPENAI_API_KEY",
    )
    model = _read_env(
        "ORCHESTRATION_OPENAI_MODEL",
        fallback_key="OPENAI_MODEL",
    )
    base_url = (
        _read_env("ORCHESTRATION_OPENAI_BASE_URL") or _DEFAULT_OPENAI_BASE_URL
    )
    timeout_seconds = _read_positive_float(
        "ORCHESTRATION_OPENAI_TIMEOUT_SECONDS",
        default_value=_DEFAULT_OPENAI_TIMEOUT_SECONDS,
    )
    max_retries = _read_non_negative_int(
        "ORCHESTRATION_OPENAI_MAX_RETRIES",
        default_value=0,
    )
    reasoning_effort = _read_allowed_string(
        "ORCHESTRATION_OPENAI_REASONING_EFFORT",
        allowed_values={"none", "minimal", "low", "medium", "high"},
    )
    service_tier = _read_allowed_string(
        "ORCHESTRATION_OPENAI_SERVICE_TIER",
        allowed_values={"auto", "default", "flex", "scale", "priority"},
    )
    prompt_cache_retention = _read_allowed_string(
        "ORCHESTRATION_OPENAI_PROMPT_CACHE_RETENTION",
        allowed_values={"in_memory", "24h"},
        aliases={"in-memory": "in_memory"},
    )
    prompt_cache_key_prefix = (
        _read_env("ORCHESTRATION_OPENAI_PROMPT_CACHE_KEY_PREFIX")
        or "orchestration-response-synthesis"
    )
    return OrchestrationOpenAIResponseSynthesisConfig(
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        prompt_cache_retention=prompt_cache_retention,
        prompt_cache_key_prefix=prompt_cache_key_prefix,
    )


def load_orchestration_conversation_context_config() -> (
    OrchestrationConversationContextConfig
):
    """Load bounded conversation-context reuse settings from environment."""

    candidate_limit = _read_non_negative_int(
        "ORCHESTRATION_CONVERSATION_CONTEXT_CANDIDATE_LIMIT",
        default_value=_DEFAULT_CONVERSATION_CONTEXT_CANDIDATE_LIMIT,
    )
    if candidate_limit <= 0:
        candidate_limit = _DEFAULT_CONVERSATION_CONTEXT_CANDIDATE_LIMIT
    return OrchestrationConversationContextConfig(
        candidate_limit=candidate_limit
    )


def load_orchestration_runtime_rollout_config() -> (
    OrchestrationRuntimeRolloutConfig
):
    """Load bounded rollout controls for orchestration continuity and synthesis."""

    return OrchestrationRuntimeRolloutConfig(
        response_synthesis_enabled=_read_bool(
            "ORCHESTRATION_RESPONSE_SYNTHESIS_ENABLED",
            default_value=_DEFAULT_SYNTHESIS_ENABLED,
        ),
        conversation_continuity_enabled=_read_bool(
            "ORCHESTRATION_CONVERSATION_CONTINUITY_ENABLED",
            default_value=_DEFAULT_CONVERSATION_CONTINUITY_ENABLED,
        ),
    )


def load_orchestration_release_control_config() -> (
    OrchestrationReleaseControlConfig
):
    """Load deterministic release-control settings for orchestration rollout closure."""

    return OrchestrationReleaseControlConfig(
        allow_degraded_safe_release=_read_bool(
            "ORCHESTRATION_ALLOW_DEGRADED_SAFE_RELEASE",
            default_value=_DEFAULT_ALLOW_DEGRADED_SAFE_RELEASE,
        ),
        require_explicit_canary=_read_bool(
            "ORCHESTRATION_REQUIRE_EXPLICIT_CANARY",
            default_value=_DEFAULT_REQUIRE_EXPLICIT_CANARY,
        ),
    )


def load_semantic_prompt_extraction_config() -> SemanticPromptExtractionConfig:
    """Load configuration for LLM-powered semantic context extraction."""

    api_key = _read_env(
        "ORCHESTRATION_SEMANTIC_EXTRACTION_API_KEY",
        fallback_key="OPENAI_API_KEY",
    )
    model = _read_env(
        "ORCHESTRATION_SEMANTIC_EXTRACTION_MODEL",
        fallback_key="OPENAI_MODEL",
    )
    base_url = (
        _read_env("ORCHESTRATION_SEMANTIC_EXTRACTION_BASE_URL")
        or _DEFAULT_OPENAI_BASE_URL
    )
    timeout_seconds = _read_positive_float(
        "ORCHESTRATION_SEMANTIC_EXTRACTION_TIMEOUT_SECONDS",
        default_value=_DEFAULT_SEMANTIC_EXTRACTION_TIMEOUT_SECONDS,
    )
    max_retries = _read_non_negative_int(
        "ORCHESTRATION_SEMANTIC_EXTRACTION_MAX_RETRIES",
        default_value=0,
    )
    enabled = _read_bool(
        "ORCHESTRATION_SEMANTIC_EXTRACTION_ENABLED",
        default_value=_DEFAULT_SEMANTIC_EXTRACTION_ENABLED,
    )

    return SemanticPromptExtractionConfig(
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        enabled=enabled,
    )


def load_followup_classification_config() -> FollowupClassificationConfig:
    """Load configuration for LLM-powered follow-up conversation classification."""

    api_key = _read_env(
        "ORCHESTRATION_FOLLOWUP_CLASSIFICATION_API_KEY",
        fallback_key="OPENAI_API_KEY",
    )
    model = _read_env(
        "ORCHESTRATION_FOLLOWUP_CLASSIFICATION_MODEL",
        fallback_key="OPENAI_MODEL",
    )
    base_url = (
        _read_env("ORCHESTRATION_FOLLOWUP_CLASSIFICATION_BASE_URL")
        or _DEFAULT_OPENAI_BASE_URL
    )
    timeout_seconds = _read_positive_float(
        "ORCHESTRATION_FOLLOWUP_CLASSIFICATION_TIMEOUT_SECONDS",
        default_value=_DEFAULT_FOLLOWUP_CLASSIFICATION_TIMEOUT_SECONDS,
    )
    max_retries = _read_non_negative_int(
        "ORCHESTRATION_FOLLOWUP_CLASSIFICATION_MAX_RETRIES",
        default_value=0,
    )
    enabled = _read_bool(
        "ORCHESTRATION_FOLLOWUP_CLASSIFICATION_ENABLED",
        default_value=True,
    )
    return FollowupClassificationConfig(
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        enabled=enabled,
    )


def load_self_critique_config() -> SelfCritiqueConfig:
    """Load configuration for the non-blocking LLM self-critique pass."""

    api_key = _read_env(
        "ORCHESTRATION_SELF_CRITIQUE_API_KEY",
        fallback_key="OPENAI_API_KEY",
    )
    model = _read_env(
        "ORCHESTRATION_SELF_CRITIQUE_MODEL",
        fallback_key="OPENAI_MODEL",
    )
    base_url = (
        _read_env("ORCHESTRATION_SELF_CRITIQUE_BASE_URL")
        or _DEFAULT_OPENAI_BASE_URL
    )
    timeout_seconds = _read_positive_float(
        "ORCHESTRATION_SELF_CRITIQUE_TIMEOUT_SECONDS",
        default_value=_DEFAULT_SELF_CRITIQUE_TIMEOUT_SECONDS,
    )
    max_retries = _read_non_negative_int(
        "ORCHESTRATION_SELF_CRITIQUE_MAX_RETRIES",
        default_value=0,
    )
    enabled = _read_bool(
        "ORCHESTRATION_SELF_CRITIQUE_ENABLED",
        default_value=_DEFAULT_SELF_CRITIQUE_ENABLED,
    )
    return SelfCritiqueConfig(
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        enabled=enabled,
    )


def load_tavily_web_search_config() -> TavilyWebSearchConfig:
    """Load configuration for Tavily web search fallback."""

    api_key = _read_env("TAVILY_API_KEY")
    timeout_seconds = _read_positive_float(
        "TAVILY_TIMEOUT_SECONDS",
        default_value=_DEFAULT_TAVILY_TIMEOUT_SECONDS,
    )
    max_results = _read_non_negative_int(
        "TAVILY_MAX_RESULTS",
        default_value=5,
    )
    if max_results == 0:
        max_results = 5
    enabled = _read_bool(
        "TAVILY_WEB_SEARCH_ENABLED",
        default_value=_DEFAULT_WEB_SEARCH_FALLBACK_ENABLED,
    )

    return TavilyWebSearchConfig(
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        max_results=max_results,
        enabled=enabled,
    )


def _read_env(key: str, *, fallback_key: str | None = None) -> str | None:
    primary = os.getenv(key)
    if primary is not None and primary.strip():
        return primary.strip()
    if fallback_key is None:
        return None
    fallback = os.getenv(fallback_key)
    if fallback is not None and fallback.strip():
        return fallback.strip()
    return None


def _read_positive_float(key: str, *, default_value: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default_value
    try:
        resolved = float(raw)
    except ValueError:
        return default_value
    if resolved <= 0:
        return default_value
    return resolved


def _read_non_negative_int(key: str, *, default_value: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default_value
    try:
        resolved = int(raw)
    except ValueError:
        return default_value
    if resolved < 0:
        return default_value
    return resolved


def _read_bool(key: str, *, default_value: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default_value
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default_value


def _read_allowed_string(
    key: str,
    *,
    allowed_values: set[str],
    aliases: dict[str, str] | None = None,
) -> str | None:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip().lower()
    if aliases is not None:
        normalized = aliases.get(normalized, normalized)
    if normalized in allowed_values:
        return normalized
    return None
