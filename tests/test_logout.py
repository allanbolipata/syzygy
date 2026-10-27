"""Tests for GET /auth/logout. Clears the auth_token cookie and redirects
to /login; no real Keycloak is involved. Same bare-TestClient pattern as
test_login_redirect.py."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
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


def test_logout_redirects_to_login(client):
    response = client.get("/auth/logout")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/login"


def test_logout_clears_auth_token_cookie(client):
    response = client.get("/auth/logout")
    set_cookie = response.headers.get("set-cookie", "")
    assert set_cookie.startswith('auth_token=""')
    # The cookie is being deleted: empty value with a zero max-age,
    # not a fresh session value.
    assert "Max-Age=0" in set_cookie


def test_login_page_still_works_without_token(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in with Keycloak" in response.text
