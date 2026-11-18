"""Tests for the Operations (Ops) tab (AUDIT_LOG_TICKETS.md, ticket AL3b —
decision 8: a/b/c + "UI hidden for users without role/groups").

The Ops tab is a no-build static front end (static/index.html +
static/app.js), so its tests have two halves:

1. WIRING — the served HTML/JS actually carries the decision-8 surface:
   the Ops tab hidden by default, the three sections (pair assignment
   wired to POST /v1/pairing/import with the CLI's exact flags; the
   audit log viewer wired to GET /v1/audit with the server's exact
   filter names, payload expanders, the reversible-row Reverse button
   and a CSV export capped at the API's 500-row limit; redaction wired
   to the R-feature search/redact/unredact endpoints), the visibility
   gate reading /v1/audit/me, and the #/ops hash guard.

2. GATE + FILTER CONTRACT — the server halves the UI depends on:
   /v1/audit/me returns role + logic_groups, and applying the exact
   decision the tab applies (admin role OR >=1 logic group) to each
   class of identity yields the tab's visibility (admin -> shown, group
   user -> shown, plain user -> hidden, unknown user -> hidden). The
   filter bar's query names (from/to/actor/action/pairing_logic +
   limit/offset, time bounds in the datetime-local shape the UI builds)
   are proven server-side on the same /v1/audit/ route, including the
   UI's seconds-appended format.

Manual check (documented, per the ticket's Accept):
  1. Log in as an admin -> the "Ops" tab is visible; a group-less user
     sees no Ops tab, and a hand-typed #/ops bounces to Samples.
  2. Ops -> Pair assignment: paste the TEMPO TSV (or upload a .tsv,
     which fills the paste box), dry run -> per-row statuses; untick
     dry run -> Import pins the pairs and the pairing log downloads as
     the exact CLI --log TSV.
  3. Ops -> Audit log: set from/to/actor/action/logic -> Apply filters
     the rows server-side; expand a payload; Reverse on a reversible
     row writes the linked compensating event; Export CSV downloads the
     current filter set (banner when >500 rows).
  4. Ops -> Redaction: search a sample, Redact with a reason -> the row
     flags REDACTED; Un-redact restores pairing eligibility.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.audit as audit
from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app
from syzygy.models import AllowedUser

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "static" / "index.html"
APP_JS = REPO_ROOT / "static" / "app.js"

ADMIN = "admin@x.com"
CAROL = "carol@x.com"  # user, groups: ["TEMPO"]
DAVE = "dave@x.com"  # user, no groups
GHOST = "ghost@x.com"  # no allowed_users row at all


def _html():
    return INDEX_HTML.read_text()


def _js():
    return APP_JS.read_text()


# ---------------------------------------------------------------------------
# 1. WIRING — the Ops tab surface exists and is gated
# ---------------------------------------------------------------------------


def test_ops_tab_present_and_hidden_by_default():
    html, js = _html(), _js()
    m = re.search(r'<button class="tab[^"]*" data-tab="ops">Ops</button>', html)
    assert m, "the Ops tab button must exist in the nav"
    assert "hidden" in m.group(0), "the Ops tab must be hidden by default"
    assert '<main id="view-ops" class="view hidden">' in html
    # "ops" is a restorable view (hash navigation works both ways).
    assert re.search(r'VIEWS\s*=\s*\[[^\]]*"ops"[^\]]*\]', js)


def test_ops_gate_reads_audit_me_and_hides_without_role_or_groups():
    js = _js()
    # The gate: /v1/audit/me's role + logic_groups decide visibility —
    # visible iff the admin role or >=1 logic group (decision 8).
    assert 'api("/audit/me")' in js
    assert 'me.role === "admin" || (me.logic_groups || []).length > 0' in js
    # ...and the decision toggles the tab's hidden class.
    assert 'opsTab().classList.toggle("hidden", !entitled)' in js
    # The gate runs during init, before any hash restore.
    assert "await loadOpsAccess();" in js


def test_ops_hash_guard_bounces_non_entitled_sessions():
    """A hand-typed #/ops from a session without role/groups must not
    render the restricted view: showView bounces to samples."""
    assert 'name === "ops" && !opsAccess' in _js()


def test_import_panel_wired_to_endpoint():
    html, js = _html(), _js()
    for eid in [
        "ops-import-text", "ops-import-file", "ops-import-logic",
        "ops-import-trigger", "ops-import-idfield", "ops-import-dryrun",
        "ops-import-strict", "ops-import-run", "ops-import-rows",
        "ops-import-log",
    ]:
        assert f'id="{eid}"' in html, f"missing #{eid} in the import panel"
    # The panel posts to the AL3a endpoint (shared core with the CLI —
    # not a reimplementation) with the CLI's exact flags.
    assert 'api("/pairing/import"' in js
    for flag in ["trigger_type", "id_field", "strict", "dry_run", "logic"]:
        assert f"{flag}:" in js
    # File upload feeds the paste box (review before import); the
    # downloadable pairing log is the endpoint's log_tsv.
    assert "readAsText" in js
    assert "log_tsv" in js
    # Per-row statuses render with the CLI's four values.
    for status in ["OK", "MISMATCH", "MULTI", "FAIL"]:
        assert status in js


def test_audit_viewer_wired_with_the_servers_exact_filter_names():
    html, js = _html(), _js()
    for eid in [
        "ops-audit-from", "ops-audit-to", "ops-audit-actor",
        "ops-audit-action", "ops-audit-logic", "ops-audit-limit",
        "ops-audit-apply", "ops-audit-clear", "ops-audit-export",
        "ops-audit-prev", "ops-audit-next", "ops-audit-rows",
    ]:
        assert f'id="{eid}"' in html, f"missing #{eid} in the audit viewer"
    # Decision 7: the time bounds are aliased `from`/`to` — the UI must
    # send those names, not date_from/date_to.
    assert 'p.set("from", from + ":00")' in js
    assert 'p.set("to", to + ":59")' in js
    for name in ['"actor"', '"action"', '"pairing_logic"', '"limit"', '"offset"']:
        assert f"p.set({name}" in js, f"filter {name} not wired into the query"
    assert "api(`/audit/?${opsAuditParams()}`)" in js
    # Payload expanders + the Reverse button driven by the row's
    # server-computed reversible flag; reverse posts to the AL2 endpoint.
    assert "row.reversible" in js
    assert "/reverse" in js
    # CSV export pulls the API's 500-row cap and warns when capped.
    assert "exportMode ? 500" in js
    assert "capped at 500" in js


def test_redaction_panel_wired_to_the_r_endpoints():
    html, js = _html(), _js()
    for eid in ["ops-redact-q", "ops-redact-reason", "ops-redact-search", "ops-redact-rows"]:
        assert f'id="{eid}"' in html, f"missing #{eid} in the redaction panel"
    # Sample search reuses the listing endpoint's q filter ...
    assert "api(`/sample/?q=${encodeURIComponent(q)}&limit=50`)" in js
    # ... and the actions post to the R-feature redact/unredact routes,
    # with the operator reason in the redact body.
    assert "redact" in js and "unredact" in js
    assert "JSON.stringify({ reason: reason || null })" in js


# ---------------------------------------------------------------------------
# Fixtures — same pattern as tests/test_audit_api.py (AL2)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        audit.reset_table_cache()
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    state = {"user": {"sub": "test", "email": CAROL}}

    def override_user():
        return state["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    yield TestClient(app), state
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _allowed_user(db, email, *, role="user", groups=()):
    u = db.scalar(select(AllowedUser).where(AllowedUser.email == email))
    if u is None:
        u = AllowedUser(email=email, role=role, logic_groups=list(groups), active=True)
        db.add(u)
    else:
        u.role = role
        u.logic_groups = list(groups)
        u.active = True
    db.commit()
    return u


def _as(client, state, db, email, role="user", groups=()):
    _allowed_user(db, email, role=role, groups=groups)
    state["user"] = {"sub": email, "email": email}


def _event(db, action, *, actor="system", entity_type="sample", entity_id="X",
           pairing_logic=None, payload=None):
    row = audit.record(
        db,
        actor,
        action,
        entity_type=entity_type,
        entity_id=entity_id,
        pairing_logic=pairing_logic,
        payload=payload or {},
    )
    db.commit()
    return row


def _ops_visible_for(me_body):
    """The exact decision the Ops tab applies to the /v1/audit/me body
    (decision 8): admin role OR >=1 logic group."""
    return me_body["role"] == "admin" or len(me_body["logic_groups"]) > 0


# ---------------------------------------------------------------------------
# 2. GATE CONTRACT — /v1/audit/me drives the tab's visibility
# ---------------------------------------------------------------------------


def test_ops_gate_admin_sees_the_tab(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert _ops_visible_for(res.json()) is True


def test_ops_gate_group_user_sees_the_tab(client, db):
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert _ops_visible_for(res.json()) is True


def test_ops_gate_plain_user_without_groups_does_not_see_the_tab(client, db):
    c, state = client
    _as(c, state, db, DAVE)
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert _ops_visible_for(res.json()) is False


def test_ops_gate_unknown_user_does_not_see_the_tab(client, db):
    c, state = client
    state["user"] = {"sub": "ghost", "email": GHOST}  # no allowed_users row
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert res.json() == {"email": GHOST, "role": "user", "logic_groups": []}
    assert _ops_visible_for(res.json()) is False


# ---------------------------------------------------------------------------
# 3. FILTER CONTRACT — the viewer's query names + datetime shapes work
# ---------------------------------------------------------------------------


def test_ops_viewer_filters_narrow_the_result_set(client, db):
    """The filter bar sends from/to/actor/action/pairing_logic — the
    API's exact names (decision 7) — and each one narrows the set."""
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    _event(db, "pairing.created", actor="u1@x.com", entity_type="pairing",
           entity_id="E1", pairing_logic="TEMPO")
    _event(db, "sample.redacted", actor="u2@x.com", entity_type="sample",
           entity_id="E2", pairing_logic="IMPACT")
    _event(db, "nats.ingest", actor="system", entity_type="sample", entity_id="E3")

    assert {r["entity_id"] for r in c.get("/v1/audit/", params={"actor": "u1@x.com"}).json()["rows"]} == {"E1"}
    assert {r["entity_id"] for r in c.get("/v1/audit/", params={"action": "nats.ingest"}).json()["rows"]} == {"E3"}
    assert {r["entity_id"] for r in c.get("/v1/audit/", params={"pairing_logic": "IMPACT"}).json()["rows"]} == {"E2"}


