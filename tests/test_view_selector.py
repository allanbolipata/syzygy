"""Tests for the session-wide pairing-logic View selector (card t_3e1444f0).

Part A (HTTP): the new GET /v1/pairing-logic/default endpoint exposes
``settings.default_pairing_logic_name`` verbatim — the client decides whether
it resolves to an ACTIVE logic, so the endpoint itself performs no activation
check and returns ``{"name": null}`` when unset.

Part B (static UI wiring, test_login_page.py convention): the topbar View
select exists, the three per-tab logic dropdowns (#pr-logic / #m-logic /
#tq-logic) are GONE, the three pairing-tab gate panels and View labels exist,
and app.js keeps its persistence/gating anchors (localStorage key
``syzygy.logic_view``, View-driven loaders, no ALL option).
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.routers.pairing_logic as pairing_logic_module
from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "static" / "index.html"
APP_JS = REPO_ROOT / "static" / "app.js"


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    def override_user():
        return {"sub": "test", "email": "test@example.com"}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    # Bare client (no context manager): the app lifespan is not run, so
    # no real database engine is used.
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


# ---------- Part A: /pairing-logic/default endpoint ----------


def test_default_endpoint_returns_configured_name(client, monkeypatch):
    monkeypatch.setattr(
        pairing_logic_module.settings, "default_pairing_logic_name", "ARGOS"
    )
    res = client.get("/v1/pairing-logic/default")
    assert res.status_code == 200
    assert res.json() == {"name": "ARGOS"}


def test_default_endpoint_null_when_unset(client, monkeypatch):
    monkeypatch.setattr(
        pairing_logic_module.settings, "default_pairing_logic_name", None
    )
    res = client.get("/v1/pairing-logic/default")
    assert res.status_code == 200
    assert res.json() == {"name": None}


def test_default_endpoint_is_inclusive_of_listing(client, monkeypatch):
    # The endpoint passes the setting verbatim: even a name with no
    # PairingLogic row (inactive/unregistered) is returned — activation is
    # resolved client-side against GET /pairing-logic/ (active-only options).
    monkeypatch.setattr(
        pairing_logic_module.settings, "default_pairing_logic_name", "NOT-A-LOGIC"
    )
    res = client.get("/v1/pairing-logic/default")
    assert res.status_code == 200
    assert res.json() == {"name": "NOT-A-LOGIC"}


# ---------- Part B: static UI wiring ----------


def test_topbar_view_select_exists_once():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.count('id="view-logic"') == 1
    assert '<label for="view-logic">View</label>' in html


def test_per_tab_logic_selectors_removed():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for stale in ('id="pr-logic"', 'id="m-logic"', 'id="tq-logic"'):
        assert stale not in html, f"{stale} should be gone (the View is the only logic control)"


def test_gates_view_labels_and_mismatch_hints_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for anchor in (
        'id="pairs-gate"', 'id="matches-gate"', 'id="try-gate"',
        'id="pr-view-label"', 'id="m-view-label"', 'id="tq-view-label"',
        'id="m-logic-mismatch"', 'id="tq-logic-mismatch"',
    ):
        assert anchor in html, anchor


def test_no_all_option_anywhere():
    # "ALL" was removed (spec update 2): no ALL option in the static HTML,
    # and app.js builds the View options without one.
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'value="ALL"' not in html
    js = APP_JS.read_text(encoding="utf-8")
    view_section = js[js.index("#view-logic"): js.index("#view-logic") + 4000]
    assert 'value="ALL"' not in view_section


def test_app_js_view_wiring_anchors():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'const VIEW_LOGIC_KEY = "syzygy.logic_view";' in js
    assert "localStorage.getItem(VIEW_LOGIC_KEY)" in js
    assert "localStorage.setItem(VIEW_LOGIC_KEY" in js
    assert "async function resolveInitialViewLogic" in js
    assert 'api("/pairing-logic/default")' in js
    assert "function gatePairingTabs" in js
    assert 'const PAIRING_VIEWS = ["pairs", "matches", "try"];' in js
    assert "function rerenderPairingTabs" in js


def test_app_js_has_no_stale_per_tab_selector_reads():
    js = APP_JS.read_text(encoding="utf-8")
    for stale in ('$("#pr-logic")', '$("#m-logic")', '$("#tq-logic")'):
        assert stale not in js, f"{stale} read remains in app.js"
    # The View is read instead, everywhere logic is needed.
    for anchor in (
        "const logic = viewLogic; // the View IS the logic (no per-tab selector)",
    ):
        assert js.count(anchor) >= 3, anchor
