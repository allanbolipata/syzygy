"""Tests for the Keycloak refresh-token flow in ``syzygy.auth``.

A local stub Keycloak server (JWKS certs + token endpoint) stands in for
the realm: it issues access tokens with a configurable TTL (minutes, like
the staging stopgap) and answers ``grant_type=refresh_token`` requests,
tracking every refresh grant it receives so tests can assert that non-
expiry failures never hit the token endpoint.

Covers the acceptance criteria:
- expired access token + stubbed refresh endpoint -> 200, cookies rotated;
- session survives idle LONGER than the realm's issued TTL (60 s realm
  TTL, 70 s idle per request, repeated) without booting to /login;
- bad signature / not-in-allowed_users do NOT trigger a refresh;
- concurrent requests during rotation stay consistent;
- /callback stores the refresh cookie; /logout clears both cookies;
- the dev_auth_token path and allowed_users gating are unchanged.

Same bare-TestClient + dependency-override pattern as test_auth_e2e.py.
"""

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

import syzygy.auth as auth
from syzygy.database import Base, get_db, settings
from syzygy.main import app
from syzygy.models import AllowedUser

REALM = "refresh-flow-realm"
CLIENT_ID = "syzygy"
CLIENT_SECRET = "stub-secret"

ALLOWED_EMAIL = "allowed@example.com"
OTHER_EMAIL = "other@example.com"

# Realm "Access Token Lifespan": minutes, like the staging stopgap.
REALM_ACCESS_TTL = 60
# Refresh cookie lifetime the stub reports (realm "Refresh Token Max").
REALM_REFRESH_TTL = 12 * 60 * 60

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WRONG_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pub_pem(key):
    # For jwt.decode() verification: pyjwt resolves a private-key PEM to an
    # RSAPrivateKey, which has no verify().
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
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


JWKS = {"keys": [_jwk(KEY, "key-a")]}


def _issuer():
    # settings.keycloak_url is monkeypatched to the stub per test, so the
    # iss matches whatever realm the app currently verifies against.
    return f"{settings.keycloak_url}/realms/{REALM}"


def _make_token(email, ttl=REALM_ACCESS_TTL, age=0, key=KEY, kid="key-a"):
    """A Keycloak-shaped JWT signed by ``key``. ``age`` seconds the token
    is backdated, so age > ttl yields an EXPIRED-but-validly-signed token
    (the 'user was idle' state)."""
    iat = datetime.now(timezone.utc) - timedelta(seconds=age)
    payload = {
        "sub": f"user-{email}",
        "email": email,
        "preferred_username": email.split("@")[0],
        "iss": _issuer(),
        "aud": CLIENT_ID,
        "iat": iat,
        "exp": iat + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, _pem(key), algorithm="RS256", headers={"kid": kid})


# Mutable stub behaviour + request record, guarded because the handler
# runs on the ThreadingHTTPServer's threads.
_STATE = {}
_STATE_LOCK = threading.Lock()


def _reset_state():
    with _STATE_LOCK:
        _STATE.update(
            {
                "access_ttl": REALM_ACCESS_TTL,
                "code_email": ALLOWED_EMAIL,
                "refresh_email": ALLOWED_EMAIL,
                "issue_refresh_on_code": True,
                "rotate": False,  # Keycloak default: no refresh rotation
                "valid_refresh": {"refresh-1"},
                "code_exchanges": 0,
                "refresh_hits": 0,
                "last_code_form": None,
                "last_refresh_form": None,
            }
        )


