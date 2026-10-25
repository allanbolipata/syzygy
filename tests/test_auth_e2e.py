"""End-to-end auth test: one stub Keycloak server (JWKS certs + token
endpoint, on the patterns of test_auth_verify.py and test_callback.py)
and the real app, walking the full browser login flow:

    /v1 (no token) -> 401
    /login -> 200 HTML
    /auth/redirect -> 302 with state
    /auth/callback -> 302 to /ui + auth_token cookie
    /v1 with the cookie -> 200
    same flow with a token whose email is NOT in allowed_users
    -> /v1 with the cookie -> 403
    /auth/logout -> 302 to /login, cookie cleared

The app's ``get_db`` dependency is overridden with the in-memory
session from the ``db`` fixture and the app lifespan is not run (bare
TestClient)."""

import base64
import json
import threading
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
from sqlalchemy.pool import StaticPool

import auth
from database import Base, get_db, settings
from main import app
from models import AllowedUser

REALM = "test-realm"
CLIENT_ID = "syzygy"
CLIENT_SECRET = "stub-secret"

ALLOWED_EMAIL = "allowed@example.com"
OTHER_EMAIL = "other@example.com"

# Code -> email the stub token endpoint will issue a token for.
CODE_EMAILS = {
    "code-allowed": ALLOWED_EMAIL,
    "code-other": OTHER_EMAIL,
}

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


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


JWKS = {"keys": [_jwk(KEY, "key-a")]}


def _make_token(email):
    """A valid Keycloak-shaped JWT for the stub realm, signed by KEY."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"user-{email}",
        "email": email,
        "preferred_username": email.split("@")[0],
        "iss": f"{settings.keycloak_url}/realms/{REALM}",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    return jwt.encode(payload, _pem(KEY), algorithm="RS256", headers={"kid": "key-a"})


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
        if self.path == f"/realms/{REALM}/protocol/openid-connect/token":
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode())
            code = form.get("code", [""])[0]
            email = CODE_EMAILS.get(code)
            if email is None:
                body = json.dumps({"error": "invalid_grant"}).encode()
                self.send_response(400)
            else:
                body = json.dumps(
                    {
                        "access_token": _make_token(email),
                        "expires_in": 3600,
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches base class param name
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
    monkeypatch.setattr(settings, "keycloak_url", stub_server)
    monkeypatch.setattr(settings, "keycloak_realm", REALM)
    monkeypatch.setattr(settings, "keycloak_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "keycloak_client_secret", CLIENT_SECRET)
    auth._jwks_cache.clear()
    yield
    auth._jwks_cache.clear()


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
    # real database engine is used. Don't follow redirects so the 307 and
    # its Location/Set-Cookie headers can be inspected directly.
    c = TestClient(app, follow_redirects=False)
    yield c
    c.cookies.clear()
    app.dependency_overrides.pop(get_db, None)


def _client():
    # A second cookie jar while the ``client`` fixture's override is still
    # in effect.
    return TestClient(app, follow_redirects=False)


def _redirect_state(c):
    response = c.get("/auth/redirect")
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    assert location.startswith(
        f"{settings.keycloak_url}/realms/{REALM}/protocol/openid-connect/auth?"
    )
    return parse_qs(urlsplit(location).query)["state"][0]


def test_auth_e2e_flow(client, kc_settings):
    # 1. A /v1 read endpoint with no token -> 401. (The exact route path is
    #    /v1/sample/; /v1/sample is a trailing-slash redirect.)
    response = client.get("/v1/sample/")
    assert response.status_code == 401

    # 2. /login without a token -> 200 HTML.
    response = client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    # 3. /auth/redirect -> 302 carrying a state param.
    state = _redirect_state(client)

    # 4. /auth/callback with a valid code+state -> 302 to /ui and an
    #    auth_token cookie (the token the stub issued for the allowed
    #    user's email).
    response = client.get(f"/auth/callback?code=code-allowed&state={state}")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"
    cookie = response.headers.get("set-cookie", "")
    assert cookie.startswith("auth_token=")
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie

    # 5. The same /v1 endpoint using that cookie -> 200.
    response = client.get("/v1/sample/")
    assert response.status_code == 200

    # 6. Repeat the flow with a token whose email is NOT in
    #    allowed_users: the cookie is set, but /v1 -> 403.
    other = _client()
    try:
        state = _redirect_state(other)
        response = other.get(f"/auth/callback?code=code-other&state={state}")
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/ui"
        assert response.headers.get("set-cookie", "").startswith("auth_token=")

        response = other.get("/v1/sample/")
        assert response.status_code == 403
    finally:
        other.cookies.clear()

    # 7. /auth/logout -> 302 to /login and the cookie is cleared.
    response = client.get("/auth/logout")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/login"
    cookie = response.headers.get("set-cookie", "")
    assert cookie.startswith("auth_token=")
    assert "Max-Age=0" in cookie
