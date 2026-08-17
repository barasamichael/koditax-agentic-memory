"""Deterministic OIDC ID-token validation primitives for OAuth callback flow."""

from __future__ import annotations

from typing import Any
from typing import cast
from typing import Protocol
from datetime import UTC
from datetime import datetime
from collections.abc import Mapping
from collections.abc import Callable

import httpx
from authlib.jose import jwt
from authlib.jose import JsonWebKey
from authlib.jose.errors import JoseError
from authlib.jose.errors import DecodeError
from authlib.jose.errors import BadSignatureError
from authlib.jose.errors import ExpiredTokenError
from authlib.jose.errors import InvalidClaimError
from authlib.jose.errors import MissingClaimError

from services.auth.app.oauth_config import OAuthProviderConfig

_DEFAULT_ID_TOKEN_CLOCK_SKEW_SECONDS = 60
_DEFAULT_JWKS_TIMEOUT_SECONDS = 5.0


class OidcIdTokenValidationError(ValueError):
    """Represent deterministic OIDC ID-token validation failures."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


class OidcJwksResolverProtocol(Protocol):
    """Define trusted JWKS resolution boundary for provider-bound token verification."""

    def resolve_jwks(self, *, provider: OAuthProviderConfig) -> Mapping[str, object]:
        """Resolve provider JWKS payload used for deterministic signature validation."""

        ...


class OidcIdTokenValidatorProtocol(Protocol):
    """Define ID-token validation boundary for callback flow semantics."""

    def validate_id_token(
        self,
        *,
        provider: OAuthProviderConfig,
        id_token: str,
        expected_nonce: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> Mapping[str, object]:
        """Validate one ID token and return claims when valid."""

        ...


class HttpxOidcJwksResolver:
    """Resolve provider JWKS with deterministic mapping and failure semantics."""

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_JWKS_TIMEOUT_SECONDS,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def resolve_jwks(self, *, provider: OAuthProviderConfig) -> Mapping[str, object]:
        client = self._build_client()
        try:
            response = client.get(provider.jwks_uri)
        except Exception as error:
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={"provider_id": provider.provider_id},
            ) from error
        finally:
            client.close()
        if response.status_code != 200:
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "jwks_http_200_required",
                },
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "jwks_json_payload_required",
                },
            ) from error
        if not isinstance(payload, dict):
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "jwks_mapping_payload_required",
                },
            )
        payload_map = cast(Mapping[str, object], payload)
        keys = payload_map.get("keys")
        if not isinstance(keys, list) or not keys:
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "jwks_keys_array_required",
                },
            )
        return cast(Mapping[str, object], payload)

    def _build_client(self) -> httpx.Client:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.Client(timeout=self._timeout_seconds)


class AuthlibOidcIdTokenValidator:
    """Validate OIDC ID tokens using Authlib JWT/JWKS validation primitives."""

    def __init__(
        self,
        *,
        jwks_resolver: OidcJwksResolverProtocol | None = None,
        clock_skew_seconds: int = _DEFAULT_ID_TOKEN_CLOCK_SKEW_SECONDS,
    ) -> None:
        self._jwks_resolver = (
            get_default_oidc_jwks_resolver() if jwks_resolver is None else jwks_resolver
        )
        self._clock_skew_seconds = clock_skew_seconds

    def validate_id_token(
        self,
        *,
        provider: OAuthProviderConfig,
        id_token: str,
        expected_nonce: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> Mapping[str, object]:
        normalized_token = id_token.strip()
        if not normalized_token:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_missing",
                message="OIDC ID token is missing.",
                reason="oidc_id_token_missing",
                details={"provider_id": provider.provider_id},
            )
        normalized_nonce = expected_nonce.strip()
        if not normalized_nonce:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_nonce_invalid",
                message="OIDC ID token nonce is invalid.",
                reason="oidc_id_token_nonce_invalid",
                details={"provider_id": provider.provider_id},
            )
        jwks_payload = self._jwks_resolver.resolve_jwks(provider=provider)
        try:
            jwks_key_set = JsonWebKey.import_key_set(jwks_payload)
        except Exception as error:
            raise OidcIdTokenValidationError(
                status_code=503,
                error_code="oidc_jwks_resolution_failed",
                message="OIDC JWKS resolution failed.",
                reason="oidc_jwks_resolution_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "jwks_key_set_importable",
                },
            ) from error
        claims_options = {
            "iss": {"essential": True, "value": provider.issuer},
            "aud": {"essential": True, "value": provider.client_id},
            "exp": {"essential": True},
            "iat": {"essential": True},
            "nonce": {"essential": True, "value": normalized_nonce},
        }
        try:
            jwt_any = cast(Any, jwt)
            claims_obj = jwt_any.decode(
                normalized_token,
                jwks_key_set,
                claims_options=claims_options,
            )
            claims_obj.validate(
                now=_as_epoch(now_provider=now_provider),
                leeway=self._clock_skew_seconds,
            )
        except ExpiredTokenError as error:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_expired",
                message="OIDC ID token is expired.",
                reason="oidc_id_token_expired",
                details={"provider_id": provider.provider_id},
            ) from error
        except MissingClaimError as error:
            raise _map_claim_validation_error(
                provider_id=provider.provider_id,
                claim_name=getattr(error, "claim_name", None),
            ) from error
        except InvalidClaimError as error:
            raise _map_claim_validation_error(
                provider_id=provider.provider_id,
                claim_name=getattr(error, "claim_name", None),
            ) from error
        except BadSignatureError as error:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_signature_invalid",
                message="OIDC ID token signature is invalid.",
                reason="oidc_id_token_signature_invalid",
                details={"provider_id": provider.provider_id},
            ) from error
        except DecodeError as error:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_signature_invalid",
                message="OIDC ID token signature is invalid.",
                reason="oidc_id_token_signature_invalid",
                details={"provider_id": provider.provider_id},
            ) from error
        except JoseError as error:
            raise OidcIdTokenValidationError(
                status_code=401,
                error_code="oidc_id_token_signature_invalid",
                message="OIDC ID token signature is invalid.",
                reason="oidc_id_token_signature_invalid",
                details={"provider_id": provider.provider_id},
            ) from error

        audience_claim = claims_obj.get("aud")
        if isinstance(audience_claim, list):
            audience_claim_list = cast(list[object], audience_claim)
            if len(audience_claim_list) > 1:
                authorized_party = claims_obj.get("azp")
                if (
                    not isinstance(authorized_party, str)
                    or authorized_party.strip() != provider.client_id
                ):
                    raise OidcIdTokenValidationError(
                        status_code=401,
                        error_code="oidc_id_token_audience_mismatch",
                        message="OIDC ID token audience claim does not match provider client.",
                        reason="oidc_id_token_audience_mismatch",
                        details={"provider_id": provider.provider_id},
                    )
        return cast(Mapping[str, object], dict(claims_obj))


def get_default_oidc_jwks_resolver() -> OidcJwksResolverProtocol:
    """Return default JWKS resolver for OIDC validation flow."""

    return _DEFAULT_OIDC_JWKS_RESOLVER


def get_default_oidc_id_token_validator() -> OidcIdTokenValidatorProtocol:
    """Return default OIDC ID-token validator."""

    return _DEFAULT_OIDC_ID_TOKEN_VALIDATOR


def _as_epoch(*, now_provider: Callable[[], datetime] | None) -> int:
    now = datetime.now(UTC) if now_provider is None else now_provider()
    return int(now.timestamp())


def _map_claim_validation_error(
    *,
    provider_id: str,
    claim_name: str | None,
) -> OidcIdTokenValidationError:
    normalized_claim = "" if claim_name is None else claim_name.strip().lower()
    if normalized_claim == "iss":
        return OidcIdTokenValidationError(
            status_code=401,
            error_code="oidc_id_token_issuer_mismatch",
            message="OIDC ID token issuer claim does not match provider policy.",
            reason="oidc_id_token_issuer_mismatch",
            details={"provider_id": provider_id},
        )
    if normalized_claim in {"aud", "azp"}:
        return OidcIdTokenValidationError(
            status_code=401,
            error_code="oidc_id_token_audience_mismatch",
            message="OIDC ID token audience claim does not match provider client.",
            reason="oidc_id_token_audience_mismatch",
            details={"provider_id": provider_id},
        )
    if normalized_claim == "nonce":
        return OidcIdTokenValidationError(
            status_code=401,
            error_code="oidc_id_token_nonce_invalid",
            message="OIDC ID token nonce is invalid.",
            reason="oidc_id_token_nonce_invalid",
            details={"provider_id": provider_id},
        )
    if normalized_claim in {"exp", "iat", "nbf"}:
        return OidcIdTokenValidationError(
            status_code=401,
            error_code="oidc_id_token_expired",
            message="OIDC ID token is expired.",
            reason="oidc_id_token_expired",
            details={"provider_id": provider_id},
        )
    return OidcIdTokenValidationError(
        status_code=401,
        error_code="oidc_id_token_signature_invalid",
        message="OIDC ID token signature is invalid.",
        reason="oidc_id_token_signature_invalid",
        details={"provider_id": provider_id},
    )


_DEFAULT_OIDC_JWKS_RESOLVER = HttpxOidcJwksResolver()
_DEFAULT_OIDC_ID_TOKEN_VALIDATOR = AuthlibOidcIdTokenValidator(
    jwks_resolver=_DEFAULT_OIDC_JWKS_RESOLVER
)