class _StubKeycloakHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == f"/realms/{REALM}/protocol/openid-connect/certs":
            body = json.dumps(JWKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != f"/realms/{REALM}/protocol/openid-connect/token":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        grant = form.get("grant_type", [""])[0]
        with _STATE_LOCK:
            if grant == "authorization_code":
                _STATE["code_exchanges"] += 1
                _STATE["last_code_form"] = dict(form)
                email = _STATE["code_email"]
                body = {
                    "access_token": _make_token(
                        email, ttl=_STATE["access_ttl"]
                    ),
                    "expires_in": _STATE["access_ttl"],
                }
                if _STATE["issue_refresh_on_code"]:
                    body["refresh_token"] = "refresh-1"
                    body["refresh_expires_in"] = REALM_REFRESH_TTL
                status = 200
            elif grant == "refresh_token":
                _STATE["refresh_hits"] += 1
                _STATE["last_refresh_form"] = dict(form)
                presented = form.get("refresh_token", [""])[0]
                if presented in _STATE["valid_refresh"]:
                    if _STATE["rotate"]:
                        new_refresh = "refresh-rotated"
                        _STATE["valid_refresh"] = {new_refresh}
                    else:
                        new_refresh = presented
                    body = {
                        "access_token": _make_token(
                            _STATE["refresh_email"],
                            ttl=_STATE["access_ttl"],
                        ),
                        "expires_in": _STATE["access_ttl"],
                        "refresh_token": new_refresh,
                        "refresh_expires_in": REALM_REFRESH_TTL,
                    }
                    status = 200
                else:
                    body = {"error": "invalid_grant"}
                    status = 400
            else:
                body = {"error": "unsupported_grant_type"}
                status = 400
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002 - matches base class
        pass


@pytest.fixture(scope="module")
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubKeycloakHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def kc_settings(stub_server, monkeypatch):
    _reset_state()
    monkeypatch.setattr(settings, "keycloak_url", stub_server)
    monkeypatch.setattr(settings, "keycloak_realm", REALM)
    monkeypatch.setattr(settings, "keycloak_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "keycloak_client_secret", CLIENT_SECRET)
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()
    yield
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AllowedUser(email=ALLOWED_EMAIL, display_name="Allowed User")
    )
    session.commit()
    yield session
    session.close()


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    # Bare client (no context manager): the app lifespan is not run, so no
    # real database engine is used.
    c = TestClient(app, follow_redirects=False)
    yield c
    c.cookies.clear()
    app.dependency_overrides.pop(get_db, None)


def _redirect_state(c):
    response = c.get("/auth/redirect")
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    return parse_qs(urlsplit(location).query)["state"][0]


def _set_cookie_map(response):
    """{cookie name: raw Set-Cookie header} for every cookie in the
    response (httpx exposes all Set-Cookie headers via get_list)."""
    out = {}
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0].strip()
        out[name] = header
    return out


def _cookie_value(response, name):
    for header in response.headers.get_list("set-cookie"):
        if header.split("=", 1)[0].strip() == name:
            value = header.split("=", 1)[1].split(";", 1)[0].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    return None


def _refresh_hits():
    with _STATE_LOCK:
        return _STATE["refresh_hits"]


def _last_refresh_form():
    with _STATE_LOCK:
        return _STATE["last_refresh_form"]


def test_verify_token_expired_raises_token_expired_error(kc_settings):
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    with pytest.raises(auth.TokenExpiredError) as excinfo:
        auth.verify_token(expired)
    assert "expired" in str(excinfo.value)
    # Still a TokenError: every existing `except TokenError` keeps
    # catching it (e.g. the WARNING logging in _resolve_user).
    assert isinstance(excinfo.value, auth.TokenError)


def test_callback_sets_refresh_cookie(client, kc_settings):
    state = _redirect_state(client)
    response = client.get(f"/auth/callback?code=any-code&state={state}")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"

    cookies = _set_cookie_map(response)
    assert "auth_token" in cookies
    refresh = cookies.get("refresh_token")
    assert refresh is not None
    assert refresh.startswith("refresh_token=refresh-1")
    assert "HttpOnly" in refresh
    assert "SameSite=lax" in refresh
    # Path=/ on purpose: the auth dependency runs on /v1/* endpoints,
    # where an /auth-scoped cookie would never be sent.
    assert "Path=/" in refresh
    # Lifetime comes from the realm's reported refresh_expires_in.
    assert f"Max-Age={REALM_REFRESH_TTL}" in refresh


def test_callback_without_refresh_token_sets_no_refresh_cookie(
    client, kc_settings
):
    # A client/realm without refresh-grant support issues no refresh
    # token: the callback degrades to the old single-cookie behavior.
    _STATE["issue_refresh_on_code"] = False
    state = _redirect_state(client)
    response = client.get(f"/auth/callback?code=any-code&state={state}")
    assert response.status_code in (302, 307)
    cookies = _set_cookie_map(response)
    assert "auth_token" in cookies
    assert "refresh_token" not in cookies


