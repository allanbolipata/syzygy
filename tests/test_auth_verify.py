"""Tests for auth.verify_token against a local JWKS stub (no real Keycloak)."""

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import auth
from database import settings

REALM = "test-realm"
CLIENT_ID = "syzygy"


def _generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY_A = _generate_key()
KEY_B = _generate_key()


def _pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk(key, kid):
    pub = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64url(pub.n),
        "e": _b64url(pub.e),
    }


JWKS = {"keys": [_jwk(KEY_A, "key-a"), _jwk(KEY_B, "key-b")]}


def _enc_jwk(key, kid):
    """A Keycloak-style encryption-only key (RSA-OAEP, use: enc)."""
    pub = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "alg": "RSA-OAEP",
        "use": "enc",
        "kid": kid,
        "n": _b64url(pub.n),
        "e": _b64url(pub.e),
    }


# Real Keycloak realms expose an 'enc' key next to the signing key.
JWKS_WITH_ENC = {"keys": [_jwk(KEY_A, "key-a"), _enc_jwk(KEY_B, "key-enc")]}

REALM_ENC = "enc-realm"


class _JwksHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == f"/realms/{REALM}/protocol/openid-connect/certs":
            body = json.dumps(JWKS).encode()
        elif self.path == f"/realms/{REALM_ENC}/protocol/openid-connect/certs":
            body = json.dumps(JWKS_WITH_ENC).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - matches base class param name
        pass


@pytest.fixture(scope="module")
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def kc_settings(stub_server, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_url", stub_server)
    monkeypatch.setattr(settings, "keycloak_realm", REALM)
    monkeypatch.setattr(settings, "keycloak_client_id", CLIENT_ID)
    auth._jwks_cache.clear()
    yield
    auth._jwks_cache.clear()


def _issuer():
    return f"{settings.keycloak_url}/realms/{REALM}"


def _sign(key, kid, **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-123",
        "email": "user@example.com",
        "preferred_username": "user123",
        "iss": _issuer(),
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(overrides)
    return jwt.encode(payload, _pem(key), algorithm="RS256", headers={"kid": kid})


def test_valid_token_returns_identity_claims(kc_settings):
    token = _sign(KEY_A, "key-a")
    assert auth.verify_token(token) == {
        "sub": "user-123",
        "email": "user@example.com",
        "preferred_username": "user123",
    }


def test_expired_token_raises(kc_settings):
    now = datetime.now(timezone.utc)
    token = _sign(KEY_A, "key-a", exp=now - timedelta(minutes=5))
    with pytest.raises(auth.TokenError):
        auth.verify_token(token)


def test_other_client_audience_accepted(kc_settings):
    # Tokens from other clients in the same realm are accepted by design:
    # the org shares Keycloak, and the allowed_users table is the gate.
    token = _sign(KEY_A, "key-a", aud="some-other-client")
    claims = auth.verify_token(token)
    assert claims["email"] == "user@example.com"


def test_wrong_issuer_raises(kc_settings):
    token = _sign(KEY_A, "key-a", iss="http://elsewhere/realms/test-realm")
    with pytest.raises(auth.TokenError):
        auth.verify_token(token)


def test_token_signed_with_different_key_raises(kc_settings):
    # kid maps to KEY_A in the JWKS, but the token was signed with KEY_B.
    token = _sign(KEY_B, "key-a")
    with pytest.raises(auth.TokenError):
        auth.verify_token(token)


def test_missing_kid_raises(kc_settings):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-123",
        "email": "user@example.com",
        "preferred_username": "user123",
        "iss": _issuer(),
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(payload, _pem(KEY_A), algorithm="RS256")
    with pytest.raises(auth.TokenError):
        auth.verify_token(token)


def test_unknown_kid_raises(kc_settings):
    token = _sign(KEY_A, "key-not-in-jwks")
    with pytest.raises(auth.TokenError):
        auth.verify_token(token)


@pytest.fixture
def kc_settings_enc(stub_server, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_url", stub_server)
    monkeypatch.setattr(settings, "keycloak_realm", REALM_ENC)
    monkeypatch.setattr(settings, "keycloak_client_id", CLIENT_ID)
    auth._jwks_cache.clear()
    yield
    auth._jwks_cache.clear()


def test_jwks_fetched_once_per_realm(kc_settings):
    auth.verify_token(_sign(KEY_A, "key-a"))
    assert len(auth._jwks_cache) == 1
    assert list(auth._jwks_cache) == [REALM]
    # Second verify must hit the cache, not refetch.
    auth.verify_token(_sign(KEY_A, "key-a"))
    assert list(auth._jwks_cache) == [REALM]


def test_jwks_with_enc_key_still_verifies(kc_settings_enc):
    # Real Keycloak realms expose an RSA-OAEP 'use: enc' key alongside the
    # RS256 signing key. The enc key must be ignored when building the
    # cache, and tokens signed with the sig key must still verify.
    # Note: _issuer() hardcodes REALM, so pass the enc-realm issuer explicitly.
    token = _sign(KEY_A, "key-a", iss=f"{settings.keycloak_url}/realms/{REALM_ENC}")
    assert auth.verify_token(token) == {
        "sub": "user-123",
        "email": "user@example.com",
        "preferred_username": "user123",
    }
