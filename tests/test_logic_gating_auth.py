"""C1 — logic gating: Keycloak claims -> principal entitlements.

Direct-call tests with synthetic claims (repo convention) cover the claim
parsing: exact set, fail-closed empty set, gating-off full set, dev-admin,
unknown-name warning, and configured (non-default) claim names. A local
JWKS stub covers the token-path pass-through of the entitlement claims;
TestClient tests cover the /auth/me JSON shape and the dev-token path end
to end.

``logic_gating_enabled`` defaults to False (dark launch); every test that
turns it on does so via monkeypatch, mirroring the root conftest's
``dev_auth_token`` discipline.
"""
import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.auth as auth
from syzygy.database import Base, get_db, settings
from syzygy.main import app
from syzygy.models import AllowedUser, PairingLogic

EMAIL = "logic@example.com"
ALL_LOGICS = {"ARGOS", "DMP", "HARMONY", "ACCESS", "TEMPO"}
REALM = "gating-claim-realm"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AllowedUser(email=EMAIL, display_name="Logic User"))
    for name in sorted(ALL_LOGICS):
        session.add(PairingLogic(name=name, strategy_name="default"))
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


@pytest.fixture(autouse=True)
def _uniform_open_logics(monkeypatch):
    """These claim-parsing tests predate P1 (GATING_OPEN_LOGICS) and assert
    the *claim-derived* set in isolation. Pin the default-open set to empty
    for the whole module so the open set does not leak into them; the
    P1-specific tests at the bottom of the file override this with an
    explicit value."""
    monkeypatch.setattr(settings, "gating_open_logics", "", raising=False)


def _claims(logics=None, admin=None):
    """A synthetic verified-claims dict (Keycloak-shaped identity keys)."""
    claims = {
        "sub": f"user-{EMAIL}",
        "email": EMAIL,
        "preferred_username": "logic",
    }
    if logics is not None:
        claims["syzygy_logics"] = logics
    if admin is not None:
        claims["syzygy_admin"] = admin
    return claims


# --- JWKS stub for the token-path pass-through tests ---------------------

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


JWKS = {"keys": [_jwk(KEY, "gating-key-a")]}


class _JwksHandler(BaseHTTPRequestHandler):
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
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches base class
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
    monkeypatch.setattr(settings, "keycloak_client_id", "syzygy")
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()
    yield
    auth._jwks_cache.clear()
    auth._jwks_fetched_at.clear()


def _make_entitled_token(logics=None, admin=None):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"user-{EMAIL}",
        "email": EMAIL,
        "preferred_username": "logic",
        "iss": f"{settings.keycloak_url}/realms/{REALM}",
        "aud": "syzygy",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    if logics is not None:
        payload["syzygy_logics"] = logics
    if admin is not None:
        payload["syzygy_admin"] = admin
    return jwt.encode(payload, _pem(KEY), algorithm="RS256", headers={"kid": "gating-key-a"})


def test_claim_present_exact_set(db, monkeypatch):
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    status, user, _ = auth._user_from_claims(
        _claims(logics=["ARGOS", "DMP"]), db
    )
    assert status == "ok"
    assert user["allowed_logics"] == {"ARGOS", "DMP"}
    assert user["is_logic_admin"] is False


def test_admin_claim_sets_is_logic_admin(db, monkeypatch):
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    _, user, _ = auth._user_from_claims(
        _claims(logics=["HARMONY"], admin=True), db
    )
    assert user["allowed_logics"] == {"HARMONY"}
    assert user["is_logic_admin"] is True


def test_claim_absent_gating_on_empty_set(db, monkeypatch):
    # Fail-closed: gating on + no syzygy_logics claim -> empty set.
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    _, user, _ = auth._user_from_claims(_claims(), db)
    assert user["allowed_logics"] == set()
    assert user["is_logic_admin"] is False


def test_gating_off_full_set(db):
    # logic_gating_enabled defaults to False: exactly today's behavior —
    # full set + admin so /auth/me needs no flag knowledge.
    _, user, _ = auth._user_from_claims(_claims(logics=["ARGOS"]), db)
    assert user["allowed_logics"] == ALL_LOGICS
    assert user["is_logic_admin"] is True


def test_unknown_logic_names_ignored_with_warning(db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    with caplog.at_level("WARNING", logger="syzygy.auth"):
        _, user, _ = auth._user_from_claims(
            _claims(logics=["ARGOS", "BOGUS"]), db
        )
    assert user["allowed_logics"] == {"ARGOS"}
    assert any("BOGUS" in record.getMessage() for record in caplog.records)


def test_non_default_claim_names(db, monkeypatch):
    # Claim names are configuration, not literals (org Keycloak scheme
    # undecided): parsing follows the configured names.
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "logic_claim_name", "org_logics")
    monkeypatch.setattr(settings, "logic_admin_claim", "org_admin")
    claims = _claims()
    claims["org_logics"] = ["TEMPO", "ACCESS"]
    claims["org_admin"] = True
    _, user, _ = auth._user_from_claims(claims, db)
    assert user["allowed_logics"] == {"TEMPO", "ACCESS"}
    assert user["is_logic_admin"] is True


def test_dev_user_full_set_and_admin(db, monkeypatch):
    # The synthetic local identity is an admin: full set + admin, gating
    # enabled or not.
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    user = auth._dev_user(db)
    assert user["sub"] == "dev-admin"
    assert user["allowed_logics"] == ALL_LOGICS
    assert user["is_logic_admin"] is True