def test_expired_access_token_silently_refreshed(client, db, kc_settings):
    # The realm issues 60-second access tokens; the browser presents one
    # minted 70 s ago (idle past the TTL) plus a live refresh cookie.
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    response = client.get(
        "/v1/auth/me",
        cookies={"auth_token": expired, "refresh_token": "refresh-1"},
    )
    assert response.status_code == 200
    assert response.json()["email"] == ALLOWED_EMAIL

    # Exactly one refresh grant, with client credentials.
    assert _refresh_hits() == 1
    form = _last_refresh_form()
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["refresh-1"]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]

    # Both cookies were rotated on the response.
    cookies = _set_cookie_map(response)
    assert "auth_token" in cookies
    assert "refresh_token" in cookies
    fresh = _cookie_value(response, "auth_token")
    assert fresh is not None and fresh != expired
    claims = jwt.decode(
        fresh,
        _pub_pem(KEY),
        algorithms=["RS256"],
        issuer=_issuer(),
        options={"verify_aud": False},
    )
    assert claims["email"] == ALLOWED_EMAIL
    # The rotated cookie carries the realm's issued TTL (60 s), not a
    # hardcoded default — whatever lifespan the realm issues is what the
    # session sees.
    assert f"Max-Age={REALM_ACCESS_TTL}" in cookies["auth_token"]
    assert "HttpOnly" in cookies["refresh_token"]
    assert "SameSite=lax" in cookies["refresh_token"]


def test_session_survives_idle_longer_than_realm_ttl(client, db, kc_settings):
    # Realm TTL is 60 seconds. The user idles 70 s between requests —
    # every presented cookie is past the realm's issued lifespan. The
    # session must transparently refresh through each idle period instead
    # of booting to /login, regardless of the lifespan the realm issues.
    idle = 70  # > REALM_ACCESS_TTL
    iterations = 10
    for i in range(iterations):
        aged = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=idle)
        response = client.get(
            "/v1/auth/me",
            cookies={"auth_token": aged, "refresh_token": "refresh-1"},
        )
        assert response.status_code == 200, (
            f"iteration {i}: session booted (realm TTL "
            f"{REALM_ACCESS_TTL} s, idle {idle} s)"
        )
        fresh = _cookie_value(response, "auth_token")
        assert fresh is not None
        claims = jwt.decode(
            fresh,
            _pub_pem(KEY),
            algorithms=["RS256"],
            issuer=_issuer(),
            options={"verify_aud": False},
        )
        assert claims["exp"] - claims["iat"] == REALM_ACCESS_TTL
    # The session bridged iterations x idle seconds of total idle against
    # a REALM_ACCESS_TTL-second realm TTL — never a 401, never /login.
    assert iterations * idle > REALM_ACCESS_TTL
    assert _refresh_hits() == iterations


def test_bad_signature_does_not_trigger_refresh(client, db, kc_settings):
    # Validly-shaped but WRONGLY-signed token (kid matches, signature
    # does not): a hard failure — no refresh grant may be attempted.
    forged = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70, key=WRONG_KEY)
    response = client.get(
        "/v1/auth/me",
        cookies={"auth_token": forged, "refresh_token": "refresh-1"},
    )
    assert response.status_code == 401
    assert _refresh_hits() == 0
    assert _cookie_value(response, "auth_token") is None


def test_not_in_allowed_users_does_not_trigger_refresh(
    client, db, kc_settings
):
    # A VALID (unexpired) token for an email outside the allow-list is a
    # "forbidden", not a token failure: no refresh grant, 403 with the
    # explanatory detail (gating unchanged).
    valid = _make_token(OTHER_EMAIL, ttl=REALM_ACCESS_TTL)
    response = client.get(
        "/v1/auth/me",
        cookies={"auth_token": valid, "refresh_token": "refresh-1"},
    )
    assert response.status_code == 403
    assert "not in the allow-list" in response.json()["detail"]
    assert _refresh_hits() == 0


def test_expired_token_without_refresh_cookie_is_401(client, db, kc_settings):
    # Expired access token but no refresh cookie (e.g. a session created
    # before this change): nothing to refresh with, plain 401.
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    response = client.get("/v1/auth/me", cookies={"auth_token": expired})
    assert response.status_code == 401
    assert _refresh_hits() == 0


