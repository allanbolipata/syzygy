"""Tests for the dev-only local login (``dev_auth_token``).

When the static token is configured, it authenticates as the local dev
admin WITHOUT contacting Keycloak (``auth.verify_token`` is a recording
stub, so any Keycloak call is observed and provably absent for the dev
path) and ``/auth/redirect`` issues the session cookie instead of
kicking the browser to the Keycloak authorize endpoint. With the token
unset, behavior is exactly the Keycloak flow."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
from database import Base, get_db, settings
from main import app

DEV_TOKEN = "dev-secret-token"


class _KeycloakStub:
    """Stand-in for verify_token: records every call and rejects the
    token, like a real Keycloak that knows nothing about it."""

    def __init__(self):
        self.calls = []

    def __call__(self, token: str) -> dict:
        self.calls.append(token)
        raise auth.TokenError("keycloak rejects all tokens in this test")


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
def client(db, monkeypatch):
    """Dev auth enabled; yields (client, keycloak-stub)."""
    stub = _KeycloakStub()
    monkeypatch.setattr(auth, "verify_token", stub)
    monkeypatch.setattr(settings, "dev_auth_token", DEV_TOKEN)

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    # follow_redirects=False so the 307 and its headers can be inspected.
    yield TestClient(app, follow_redirects=False), stub
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client_disabled(db, monkeypatch):
    """Dev auth disabled; yields (client, keycloak-stub)."""
    stub = _KeycloakStub()
    monkeypatch.setattr(auth, "verify_token", stub)
    monkeypatch.setattr(settings, "dev_auth_token", "")

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, follow_redirects=False), stub
    app.dependency_overrides.pop(get_db, None)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_dev_login_flag_true_when_enabled(client):
    test_client, _ = client
    assert test_client.get("/auth/dev-login").json() == {"dev_login": True}
    assert test_client.get("/v1/auth/dev-login").json() == {
        "dev_login": True
    }


def test_dev_login_flag_false_when_disabled(client_disabled):
    test_client, _ = client_disabled
    assert test_client.get("/auth/dev-login").json() == {
        "dev_login": False
    }


def test_dev_token_bearer_is_200_without_keycloak(client):
    test_client, stub = client
    # No AllowedUser rows exist: the dev path bypasses the allow-list.
    response = test_client.get("/v1/auth/me", headers=_bearer(DEV_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "dev-admin"
    assert body["email"] == settings.dev_auth_email
    assert body["preferred_username"] == "dev-admin"
    assert body["display_name"]

    assert (
        test_client.get("/v1/sample/", headers=_bearer(DEV_TOKEN)).status_code
        == 200
    )
    # The dev token never touches the Keycloak verification path.
    assert stub.calls == []


def test_dev_token_cookie_is_200(client):
    test_client, stub = client
    response = test_client.get(
        "/v1/auth/me", cookies={"auth_token": DEV_TOKEN}
    )
    assert response.status_code == 200
    assert response.json()["email"] == settings.dev_auth_email
    assert stub.calls == []


def test_wrong_token_is_401_even_in_dev_mode(client):
    test_client, _ = client
    assert (
        test_client.get(
            "/v1/auth/me", headers=_bearer("wrong-token")
        ).status_code
        == 401
    )


def test_no_token_is_401_even_in_dev_mode(client):
    test_client, _ = client
    assert test_client.get("/v1/auth/me").status_code == 401


def test_dev_redirect_issues_cookie_instead_of_keycloak(client):
    test_client, stub = client
    response = test_client.get("/auth/redirect")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"
    set_cookie = response.headers.get("set-cookie", "")
    assert f"auth_token={DEV_TOKEN}" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert stub.calls == []


def test_dev_redirect_session_authenticates(client):
    test_client, _ = client
    response = test_client.get("/auth/redirect")
    token = None
    for part in response.headers.get("set-cookie", "").split(";"):
        if part.startswith("auth_token="):
            token = part[len("auth_token="):]
    assert token == DEV_TOKEN
    assert (
        test_client.get(
            "/v1/auth/me", cookies={"auth_token": token}
        ).status_code
        == 200
    )


def test_disabled_dev_token_is_not_accepted(client_disabled):
    # The dev token string, with dev auth off, is just a garbage token:
    # 401, and the normal Keycloak flow is untouched.
    test_client, _ = client_disabled
    assert (
        test_client.get(
            "/v1/auth/me", headers=_bearer(DEV_TOKEN)
        ).status_code
        == 401
    )
    response = test_client.get("/auth/redirect")
    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith(
        f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        "/protocol/openid-connect/auth"
    )