def test_verify_token_passes_entitlement_claims_through(kc_settings):
    # A token carrying the configured claims returns them under those
    # names; the identity keys are unchanged.
    token = _make_entitled_token(logics=["DMP"], admin=True)
    claims = auth.verify_token(token)
    assert claims["sub"] == f"user-{EMAIL}"
    assert claims["email"] == EMAIL
    assert claims["syzygy_logics"] == ["DMP"]
    assert claims["syzygy_admin"] is True


def test_verify_token_without_entitlement_claims_keeps_shape(kc_settings):
    # Tokens without the entitlement claims keep the three-key shape
    # (existing verify_token contract).
    token = _make_entitled_token()
    claims = auth.verify_token(token)
    assert set(claims) == {"sub", "email", "preferred_username"}


def test_auth_me_exposes_entitlements(client, db, monkeypatch):
    # /auth/me shape: both fields serialized (set -> list), identity
    # fields intact.
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    _, user, _ = auth._user_from_claims(
        _claims(logics=["ARGOS"], admin=True), db
    )
    app.dependency_overrides[auth.get_current_user] = lambda: user
    try:
        response = client.get("/v1/auth/me")
    finally:
        app.dependency_overrides.pop(auth.get_current_user, None)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == EMAIL
    assert body["display_name"] == "Logic User"
    assert isinstance(body["allowed_logics"], list)
    assert set(body["allowed_logics"]) == {"ARGOS"}
    assert body["is_logic_admin"] is True


def test_auth_me_dev_token_full_admin(client, monkeypatch):
    # Dev token end to end (gating off by default): full set + admin.
    monkeypatch.setattr(settings, "dev_auth_token", "dev-token-123")
    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer dev-token-123"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "dev-admin"
    assert set(body["allowed_logics"]) == ALL_LOGICS
    assert body["is_logic_admin"] is True


# ---------------------------------------------------------------------------
# P1 — GATING_OPEN_LOGICS: the default-open logic set (union semantics)
# ---------------------------------------------------------------------------

OPEN_DEFAULT = "ARGOS,TEMPO,ACCESS"
OPEN_SET = {"ARGOS", "TEMPO", "ACCESS"}


def test_open_set_visible_with_no_claims(db, monkeypatch):
    """P1: an authenticated principal with NO logic claim still sees the
    default-open set — the asymmetric policy (the open set applies to every
    authenticated principal, no enrollment needed)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "gating_open_logics", OPEN_DEFAULT)
    _, user, _ = auth._user_from_claims(_claims(), db)
    assert user["allowed_logics"] == OPEN_SET
    assert user["is_logic_admin"] is False


def test_claim_adds_gated_logic_to_open_set(db, monkeypatch):
    """P1: a claim for a GATED logic (DMP, not in the open set) adds to the
    open set — allowed = open ∪ claim; enrollment adds, never subtracts. A
    claim for an already-open logic (ARGOS) adds nothing (union)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "gating_open_logics", OPEN_DEFAULT)
    _, dmp_user, _ = auth._user_from_claims(_claims(logics=["DMP"]), db)
    assert dmp_user["allowed_logics"] == OPEN_SET | {"DMP"}
    _, argos_user, _ = auth._user_from_claims(_claims(logics=["ARGOS"]), db)
    assert argos_user["allowed_logics"] == OPEN_SET


def test_flag_off_ignores_open_set(db, monkeypatch):
    """P1 dark-launch: with the flag off the open set is irrelevant — the
    principal still gets the FULL set + admin (byte-identical to today),
    even when GATING_OPEN_LOGICS is non-empty."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    monkeypatch.setattr(settings, "gating_open_logics", OPEN_DEFAULT)
    _, user, _ = auth._user_from_claims(_claims(logics=["DMP"]), db)
    assert user["allowed_logics"] == ALL_LOGICS
    assert user["is_logic_admin"] is True


def test_open_set_empty_restores_uniform(db, monkeypatch):
    """P1: an empty GATING_OPEN_LOGICS restores today's uniform fail-closed
    model — claim-derived only (no claim -> empty set)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "gating_open_logics", "")
    _, nobody, _ = auth._user_from_claims(_claims(), db)
    assert nobody["allowed_logics"] == set()
    _, dmp_user, _ = auth._user_from_claims(_claims(logics=["DMP"]), db)
    assert dmp_user["allowed_logics"] == {"DMP"}


def test_open_set_unknown_names_ignored_with_warning(db, monkeypatch, caplog):
    """P1: unknown names in GATING_OPEN_LOGICS are ignored with a server
    warning (the same rule applied to the token claim)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "gating_open_logics", "ARGOS,BOGUS,TEMPO")
    with caplog.at_level("WARNING", logger="syzygy.auth"):
        _, user, _ = auth._user_from_claims(_claims(), db)
    assert user["allowed_logics"] == {"ARGOS", "TEMPO"}
    assert any("BOGUS" in record.getMessage() for record in caplog.records)


def test_auth_me_reflects_open_union(client, db, monkeypatch):
    """P1: /auth/me exposes the open-set ∪ claim union so the UI View
    selector matches server-side truth."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    monkeypatch.setattr(settings, "gating_open_logics", OPEN_DEFAULT)
    _, user, _ = auth._user_from_claims(_claims(logics=["DMP"]), db)
    app.dependency_overrides[auth.get_current_user] = lambda: user
    try:
        response = client.get("/v1/auth/me")
    finally:
        app.dependency_overrides.pop(auth.get_current_user, None)
    assert response.status_code == 200
    body = response.json()
    assert set(body["allowed_logics"]) == OPEN_SET | {"DMP"}
    assert body["is_logic_admin"] is False