def test_ops_viewer_time_filters_accept_the_uis_datetime_local_shape(client, db):
    """The UI's from/to arrive as ``YYYY-MM-DDTHH:MM`` + the seconds the
    JS appends (``:00`` / ``:59``) — the API must accept exactly that
    shape, and the window must include/exclude the right events."""
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    for i in range(3):
        _event(db, "p.event", actor="u@x.com", entity_type="pairing", entity_id=f"PG{i}")
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M") + ":00"
    to = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M") + ":59"
    res = c.get("/v1/audit/", params={"from": frm, "to": to})
    assert res.status_code == 200
    assert res.json()["total"] == 3
    # A window entirely in the past matches nothing.
    past = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M") + ":59"
    res = c.get("/v1/audit/", params={"to": past})
    assert res.json()["total"] == 0 and res.json()["rows"] == []


def test_ops_viewer_limit_and_offset_follow_the_api(client, db):
    """The viewer's per-page select (50/100/250/500) and pagination send
    limit/offset; the API enforces the 500 cap the export relies on."""
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    for i in range(10):
        _event(db, "p.event", actor="u@x.com", entity_type="pairing", entity_id=f"PG{i}")
    res = c.get("/v1/audit/", params={"limit": 50, "offset": 0})
    assert res.status_code == 200
    assert res.json()["total"] == 10 and len(res.json()["rows"]) == 10
    res = c.get("/v1/audit/", params={"limit": 500, "offset": 0})
    assert res.status_code == 200
    res = c.get("/v1/audit/", params={"limit": 501})
    assert res.status_code == 422
