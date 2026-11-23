from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

import app.auth.oidc as oidc


def _token(private_key, *, audience="evidencepilot-api"):
    return jwt.encode(
        {
            "sub": "user-123",
            "iss": "https://identity.example.test/realms/research",
            "aud": audience,
            "iat": 1_900_000_000,
            "exp": 1_900_000_300,
            "email": "researcher@example.test",
            "realm_access": {"roles": ["RESEARCHER", "REVIEWER"]},
            "tenant_id": "tenant-a",
        },
        private_key,
        algorithm="RS256",
    )


def test_development_identity_is_explicit_and_limited(monkeypatch):
    monkeypatch.setattr(oidc, "settings", SimpleNamespace(auth_enabled=False, environment="development"))
    principal = oidc.get_current_principal(None)
    assert principal.development_identity is True
    assert principal.subject == "local-development"
    assert principal.roles == ("RESEARCHER",)


def test_production_cannot_start_with_authentication_disabled(monkeypatch):
    monkeypatch.setattr(oidc, "settings", SimpleNamespace(auth_enabled=False, environment="production"))
    with pytest.raises(RuntimeError, match="AUTH_ENABLED"):
        oidc.validate_auth_configuration()


def test_oidc_verifies_signature_issuer_audience_and_extracts_roles(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        oidc,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            oidc_issuer="https://identity.example.test/realms/research",
            oidc_audience="evidencepilot-api",
            oidc_jwks_url="https://identity.example.test/realms/research/protocol/openid-connect/certs",
        ),
    )

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private_key.public_key())

    monkeypatch.setattr(oidc, "_jwks_client", lambda _url: FakeJwksClient())
    principal = oidc.verify_access_token(_token(private_key))
    assert principal.subject == "user-123"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == ("RESEARCHER", "REVIEWER")


def test_oidc_rejects_wrong_audience(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        oidc,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            oidc_issuer="https://identity.example.test/realms/research",
            oidc_audience="evidencepilot-api",
            oidc_jwks_url="https://identity.example.test/jwks",
        ),
    )

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private_key.public_key())

    monkeypatch.setattr(oidc, "_jwks_client", lambda _url: FakeJwksClient())
    with pytest.raises(HTTPException) as error:
        oidc.verify_access_token(_token(private_key, audience="another-api"))
    assert error.value.status_code == 401


def test_oidc_rejects_unapproved_signing_algorithm(monkeypatch):
    monkeypatch.setattr(
        oidc,
        "settings",
        SimpleNamespace(auth_enabled=True, oidc_issuer="issuer", oidc_audience="api", oidc_jwks_url="https://id.test/jwks"),
    )
    token = jwt.encode({"sub": "x", "iss": "issuer", "aud": "api", "iat": 1, "exp": 9999999999}, "secret", algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        oidc.verify_access_token(token)
    assert error.value.status_code == 401
