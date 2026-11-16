"""Tests for JWKS kid-cache invalidation (``syzygy.auth``).

When Keycloak rotates its signing keys, tokens minted after the rotation
carry a kid absent from the in-process JWKS cache. ``verify_token`` must
refetch the JWKS once (rate-capped) before giving up, so post-rotation
tokens verify without an app restart — and a flood of unknown-kid tokens
must not hammer the certs endpoint.

No real Keycloak: a local stub whose served JWKS is mutable per test.
"""

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import syzygy.auth as auth
from syzygy.database import settings

REALM = "refetch-realm"
CLIENT_ID = "syzygy"


def _generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY_A = _generate_key()
KEY_B = _generate_key()

# Mutable state the stub handler reads; reset per test.
_STATE = {"keys": [], "hits": 0}


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


def _issuer():
    return f"{settings.keycloak_url}/realms/{REALM}"


def _sign(key, kid):
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
    return jwt.encode(payload, _pem(key), algorithm="RS256", headers={"kid": kid})


class _MutableJwksHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == f"/realms/{REALM}/protocol/openid-connect/certs":
            _STATE["hits"] += 1
            body = json.dumps({"keys": _STATE["keys"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches base class
        pass


@pytest.fixture
def stub_server():
    _STATE["keys"] = []
    _STATE["hits"] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MutableJwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def kc(stub_server, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_url", stub_server)
    monkeypatch.setattr(settings, "keycloak_realm", REALM)
    monkeypatch.setattr(settings, "keycloak_client_id", CLIENT_ID)
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()
    yield
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()


def test_rotation_refetch_verifies_new_kid(kc, monkeypatch):
    # Isolate the refetch + re-verify logic from the rate cap.
    monkeypatch.setattr(auth, "_JWKS_MIN_REFETCH_INTERVAL", 0.0)

    # Keycloak initially serves kid A; verifying a kid-A token seeds the
    # in-process JWKS cache with kid A only.
    _STATE["keys"] = [_jwk(KEY_A, "kid-a")]
    assert auth.verify_token(_sign(KEY_A, "kid-a"))["sub"] == "user-123"
    assert set(auth._jwks_cache[REALM]) == {"kid-a"}
    hits_before = _STATE["hits"]

    # Keycloak rotates to kid B. A post-rotation token carries kid B,
    # absent from the cache; the refetch must pull kid B so the token
    # verifies without an app restart.
    _STATE["keys"] = [_jwk(KEY_B, "kid-b")]
    assert auth.verify_token(_sign(KEY_B, "kid-b"))["sub"] == "user-123"
    assert set(auth._jwks_cache[REALM]) == {"kid-b"}
    assert _STATE["hits"] == hits_before + 1


def test_unknown_kid_flood_is_rate_capped(kc):
    # The real 60 s cap is in effect. A flood of unknown-kid tokens must
    # force at most ONE refetch, not one per token.
    _STATE["keys"] = [_jwk(KEY_A, "kid-a")]
    auth.verify_token(_sign(KEY_A, "kid-a"))  # initial fetch
    assert _STATE["hits"] == 1

    # Let the cap "elapse", then point the stub at a JWKS lacking the
    # flood's kid.
    auth._jwks_fetched_at[REALM] = (
        time.monotonic() - (auth._JWKS_MIN_REFETCH_INTERVAL + 1)
    )
    _STATE["keys"] = [_jwk(KEY_B, "kid-b")]

    for _ in range(10):
        with pytest.raises(auth.TokenError):
            auth.verify_token(_sign(KEY_B, "kid-unknown"))

    # 10 unknown-kid tokens forced exactly one refetch (the first); the
    # remaining nine were capped by the min interval.
    assert _STATE["hits"] == 2


def test_refetch_failure_keeps_cached_keys(kc, monkeypatch):
    # A failed refetch (endpoint blip) must not wipe the intact cache:
    # previously-cached kids keep verifying.
    monkeypatch.setattr(auth, "_JWKS_MIN_REFETCH_INTERVAL", 0.0)
    _STATE["keys"] = [_jwk(KEY_A, "kid-a")]
    assert auth.verify_token(_sign(KEY_A, "kid-a"))["sub"] == "user-123"

    # Kill the endpoint so any refetch fails.
    monkeypatch.setattr(settings, "keycloak_url", "http://127.0.0.1:1")

    # An unknown kid triggers a (failing) refetch -> TokenError...
    with pytest.raises(auth.TokenError):
        auth.verify_token(_sign(KEY_B, "kid-b"))

    # ...but the previously-cached kid still verifies.
    assert auth.verify_token(_sign(KEY_A, "kid-a"))["sub"] == "user-123"


def test_initial_jwks_fetch_failure_is_tokenerror(kc, monkeypatch):
    # An unreachable certs endpoint must surface as a TokenError (which
    # _resolve_user logs) rather than an unhandled httpx exception (500).
    monkeypatch.setattr(settings, "keycloak_url", "http://127.0.0.1:1")
    with pytest.raises(auth.TokenError) as excinfo:
        auth.verify_token(_sign(KEY_A, "kid-a"))
    assert "JWKS fetch failed" in str(excinfo.value)
