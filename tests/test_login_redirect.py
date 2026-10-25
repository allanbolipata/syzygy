"""Tests for GET /login (login page) and GET /auth/redirect (Keycloak
authorize redirect). No real Keycloak is involved; the app's ``get_db``
dependency is overridden with the in-memory session from the ``db``
fixture and the app lifespan is not run (bare TestClient)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db, settings
from main import app


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
    # its Location header can be inspected directly.
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.pop(get_db, None)


def test_login_page_is_served(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Sign in with Keycloak" in response.text


def test_login_page_is_not_cached(client):
    response = client.get("/login")
    assert response.headers.get("Cache-Control") == "no-store"


def test_auth_redirect_goes_to_keycloak_authorize(client):
    response = client.get("/auth/redirect")
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    authorize_url = (
        f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        "/protocol/openid-connect/auth"
    )
    assert location.startswith(authorize_url)

    query = parse_qs(urlsplit(location).query)
    assert query["client_id"] == [settings.keycloak_client_id]
    assert query["redirect_uri"] == ["http://testserver/auth/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    assert query["state"][0], "state param missing or empty"


def test_auth_redirect_states_differ_per_request(client):
    first = client.get("/auth/redirect").headers["location"]
    second = client.get("/auth/redirect").headers["location"]
    first_state = parse_qs(urlsplit(first).query)["state"][0]
    second_state = parse_qs(urlsplit(second).query)["state"][0]
    assert first_state != second_state