def test_rejected_refresh_grant_falls_back_to_401(client, db, kc_settings):
    # Expired access token + a refresh token Keycloak no longer accepts:
    # the refresh is attempted once, fails, and the request falls through
    # to the existing unauthenticated behavior (401, no cookie rotation).
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    response = client.get(
        "/v1/auth/me",
        cookies={"auth_token": expired, "refresh_token": "refresh-stale"},
    )
    assert response.status_code == 401
    assert _refresh_hits() == 1
    assert _cookie_value(response, "auth_token") is None
    assert _cookie_value(response, "refresh_token") is None


def test_refreshed_token_still_gated_by_allowed_users(client, db, kc_settings):
    # The refresh grant succeeds but the fresh token's email is not in
    # allowed_users: the refreshed session is still gated (403).
    _STATE["refresh_email"] = OTHER_EMAIL
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    response = client.get(
        "/v1/auth/me",
        cookies={"auth_token": expired, "refresh_token": "refresh-1"},
    )
    assert response.status_code == 403
    assert "not in the allow-list" in response.json()["detail"]
    assert _refresh_hits() == 1


def test_expired_bearer_token_refreshes_via_cookie(client, db, kc_settings):
    # The presented token comes from the Authorization header, not the
    # cookie; the refresh cookie still drives the rotation and the
    # rotated cookies land on the response.
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    response = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {expired}"},
        cookies={"refresh_token": "refresh-1"},
    )
    assert response.status_code == 200
    assert _refresh_hits() == 1
    assert _cookie_value(response, "auth_token") is not None
    assert _cookie_value(response, "refresh_token") is not None


def test_concurrent_rotation_is_consistent(kc_settings, tmp_path):
    # Eight simultaneous requests all present the SAME expired cookies
    # (the single-user-per-browser race at the exact expiry moment). With
    # no refresh-token rotation (Keycloak default), every request may
    # refresh: all must succeed and every response must rotate the
    # cookies to a valid token for the same user — no corrupted state.
    # File-backed sqlite (NullPool): in-memory sqlite would hand each
    # thread its own empty database.
    engine = create_engine(
        f"sqlite:///{tmp_path}/concurrent.db",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AllowedUser(email=ALLOWED_EMAIL, display_name="Allowed User"))
    session.commit()
    session.close()

    def override_get_db():
        s = sessionmaker(bind=engine)()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    _STATE["rotate"] = False
    expired = _make_token(ALLOWED_EMAIL, ttl=REALM_ACCESS_TTL, age=70)
    results = []
    results_lock = threading.Lock()

    def hit():
        c = TestClient(app, follow_redirects=False)
        r = c.get(
            "/v1/auth/me",
            cookies={"auth_token": expired, "refresh_token": "refresh-1"},
        )
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 8
        assert all(r.status_code == 200 for r in results)
        # Every response rotated to a validly-signed token for the same
        # user with the realm TTL; the refresh cookie stayed consistent.
        for r in results:
            fresh = _cookie_value(r, "auth_token")
            assert fresh is not None
            claims = jwt.decode(
                fresh,
                _pub_pem(KEY),
                algorithms=["RS256"],
                issuer=_issuer(),
                options={"verify_aud": False},
            )
            assert claims["email"] == ALLOWED_EMAIL
            assert claims["exp"] - claims["iat"] == REALM_ACCESS_TTL
            assert _cookie_value(r, "refresh_token") == "refresh-1"
        assert _refresh_hits() == 8
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_logout_clears_both_cookies(client, kc_settings):
    response = client.get("/auth/logout")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/login"
    cookies = _set_cookie_map(response)
    assert "Max-Age=0" in cookies["auth_token"]
    assert "Max-Age=0" in cookies["refresh_token"]
    assert "Path=/" in cookies["refresh_token"]


def test_dev_auth_token_path_untouched(client, db, kc_settings, monkeypatch):
    # The dev-only static token authenticates before any Keycloak
    # verification: no refresh grant is ever attempted for it.
    monkeypatch.setattr(settings, "dev_auth_token", "dev-token-123")
    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer dev-token-123"}
    )
    assert response.status_code == 200
    assert response.json()["sub"] == "dev-admin"
    assert _refresh_hits() == 0
    assert _cookie_value(response, "auth_token") is None
