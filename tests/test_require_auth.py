"""Tests for the /v1 auth requirement: a valid Keycloak token whose email
is an active row in allowed_users. ``auth.verify_token`` is monkeypatched
(no real Keycloak); the app's ``get_db`` dependency is overridden with the
in-memory session from the ``db`` fixture."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
from database import Base, get_db
from main import app
from models import AllowedUser


VALID_TOKEN = "valid.keycloak.token"
VALID_CLAIMS = {
    "sub": "user-123",
    "email": "alice@example.com",
    "preferred_username": "alice",
}


def _fake_verify_token(token: str) -> dict:
    if token == VALID_TOKEN:
        return dict(VALID_CLAIMS)
    raise auth.TokenError(f"not a valid token: {token!r}")


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
    monkeypatch.setattr(auth, "verify_token", _fake_verify_token)

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    # Bare client (no context manager): the app lifespan is not run, so no
    # real database engine is used.
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_no_token_is_401(client):
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/sample").status_code == 401


def test_garbage_token_is_401(client):
    headers = _bearer("garbage.token.here")
    assert client.get("/v1/auth/me", headers=headers).status_code == 401
    assert client.get("/v1/sample", headers=headers).status_code == 401


def test_valid_token_not_in_allowed_users_is_403(client, db):
    # No AllowedUser rows at all.
    headers = _bearer(VALID_TOKEN)
    assert client.get("/v1/auth/me", headers=headers).status_code == 403
    assert client.get("/v1/sample", headers=headers).status_code == 403


def test_valid_token_inactive_user_is_403(client, db):
    db.add(AllowedUser(email="alice@example.com", display_name="Alice", active=False))
    db.commit()
    headers = _bearer(VALID_TOKEN)
    assert client.get("/v1/auth/me", headers=headers).status_code == 403
    assert client.get("/v1/sample", headers=headers).status_code == 403


def test_valid_token_allowed_active_user_is_200(client, db):
    db.add(AllowedUser(email="alice@example.com", display_name="Alice"))
    db.commit()
    headers = _bearer(VALID_TOKEN)

    response = client.get("/v1/auth/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == "Alice"

    assert client.get("/v1/sample", headers=headers).status_code == 200


def test_valid_token_via_cookie_is_200(client, db):
    db.add(AllowedUser(email="alice@example.com", display_name="Alice"))
    db.commit()
    response = client.get("/v1/auth/me", cookies={"auth_token": VALID_TOKEN})
    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"
