"""Tests for GET /auth/callback (authorization-code exchange + session
cookie). A local stub server stands in for Keycloak's token endpoint (no
real Keycloak); the app's ``get_db`` dependency is overridden with the
in-memory session from the ``db`` fixture and the app lifespan is not run
(bare TestClient)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db, settings
from main import app

REALM = "test-realm"
CLIENT_ID = "syzygy"
CLIENT_SECRET = "stub-secret"

# Mutable stub behaviour so individual tests can flip the token endpoint's
# status code, and a record of the last POST it received.
TOKEN_RESPONSE = {
    "status": 200,
    "body": {"access_token": "stub-token", "expires_in": 3600},
}
LAST_TOKEN_REQUEST: dict = {}


class _TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == f"/realms/{REALM}/protocol/openid-connect/token":
            length = int(self.headers.get("Content-Length", 0))
            LAST_TOKEN_REQUEST["path"] = self.path
            LAST_TOKEN_REQUEST["form"] = parse_qs(self.rfile.read(length).decode())
            payload = json.dumps(TOKEN_RESPONSE["body"]).encode()
            self.send_response(TOKEN_RESPONSE["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches base class param name
        pass


@pytest.fixture(scope="module")
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
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
    yield


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
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
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_stub():
    TOKEN_RESPONSE["status"] = 200
    TOKEN_RESPONSE["body"] = {"access_token": "stub-token", "expires_in": 3600}
    LAST_TOKEN_REQUEST.clear()
    yield


def _redirect_state(client):
    response = client.get("/auth/redirect")
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    return parse_qs(urlsplit(location).query)["state"][0]


def test_callback_exchanges_code_and_sets_cookie(client, kc_settings):
    state = _redirect_state(client)
    response = client.get(f"/auth/callback?code=abc&state={state}")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"

    cookie = response.headers.get("set-cookie", "")
    assert cookie.startswith("auth_token=stub-token")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=3600" in cookie

    # The code went to the Keycloak token endpoint as form data.
    assert LAST_TOKEN_REQUEST["path"] == (
        f"/realms/{REALM}/protocol/openid-connect/token"
    )
    form = LAST_TOKEN_REQUEST["form"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["abc"]
    assert form["redirect_uri"] == ["http://testserver/auth/callback"]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]


def test_callback_rejects_unknown_state(client, kc_settings):
    response = client.get("/auth/callback?code=abc&state=made-up-state")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid login state"


def test_callback_state_is_single_use(client, kc_settings):
    state = _redirect_state(client)
    first = client.get(f"/auth/callback?code=abc&state={state}")
    assert first.status_code in (302, 307)
    second = client.get(f"/auth/callback?code=abc&state={state}")
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid login state"


def test_callback_token_exchange_failure_maps_to_502(client, kc_settings):
    TOKEN_RESPONSE["status"] = 400
    state = _redirect_state(client)
    response = client.get(f"/auth/callback?code=abc&state={state}")
    assert response.status_code == 502
    assert response.json()["detail"] == "Keycloak token exchange failed"
