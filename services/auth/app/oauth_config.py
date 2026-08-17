"""
Deterministic OAuth/OIDC provider configuration and trust-policy validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse
from collections.abc import Mapping
from collections.abc import Sequence

from services.auth.app.config import get_auth_oauth_allowed_issuers
from services.auth.app.config import get_auth_oauth_required_scopes
from services.auth.app.config import get_auth_oauth_allowed_redirect_uris
from services.auth.app.config import get_auth_oauth_provider_registry_json

_CONFIG_ERROR_CODE = "oauth_provider_configuration_error"


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Represent one governed OAuth/OIDC provider configuration entry."""

    provider_id: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_id: str
    client_secret_ref: str
    redirect_uri: str
    scopes: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class OAuthProviderTrustPolicy:
    """Represent deterministic trust-policy constraints for providers."""

    allowed_issuers: frozenset[str]
    allowed_redirect_uris: frozenset[str]
    required_scopes: frozenset[str]


class OAuthProviderConfigError(ValueError):
    """Represent deterministic OAuth provider configuration failure."""

    def __init__(
        self,
        *,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = _CONFIG_ERROR_CODE
        self.message = message
        self.reason = reason
        self.details = details or {}

    def to_error_envelope(self) -> dict[str, object]:
        """Return canonical deterministic config error envelope."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "details": self.details,
        }


def get_default_oauth_provider_trust_policy() -> OAuthProviderTrustPolicy:
    """Build trust policy from deterministic configuration settings."""

    return OAuthProviderTrustPolicy(
        allowed_issuers=get_auth_oauth_allowed_issuers(),
        allowed_redirect_uris=get_auth_oauth_allowed_redirect_uris(),
        required_scopes=get_auth_oauth_required_scopes(),
    )


def load_oauth_provider_registry_from_env() -> dict[str, OAuthProviderConfig]:
    """
    Load and validate OAuth provider registry from configured JSON payload.
    """

    raw_registry = get_auth_oauth_provider_registry_json()
    parsed_registry = _parse_registry_payload(raw_registry)
    output: dict[str, OAuthProviderConfig] = {}
    for index, item in enumerate(parsed_registry):
        provider = _parse_provider_config(index=index, raw_item=item)
        if provider.provider_id in output:
            raise OAuthProviderConfigError(
                message="OAuth provider configuration is invalid.",
                reason="oauth_provider_config_invalid",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "provider_id_unique",
                },
            )
        output[provider.provider_id] = provider
    return output


def get_trusted_enabled_oauth_provider(
    *,
    provider_id: str,
    registry: Mapping[str, OAuthProviderConfig] | None = None,
    trust_policy: OAuthProviderTrustPolicy | None = None,
) -> OAuthProviderConfig:
    """
    Resolve provider only when explicitly enabled and trust-policy compliant.
    """

    normalized_provider_id = provider_id.strip()
    if not normalized_provider_id:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={"requirement": "provider_id_required"},
        )
    effective_registry = (
        load_oauth_provider_registry_from_env() if registry is None else dict(registry)
    )
    if normalized_provider_id not in effective_registry:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_id": normalized_provider_id,
                "requirement": "provider_registered",
            },
        )
    provider = effective_registry[normalized_provider_id]
    if not provider.enabled:
        raise OAuthProviderConfigError(
            message="OAuth provider is disabled by trust policy.",
            reason="oauth_provider_disabled",
            details={"provider_id": provider.provider_id},
        )
    effective_trust_policy = (
        get_default_oauth_provider_trust_policy() if trust_policy is None else trust_policy
    )
    _validate_provider_against_trust_policy(
        provider=provider,
        trust_policy=effective_trust_policy,
    )
    return provider


def _parse_registry_payload(raw_registry: str) -> list[dict[str, object]]:
    try:
        loaded = json.loads(raw_registry)
    except json.JSONDecodeError as error:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={"requirement": "valid_json_registry"},
        ) from error
    if not isinstance(loaded, list):
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={"requirement": "registry_must_be_list"},
        )
    output: list[dict[str, object]] = []
    loaded_items = cast(list[object], loaded)
    for item in loaded_items:
        if not isinstance(item, Mapping):
            raise OAuthProviderConfigError(
                message="OAuth provider configuration is invalid.",
                reason="oauth_provider_config_invalid",
                details={"requirement": "registry_items_must_be_objects"},
            )
        item_map = cast(Mapping[str, object], item)
        output.append(dict(item_map))
    return output


def _parse_provider_config(*, index: int, raw_item: dict[str, object]) -> OAuthProviderConfig:
    provider_id = _read_required_string_field(
        raw_item=raw_item,
        field_name="provider_id",
        index=index,
    )
    issuer = _read_required_string_field(
        raw_item=raw_item,
        field_name="issuer",
        index=index,
    )
    authorization_endpoint = _read_required_string_field(
        raw_item=raw_item,
        field_name="authorization_endpoint",
        index=index,
    )
    token_endpoint = _read_required_string_field(
        raw_item=raw_item,
        field_name="token_endpoint",
        index=index,
    )
    jwks_uri = _read_required_string_field(
        raw_item=raw_item,
        field_name="jwks_uri",
        index=index,
    )
    client_id = _read_required_string_field(
        raw_item=raw_item,
        field_name="client_id",
        index=index,
    )
    client_secret_ref = _read_client_secret_reference(
        raw_item=raw_item,
        index=index,
    )
    redirect_uri = _read_required_string_field(
        raw_item=raw_item,
        field_name="redirect_uri",
        index=index,
    )
    scopes = _read_required_scopes(raw_item=raw_item, index=index)
    enabled = _read_enabled(raw_item=raw_item, index=index)

    _validate_https_url(value=issuer, field_name="issuer", provider_id=provider_id)
    _validate_https_url(
        value=authorization_endpoint,
        field_name="authorization_endpoint",
        provider_id=provider_id,
    )
    _validate_https_url(
        value=token_endpoint,
        field_name="token_endpoint",
        provider_id=provider_id,
    )
    _validate_https_url(value=jwks_uri, field_name="jwks_uri", provider_id=provider_id)
    _validate_https_url(
        value=redirect_uri,
        field_name="redirect_uri",
        provider_id=provider_id,
    )

    return OAuthProviderConfig(
        provider_id=provider_id,
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        jwks_uri=jwks_uri,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        redirect_uri=redirect_uri,
        scopes=scopes,
        enabled=enabled,
    )


def _read_required_string_field(
    *,
    raw_item: dict[str, object],
    field_name: str,
    index: int,
) -> str:
    raw_value = raw_item.get(field_name)
    if not isinstance(raw_value, str):
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_index": index,
                "field": field_name,
                "requirement": "non_empty_string",
            },
        )
    normalized_value = raw_value.strip()
    if not normalized_value:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_index": index,
                "field": field_name,
                "requirement": "non_empty_string",
            },
        )
    return normalized_value


def _read_client_secret_reference(*, raw_item: dict[str, object], index: int) -> str:
    raw_value = raw_item.get("client_secret_ref")
    if not isinstance(raw_value, str):
        raise OAuthProviderConfigError(
            message="OAuth provider secret reference is missing.",
            reason="oauth_provider_secret_reference_missing",
            details={
                "provider_index": index,
                "field": "client_secret_ref",
            },
        )
    normalized_value = raw_value.strip()
    if not normalized_value:
        raise OAuthProviderConfigError(
            message="OAuth provider secret reference is missing.",
            reason="oauth_provider_secret_reference_missing",
            details={
                "provider_index": index,
                "field": "client_secret_ref",
            },
        )
    return normalized_value


def _read_required_scopes(*, raw_item: dict[str, object], index: int) -> tuple[str, ...]:
    raw_scopes = raw_item.get("scopes")
    if not isinstance(raw_scopes, Sequence) or isinstance(raw_scopes, (str, bytes)):
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_index": index,
                "field": "scopes",
                "requirement": "non_empty_string_list",
            },
        )
    normalized_scopes: list[str] = []
    scope_items = cast(Sequence[object], raw_scopes)
    for scope in scope_items:
        if not isinstance(scope, str):
            raise OAuthProviderConfigError(
                message="OAuth provider configuration is invalid.",
                reason="oauth_provider_config_invalid",
                details={
                    "provider_index": index,
                    "field": "scopes",
                    "requirement": "non_empty_string_list",
                },
            )
        normalized_scope = scope.strip()
        if not normalized_scope:
            raise OAuthProviderConfigError(
                message="OAuth provider configuration is invalid.",
                reason="oauth_provider_config_invalid",
                details={
                    "provider_index": index,
                    "field": "scopes",
                    "requirement": "non_empty_string_list",
                },
            )
        if normalized_scope not in normalized_scopes:
            normalized_scopes.append(normalized_scope)
    if not normalized_scopes:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_index": index,
                "field": "scopes",
                "requirement": "non_empty_string_list",
            },
        )
    return tuple(normalized_scopes)


def _read_enabled(*, raw_item: dict[str, object], index: int) -> bool:
    raw_enabled = raw_item.get("enabled")
    if not isinstance(raw_enabled, bool):
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_index": index,
                "field": "enabled",
                "requirement": "boolean",
            },
        )
    return raw_enabled


def _validate_https_url(*, value: str, field_name: str, provider_id: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_id": provider_id,
                "field": field_name,
                "requirement": "https_uri_required",
            },
        )


def _validate_provider_against_trust_policy(
    *,
    provider: OAuthProviderConfig,
    trust_policy: OAuthProviderTrustPolicy,
) -> None:
    if trust_policy.allowed_issuers and provider.issuer not in trust_policy.allowed_issuers:
        raise OAuthProviderConfigError(
            message="OAuth provider issuer is not allowed by trust policy.",
            reason="oauth_provider_issuer_not_allowed",
            details={
                "provider_id": provider.provider_id,
                "issuer": provider.issuer,
            },
        )
    if (
        trust_policy.allowed_redirect_uris
        and provider.redirect_uri not in trust_policy.allowed_redirect_uris
    ):
        raise OAuthProviderConfigError(
            message="OAuth provider redirect URI is " + "not allowed by trust policy.",
            reason="oauth_provider_redirect_uri_not_allowed",
            details={
                "provider_id": provider.provider_id,
                "redirect_uri": provider.redirect_uri,
            },
        )
    if not trust_policy.required_scopes.issubset(set(provider.scopes)):
        raise OAuthProviderConfigError(
            message="OAuth provider configuration is invalid.",
            reason="oauth_provider_config_invalid",
            details={
                "provider_id": provider.provider_id,
                "field": "scopes",
                "requirement": "required_scopes_missing",
                "required_scopes": sorted(trust_policy.required_scopes),
            },
        )
