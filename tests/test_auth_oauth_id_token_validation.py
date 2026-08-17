"""Focused tests for deterministic OIDC ID-token validation pipeline."""

from __future__ import annotations

from typing import cast
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

import pytest
from authlib.jose import jwt
from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.oauth_config import OAuthProviderConfig
from services.auth.app.oauth_validation import OidcJwksResolverProtocol
from services.auth.app.oauth_validation import OidcIdTokenValidationError
from services.auth.app.oauth_validation import AuthlibOidcIdTokenValidator


class _StaticJwksResolver:
    """Provide deterministic static JWKS payload for ID-token validation tests."""

    def __init__(self, *, jwks_payload: Mapping[str, object]) -> None:
        self._jwks_payload = dict(jwks_payload)

    def resolve_jwks(self, *, provider: OAuthProviderConfig) -> Mapping[str, object]:
        assert provider.provider_id
        return dict(self._jwks_payload)


class _FailingJwksResolver:
    """Provide deterministic JWKS resolution failure for callback tests."""

    def resolve_jwks(self, *, provider: OAuthProviderConfig) -> Mapping[str, object]:
        assert provider.provider_id
        raise OidcIdTokenValidationError(
            status_code=503,
            error_code="oidc_jwks_resolution_failed",
            message="OIDC JWKS resolution failed.",
            reason="oidc_jwks_resolution_failed",
            details={"provider_id": provider.provider_id},
        )


@pytest.fixture()
def provider() -> OAuthProviderConfig:
    """Create deterministic provider config for OIDC validation tests."""

    return OAuthProviderConfig(
        provider_id="google",
        issuer="https://accounts.google.com",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        client_id="google-client-id",
        client_secret_ref="env:AUTH_OAUTH_SECRET_GOOGLE",
        redirect_uri="https://kodi.example.com/v1/auth/oauth/google/callback",
        scopes=("openid", "email", "profile"),
        enabled=True,
    )


def test_valid_id_token_passes_signature_and_claim_validation(
    provider: OAuthProviderConfig,
) -> None:
    signing_jwk, jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=jwks_payload)
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer=provider.issuer,
        audience=provider.client_id,
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now,
    )

    claims = validator.validate_id_token(
        provider=provider,
        id_token=token,
        expected_nonce="nonce-accepted",
        now_provider=lambda: now,
    )
    assert claims["sub"] == "oidc-subject-001"
    assert claims["iss"] == provider.issuer
    assert claims["aud"] == provider.client_id


def test_invalid_signature_is_rejected_deterministically(provider: OAuthProviderConfig) -> None:
    wrong_signing_jwk, _wrong_jwks = _generate_signing_jwk_and_jwks()
    _, trusted_jwks = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=trusted_jwks)
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=wrong_signing_jwk,
        issuer=provider.issuer,
        audience=provider.client_id,
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now,
    )

    first = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    second = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    assert first["reason"] == "oidc_id_token_signature_invalid"
    assert canonical_json_dumps(first) == canonical_json_dumps(second)


def test_issuer_mismatch_is_rejected_deterministically(provider: OAuthProviderConfig) -> None:
    signing_jwk, jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=jwks_payload)
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer="https://issuer-not-allowed.example.com",
        audience=provider.client_id,
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now,
    )

    error = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    assert error["reason"] == "oidc_id_token_issuer_mismatch"


def test_audience_mismatch_is_rejected_deterministically(provider: OAuthProviderConfig) -> None:
    signing_jwk, jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=jwks_payload)
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer=provider.issuer,
        audience="some-other-client",
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now,
    )

    error = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    assert error["reason"] == "oidc_id_token_audience_mismatch"


def test_expired_id_token_is_rejected_deterministically(provider: OAuthProviderConfig) -> None:
    signing_jwk, jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=jwks_payload),
        clock_skew_seconds=0,
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer=provider.issuer,
        audience=provider.client_id,
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now - timedelta(minutes=10),
        expires_in_seconds=30,
    )

    error = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    assert error["reason"] == "oidc_id_token_expired"


def test_nonce_mismatch_is_rejected_deterministically(provider: OAuthProviderConfig) -> None:
    signing_jwk, jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=jwks_payload)
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer=provider.issuer,
        audience=provider.client_id,
        nonce="nonce-in-token",
        subject="oidc-subject-001",
        now=now,
    )

    error = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-expected",
        now=now,
    )
    assert error["reason"] == "oidc_id_token_nonce_invalid"


def test_jwks_resolution_failure_is_rejected_deterministically(
    provider: OAuthProviderConfig,
) -> None:
    signing_jwk, _jwks_payload = _generate_signing_jwk_and_jwks()
    validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=cast(OidcJwksResolverProtocol, _FailingJwksResolver())
    )
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    token = _encode_id_token(
        signing_jwk=signing_jwk,
        issuer=provider.issuer,
        audience=provider.client_id,
        nonce="nonce-accepted",
        subject="oidc-subject-001",
        now=now,
    )

    error = _capture_validation_error(
        validator=validator,
        provider=provider,
        token=token,
        expected_nonce="nonce-accepted",
        now=now,
    )
    assert error["reason"] == "oidc_jwks_resolution_failed"


def _capture_validation_error(
    *,
    validator: AuthlibOidcIdTokenValidator,
    provider: OAuthProviderConfig,
    token: str,
    expected_nonce: str,
    now: datetime,
) -> dict[str, object]:
    with pytest.raises(OidcIdTokenValidationError) as error_info:
        validator.validate_id_token(
            provider=provider,
            id_token=token,
            expected_nonce=expected_nonce,
            now_provider=lambda: now,
        )
    return {
        "error_code": error_info.value.error_code,
        "message": error_info.value.message,
        "reason": error_info.value.reason,
    }


def _generate_signing_jwk_and_jwks() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signing_jwk = JsonWebKey.import_key(private_pem, {"kty": "RSA", "kid": "k1"})
    verification_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "kid": "k1"})
    jwks_payload = {"keys": [verification_jwk.as_dict(is_private=False)]}
    return signing_jwk, jwks_payload


def _encode_id_token(
    *,
    signing_jwk: object,
    issuer: str,
    audience: str,
    nonce: str,
    subject: str,
    now: datetime,
    expires_in_seconds: int = 300,
) -> str:
    issued_at = int(now.timestamp())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "nonce": nonce,
        "iat": issued_at,
        "exp": issued_at + expires_in_seconds,
    }
    encoded = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, signing_jwk)
    return encoded.decode("utf-8")
