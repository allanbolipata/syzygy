"""Tests for the audit query + visibility + reversal API
(AUDIT_LOG_TICKETS.md, ticket AL2).

Visibility matrix (decision 5): an admin sees every row; a group user
sees the events of the pairing logics in their groups plus their own
actions; a user with no groups sees only their own actions; a NULL
``pairing_logic`` is admin-only unless it is the actor's own action
(including the ``payload.pairing_logic`` fallback key).

Query API (decision 7): server-side filters, limit<=500, offset,
newest first, per-row ``reversible`` flag.

Reversal (decision 6/8): the original row is never modified; the
compensating action runs the normal code path and the new event
carries ``reverse_of``; permission is the actor of the event or an
admin; irreversible actions 400 with a reason.

HTTP-level TestClient tests (auth + get_db overridden — same pattern
as tests/test_list_totals.py) confirm the /v1/audit routes are wired
and the visibility is applied server-side, not client-side.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.audit as audit
from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app
from syzygy.models import AllowedUser, AuditLog, PairingLogic, PairingTable, Sample
from syzygy.pairing_service import trigger_pairing
from syzygy.routers.samples import create_sample, redact_sample
from syzygy.schemas import SampleCreate, TumorOrNormal

ADMIN = "admin@x.com"
CAROL = "carol@x.com"   # user, groups: ["TEMPO"]
DAVE = "dave@x.com"     # user, no groups


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
        audit.reset_table_cache()
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def client(db):
    """Bare TestClient with get_db + get_current_user overridden; the
    acting user is mutable via the returned ``state`` dict."""

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


def _allowed_user(db, email, *, role="user", groups=(), active=True):
    u = db.scalar(select(AllowedUser).where(AllowedUser.email == email))
    if u is None:
        u = AllowedUser(
            email=email, role=role, logic_groups=list(groups), active=active
        )
        db.add(u)
    else:
        u.role = role
        u.logic_groups = list(groups)
        u.active = active
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


def _seed_matrix(db):
    """The decision-5 fixture: four events across two logics, a logic-
    free event, and three viewers (admin / TEMPO group / no groups)."""
    _allowed_user(db, ADMIN, role="admin")
    _allowed_user(db, CAROL, groups=["TEMPO"])
    _allowed_user(db, DAVE)
    e_carol_tempo = _event(
        db, "pairing.created", actor=CAROL, entity_type="pairing",
        entity_id="P-CAROL", pairing_logic="TEMPO",
    )
    e_dave_impact = _event(
        db, "pairing.created", actor=DAVE, entity_type="pairing",
        entity_id="P-DAVE", pairing_logic="IMPACT",
    )
    e_dave_nologic = _event(
        db, "sample.updated", actor=DAVE, entity_id="S-1",
    )
    e_system_tempo = _event(
        db, "pairing.created", actor="system", entity_type="pairing",
        entity_id="P-SYS", pairing_logic="TEMPO",
    )
    return {
        "carol_tempo": e_carol_tempo.id,
        "dave_impact": e_dave_impact.id,
        "dave_nologic": e_dave_nologic.id,
        "system_tempo": e_system_tempo.id,
    }


def _visible_ids(res):
    """The visible event ids (JSON serializes UUIDs as strings)."""
    assert res.status_code == 200
    return {UUID(row["id"]) for row in res.json()["rows"]}


# ---------------------------------------------------------------------------
# Visibility matrix (decision 5)
# ---------------------------------------------------------------------------


def test_visibility_matrix_admin_sees_everything(client, db):
    ev = _seed_matrix(db)
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    assert _visible_ids(c.get("/v1/audit/")) == set(ev.values())


def test_visibility_matrix_group_user(client, db):
    ev = _seed_matrix(db)
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    got = _visible_ids(c.get("/v1/audit/"))
    # Own TEMPO action + the system TEMPO event (group) — but NOT the
    # other group's (IMPACT) event and NOT the logic-free event.
    assert got == {ev["carol_tempo"], ev["system_tempo"]}


def test_visibility_matrix_no_groups_own_actions_only(client, db):
    ev = _seed_matrix(db)
    c, state = client
    _as(c, state, db, DAVE)
    got = _visible_ids(c.get("/v1/audit/"))
    assert got == {ev["dave_impact"], ev["dave_nologic"]}


def test_dev_admin_without_allowed_row_is_admin(client, db):
    """The dev-token local admin (auth._dev_user) has no allowed_users
    row by design — its admin-ness lives in the token claims, so a
    claim with is_logic_admin must resolve to role=admin and full
    visibility."""
    ev = _seed_matrix(db)
    c, state = client
    state["user"] = {
        "sub": "dev-admin",
        "email": "dev-admin@syzygy.local",
        "is_logic_admin": True,
    }
    me = c.get("/v1/audit/me")
    assert me.status_code == 200 and me.json()["role"] == "admin"
    assert _visible_ids(c.get("/v1/audit/")) == set(ev.values())


def test_payload_pairing_logic_fallback_counts_for_groups(client, db):
    """A row with NULL column but payload.pairing_logic is visible to
    the group, like a column value (decision 5)."""
    _allowed_user(db, CAROL, groups=["TEMPO"])
    _allowed_user(db, DAVE)
    e = _event(
        db, "pairing.created", actor="other@x.com", entity_type="pairing",
        entity_id="P-FB", payload={"pairing_logic": "TEMPO"},
    )
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    assert e.id in _visible_ids(c.get("/v1/audit/"))
    _as(c, state, db, DAVE)
    assert e.id not in _visible_ids(c.get("/v1/audit/"))


# ---------------------------------------------------------------------------
# Query API: filters, pagination, limit cap (decision 7)
# ---------------------------------------------------------------------------


def _iso(dt):
    return dt.isoformat()


def test_query_filters(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    _event(db, "a.one", actor="u1@x.com", entity_type="pairing", entity_id="E1", pairing_logic="TEMPO")
    _event(db, "a.two", actor="u2@x.com", entity_type="sample", entity_id="E2")
    _event(db, "a.one", actor="u2@x.com", entity_type="pairing", entity_id="E3", pairing_logic="IMPACT")

    got = c.get("/v1/audit/", params={"action": "a.one"})
    assert {r["entity_id"] for r in got.json()["rows"]} == {"E1", "E3"}

    got = c.get("/v1/audit/", params={"actor": "u2@x.com"})
    assert {r["entity_id"] for r in got.json()["rows"]} == {"E2", "E3"}

    got = c.get("/v1/audit/", params={"entity_type": "pairing", "entity_id": "E1"})
    assert [r["entity_id"] for r in got.json()["rows"]] == ["E1"]

    got = c.get("/v1/audit/", params={"pairing_logic": "IMPACT"})
    assert [r["entity_id"] for r in got.json()["rows"]] == ["E3"]

    # Time window: everything is "now", so a wide window matches all
    # three and a window entirely in the past matches none.
    now = datetime.now(timezone.utc)
    got = c.get("/v1/audit/", params={
        "from": _iso(now - timedelta(hours=1)),
        "to": _iso(now + timedelta(hours=1)),
    })
    assert got.json()["total"] == 3
    got = c.get("/v1/audit/", params={"to": _iso(now - timedelta(days=1))})
    assert got.json()["total"] == 0 and got.json()["rows"] == []


def test_query_pagination_newest_first_and_total(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    for i in range(7):
        _event(db, "p.event", actor="u@x.com", entity_type="pairing", entity_id=f"PG{i}")

    res = c.get("/v1/audit/", params={"limit": 3, "offset": 0})
    assert res.json()["total"] == 7
    page = res.json()["rows"]
    assert len(page) == 3
    # Newest first: the page covers the three most recent inserts.
    assert page[0]["entity_id"] == "PG6" and page[-1]["entity_id"] == "PG4"

    res = c.get("/v1/audit/", params={"limit": 3, "offset": 3})
    rows = res.json()["rows"]
    assert [r["entity_id"] for r in rows] == ["PG3", "PG2", "PG1"]
    assert rows[0]["occurred_at"] <= page[-1]["occurred_at"]

    # Pages tile the full set without overlap.
    all_ids = [r["entity_id"] for r in page]
    all_ids += [r["entity_id"] for r in rows]
    res = c.get("/v1/audit/", params={"limit": 3, "offset": 6})
    all_ids += [r["entity_id"] for r in res.json()["rows"]]
    assert sorted(all_ids) == sorted(f"PG{i}" for i in range(7))


def test_query_limit_capped_at_500(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    res = c.get("/v1/audit/", params={"limit": 501})
    assert res.status_code == 422
    res = c.get("/v1/audit/", params={"limit": 0})
    assert res.status_code == 422


def test_entity_timeline(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    _event(db, "e.one", actor="u@x.com", entity_type="pairing", entity_id="T-9")
    _event(db, "e.two", actor="u@x.com", entity_type="sample", entity_id="T-9")
    _event(db, "e.three", actor="u@x.com", entity_type="sample", entity_id="OTHER")
    res = c.get("/v1/audit/entity/pairing/T-9")
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert res.json()["rows"][0]["entity_id"] == "T-9"


def test_rows_carry_reversible_flag(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    rev = _event(
        db, "pairing.created", actor="u@x.com", entity_type="pairing",
        entity_id="P-R", payload={"trigger_type": "override",
                                  "replaced_pairing_id": "0" * 32},
    )
    not_rev = _event(
        db, "pairing.created", actor="u@x.com", entity_type="pairing",
        entity_id="P-M", payload={"trigger_type": "manual"},
    )
    flags = {
        r["entity_id"]: r["reversible"]
        for r in c.get("/v1/audit/").json()["rows"]
    }
    assert flags["P-R"] is True
    assert flags["P-M"] is False
    assert rev.id is not None


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


def test_me_returns_role_and_groups(client, db):
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO", "IMPACT"])
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert res.json() == {"email": CAROL, "role": "user",
                          "logic_groups": ["TEMPO", "IMPACT"]}


def test_me_admin_role(client, db):
    c, state = client
    _as(c, state, db, ADMIN, role="admin")
    assert c.get("/v1/audit/me").json()["role"] == "admin"


def test_me_least_privilege_for_unknown_user(client, db):
    c, state = client
    state["user"] = {"sub": "ghost", "email": "ghost@x.com"}  # no allowed_users row
    res = c.get("/v1/audit/me")
    assert res.status_code == 200
    assert res.json() == {"email": "ghost@x.com", "role": "user", "logic_groups": []}
    # ...and they see nothing they did not act on.
    assert c.get("/v1/audit/").json()["rows"] == []


# ---------------------------------------------------------------------------
# Reversal (decision 6/8)
# ---------------------------------------------------------------------------


def _seed_pairing_inputs(db):
    """Copy of the AL1 fixture: TEMPO logic + tumor + two normals."""
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    tumor = Sample(
        primary_id="TUMOR-R-1",
        cmo_sample_name="Tumor R1",
        patient__cmo="PATIENT-R1",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    normal = Sample(
        primary_id="NORMAL-R-1",
        cmo_sample_name="Normal R1",
        patient__cmo="PATIENT-R1",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    normal2 = Sample(
        primary_id="NORMAL-R-2",
        cmo_sample_name="Normal R2",
        patient__cmo="PATIENT-R1",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    db.add_all([tumor, normal, normal2])
    db.commit()
    db.refresh(logic)
    return logic, tumor, normal, normal2


def _latest(db, tumor, logic):
    return db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor.id,
            PairingTable.pairing_logic_id == logic.id,
            PairingTable.latest.is_(True),
        )
    ).one()


def _pairing_created_event(db, pairing_id):
    return db.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "pairing",
            AuditLog.entity_id == str(pairing_id),
            AuditLog.action == "pairing.created",
        )
    )


def test_reversal_override_restores_previous_pairing_and_links_event(client, db):
    """The AL2 Accept criterion: reversing an override restores the old
    pairing and writes a linked audit event (reverse_of), without
    touching the original row."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    first = trigger_pairing(db, tumor, logic, "manual", normal_id=normal.id, actor=CAROL)
    override = trigger_pairing(db, tumor, logic, "override", normal_id=normal2.id, actor=CAROL)
    assert override.id != first.id
    event = _pairing_created_event(db, override.id)
    assert event is not None and event.payload["replaced_pairing_id"] == str(first.id)

    _as(c, state, db, CAROL, groups=["TEMPO"])
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    body = res.json()[0]  # one compensating event for a single-pair reversal
    assert body["action"] == "pairing.created"
    assert UUID(body["reverse_of"]) == event.id
    assert body["actor"] == CAROL

    # The old pairing's normal is restored and the override is retired.
    latest = _latest(db, tumor, logic)
    assert latest.normal_id == normal.id
    assert db.get(PairingTable, override.id).latest is False

    # The original event is untouched (append-only).
    orig = db.get(AuditLog, event.id)
    assert orig.reverse_of is None
    assert orig.payload["trigger_type"] == "override"
    # And the new event's pairing carries the link in its payload too.
    new_event = db.get(AuditLog, UUID(body["id"]))
    assert new_event.payload["replaced_pairing_id"] == str(override.id)


def test_reversal_override_that_created_first_pairing_is_400(client, db):
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    # No prior pairing: the "override" has nothing to restore.
    pairing = trigger_pairing(db, tumor, logic, "override", normal_id=normal.id, actor=CAROL)
    event = _pairing_created_event(db, pairing.id)
    assert event.payload.get("replaced_pairing_id") is None
    _as(c, state, db, CAROL, groups=["TEMPO"])
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 400
    assert "no previous pairing" in res.json()["detail"]


def test_reversal_non_override_pairing_is_400(client, db):
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    pairing = trigger_pairing(db, tumor, logic, "manual", normal_id=normal.id, actor=CAROL)
    event = _pairing_created_event(db, pairing.id)
    _as(c, state, db, CAROL, groups=["TEMPO"])
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 400
    assert "Only override pairings" in res.json()["detail"]


def test_reversal_irreversible_action_is_400(client, db):
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    e = _event(db, "sample.updated", actor=CAROL, entity_id="S-42")
    res = c.post(f"/v1/audit/{e.id}/reverse")
    assert res.status_code == 400
    assert "not reversible" in res.json()["detail"]


def test_reversal_missing_event_is_404(client, db):
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    missing = "0" * 31 + "1"
    res = c.post(f"/v1/audit/{missing}/reverse")
    assert res.status_code == 404


def test_reversal_of_another_users_event_requires_admin(client, db):
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    first = trigger_pairing(db, tumor, logic, "manual", normal_id=normal.id, actor=CAROL)
    override = trigger_pairing(db, tumor, logic, "override", normal_id=normal2.id, actor=CAROL)
    event = _pairing_created_event(db, override.id)

    _allowed_user(db, ADMIN, role="admin")
    _allowed_user(db, DAVE)

    # A non-admin who is NOT the actor: 403 (and a group-less user gets
    # the visibility 403 first — either way, not 200).
    _as(c, state, db, DAVE)
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 403

    # The admin may reverse another user's event.
    _as(c, state, db, ADMIN, role="admin")
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    assert res.json()[0]["actor"] == ADMIN
    assert UUID(res.json()[0]["reverse_of"]) == event.id
    assert _latest(db, tumor, logic).normal_id == normal.id


def test_reversal_override_superseded_is_409(client, db):
    """If the override has since been replaced, reversing it would
    clobber a newer pairing — 409, point at the current one."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    first = trigger_pairing(db, tumor, logic, "manual", normal_id=normal.id, actor=CAROL)
    override = trigger_pairing(db, tumor, logic, "override", normal_id=normal2.id, actor=CAROL)
    third = trigger_pairing(db, tumor, logic, "override", normal_id=normal.id, actor=CAROL)
    assert third.id not in (first.id, override.id)
    event = _pairing_created_event(db, override.id)

    _as(c, state, db, CAROL, groups=["TEMPO"])
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 409
    assert "since been replaced" in res.json()["detail"]


def test_reversal_sample_redacted(client, db):
    c, state = client
    _as(c, state, db, CAROL, groups=["TEMPO"])
    sample = create_sample(
        SampleCreate(
            primary_id="TUMOR-RED-R",
            cmo_sample_name="Tumor Red R",
            patient__cmo="PATIENT-RED-R",
            sample_type="tissue",
            gene_panel="PANEL-A",
            bait_set="BAIT-1",
            tumor_or_normal=TumorOrNormal.TUMOR,
        ),
        db=db,
        current_user={"email": CAROL},
    )
    redact_sample(sample_id=sample.id, payload=None, db=db, current_user={"email": CAROL})
    event = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == str(sample.id),
            AuditLog.action == "sample.redacted",
        )
    )
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    assert res.json()[0]["action"] == "sample.unredacted"
    assert UUID(res.json()[0]["reverse_of"]) == event.id
    db.refresh(sample)
    assert sample.redacted is False

    # Reversing again: the sample is no longer redacted — 409.
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Reversal of pair_assignment.imported (decision 6, per-pair)
# ---------------------------------------------------------------------------


def _import_event(c, db, text, logic="TEMPO", **flags):
    """Run the REAL import endpoint as the current user; return
    ``(response body, the pair_assignment.imported event)``."""
    body = {"text": text, "logic": logic, "id_field": "primary_id"}
    body.update(flags)
    res = c.post("/v1/pairing/import", json=body)
    assert res.status_code == 200, res.text
    event = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "pair_assignment.imported",
            AuditLog.pairing_logic == logic,
        )
    )
    assert event is not None
    return res.json(), event


def test_reversal_import_per_pair_undoes_the_imported_pairing(client, db):
    """Decision 6: reversing an import re-runs the normal pairing path
    per created pair — the strategy's own selection replaces the
    imported row, the new event carries reverse_of, and the original
    import row is untouched (append-only)."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    # The default strategy's own selection is the LATEST normal
    # (normal2, 2024-01-03); the import pins the earlier one, so the
    # reversal has something to undo.
    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db, "NORMAL_ID\tTUMOR_ID\nNORMAL-R-1\tTUMOR-R-1\n"
    )
    # Two matching candidate normals -> the CLI's MULTI status; the
    # curated pin is still created (pairing_id is set).
    assert body["rows"][0]["status"] == "MULTI"
    imported_id = body["rows"][0]["pairing_id"]
    assert imported_id
    assert _latest(db, tumor, logic).id == UUID(imported_id)

    # The viewer row carries reversible: true (a pairing was created).
    rows = c.get(
        "/v1/audit/", params={"action": "pair_assignment.imported"}
    ).json()["rows"]
    assert len(rows) == 1 and rows[0]["reversible"] is True

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["action"] == "pairing.created"
    assert UUID(body[0]["reverse_of"]) == event.id
    assert body[0]["actor"] == CAROL

    # Real behavior: the strategy's own selection (normal2) is latest
    # again and the imported row is retired.
    latest = _latest(db, tumor, logic)
    assert latest.normal_id == normal2.id
    assert db.get(PairingTable, UUID(imported_id)).latest is False

    # The compensating event names the replaced row in its payload.
    new_event = db.get(AuditLog, UUID(body[0]["id"]))
    assert new_event.reverse_of == event.id
    assert new_event.payload["replaced_pairing_id"] == imported_id
    # The original import event is untouched (append-only).
    orig = db.get(AuditLog, event.id)
    assert orig.reverse_of is None
    assert orig.payload["rows"][0]["pairing_id"] == imported_id


def test_reversal_import_multi_pair_reverses_every_created_pairing(client, db):
    """A two-row import yields two compensating events, each linked to
    the same import event — one per pair (decision 6 'per-pair')."""
    c, state = client
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    def _samples(tag, patient, day):
        tumor = Sample(
            primary_id=f"TUMOR-IMP-{tag}",
            cmo_sample_name=f"Tumor Imp {tag}",
            patient__cmo=patient,
            sample_type="tissue",
            gene_panel="PANEL-A",
            bait_set="BAIT-1",
            tumor_or_normal=TumorOrNormal.TUMOR.value,
            sequencing_date=datetime(2024, 2, 1),
        )
        pinned = Sample(
            primary_id=f"NORMAL-IMP-{tag}-1",
            cmo_sample_name=f"Normal Imp {tag} 1",
            patient__cmo=patient,
            sample_type="tissue",
            gene_panel="PANEL-A",
            bait_set="BAIT-1",
            tumor_or_normal=TumorOrNormal.NORMAL.value,
            sequencing_date=datetime(2024, 2, 2 + day),
        )
        own = Sample(
            primary_id=f"NORMAL-IMP-{tag}-2",
            cmo_sample_name=f"Normal Imp {tag} 2",
            patient__cmo=patient,
            sample_type="tissue",
            gene_panel="PANEL-A",
            bait_set="BAIT-1",
            tumor_or_normal=TumorOrNormal.NORMAL.value,
            sequencing_date=datetime(2024, 2, 5 + day),
        )
        db.add_all([tumor, pinned, own])
        return tumor, pinned, own

    t1, _, own1 = _samples("1", "PAT-IMP-1", 0)
    t2, _, own2 = _samples("2", "PAT-IMP-2", 1)
    db.commit()

    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db,
        "NORMAL_ID\tTUMOR_ID\n"
        "NORMAL-IMP-1-1\tTUMOR-IMP-1\n"
        "NORMAL-IMP-2-1\tTUMOR-IMP-2\n",
    )
    assert [r["status"] for r in body["rows"]] == ["MULTI", "MULTI"]
    imported_ids = [r["pairing_id"] for r in body["rows"]]
    assert all(imported_ids)

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    done = res.json()
    assert len(done) == 2
    # Every compensating event links to the SAME import event.
    assert {UUID(d["reverse_of"]) for d in done} == {event.id}
    assert all(d["action"] == "pairing.created" for d in done)
    # Both imported rows are retired; the strategy's own normal is
    # latest again — per pair.
    for pairing_id, tumor, own in zip(imported_ids, (t1, t2), (own1, own2)):
        assert db.get(PairingTable, UUID(pairing_id)).latest is False
        assert _latest(db, tumor, logic).normal_id == own.id


def test_reversal_import_mixed_statuses_reverses_only_created_pairs(client, db):
    """A FAIL row (unresolvable normal) created nothing — it is skipped
    and the pinned (MULTI) row is still reversed."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db,
        "NORMAL_ID\tTUMOR_ID\n"
        "NORMAL-R-1\tTUMOR-R-1\n"
        "GHOST-NORMAL\tTUMOR-R-1\n",
    )
    assert [r["status"] for r in body["rows"]] == ["MULTI", "FAIL"]
    assert body["rows"][1]["pairing_id"] is None

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    done = res.json()
    assert len(done) == 1
    assert UUID(done[0]["reverse_of"]) == event.id
    # The imported pairing is gone; the strategy's own normal is latest.
    assert _latest(db, tumor, logic).normal_id == normal2.id


def test_reversal_dry_run_import_is_not_reversible(client, db):
    """A dry-run import creates no pairings — the viewer row's flag is
    False and the reversal 400s (nothing to undo)."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db, "NORMAL_ID\tTUMOR_ID\nNORMAL-R-1\tTUMOR-R-1\n", dry_run=True
    )
    assert body["dry_run"] is True
    assert body["rows"][0]["pairing_id"] is None
    assert list(db.scalars(select(PairingTable))) == []

    rows = c.get(
        "/v1/audit/", params={"action": "pair_assignment.imported"}
    ).json()["rows"]
    assert len(rows) == 1 and rows[0]["reversible"] is False

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 400
    assert "not reversible" in res.json()["detail"]


def test_reversal_import_superseded_is_409(client, db):
    """If the imported pairing has since been replaced, reversing the
    import would clobber the newer pairing — 409 with the per-row
    reason."""
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db, "NORMAL_ID\tTUMOR_ID\nNORMAL-R-1\tTUMOR-R-1\n"
    )
    imported_id = body["rows"][0]["pairing_id"]
    # A newer pairing replaces the import.
    override = trigger_pairing(
        db, tumor, logic, "override", normal_id=normal2.id, actor=CAROL
    )
    assert override.id != UUID(imported_id)

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 409
    assert "Nothing to reverse" in res.json()["detail"]
    assert "since been replaced" in res.json()["detail"]
    # The newer pairing is untouched.
    assert _latest(db, tumor, logic).id == override.id


def test_reversal_import_of_another_users_event_requires_admin(client, db):
    c, state = client
    logic, tumor, normal, normal2 = _seed_pairing_inputs(db)
    _as(c, state, db, CAROL, groups=["TEMPO"])
    body, event = _import_event(
        c, db, "NORMAL_ID\tTUMOR_ID\nNORMAL-R-1\tTUMOR-R-1\n"
    )
    _allowed_user(db, ADMIN, role="admin")
    _allowed_user(db, DAVE)
    _allowed_user(db, "erin@x.com", groups=["TEMPO"])

    # A user with no groups cannot even see the event: 403.
    _as(c, state, db, DAVE)
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 403

    # A group user who is NOT the actor: visible, but not the actor and
    # not admin — 403.
    _as(c, state, db, "erin@x.com", groups=["TEMPO"])
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 403
    assert "admin" in res.json()["detail"]

    # The admin may reverse another user's import.
    _as(c, state, db, ADMIN, role="admin")
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 200, res.text
    done = res.json()
    assert len(done) == 1
    assert done[0]["actor"] == ADMIN
    assert UUID(done[0]["reverse_of"]) == event.id
    assert _latest(db, tumor, logic).normal_id == normal2.id


def test_reversal_import_fan_out_logic_is_409(client, db):
    """Under a multi-pair (DMP) logic the strategy's own cascade fans
    out to the full match set — not expressible as one linked
    compensating event per pair — so the row is skipped and, with no
    other rows, the reversal 409s without touching the pairing."""
    c, state = client
    logic = PairingLogic(name="DMP", strategy_name="dmp")
    db.add(logic)
    tumor = Sample(
        primary_id="TUMOR-DMP-R",
        cmo_sample_name="Tumor DMP R",
        patient__cmo="PAT-DMP-R",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        sequencing_date=datetime(2024, 3, 1),
    )
    normal = Sample(
        primary_id="NORMAL-DMP-R",
        cmo_sample_name="Normal DMP R",
        patient__cmo="PAT-DMP-R",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 3, 2),
    )
    db.add_all([tumor, normal])
    db.commit()
    db.refresh(logic)

    _as(c, state, db, ADMIN, role="admin")
    body, event = _import_event(
        c, db,
        "NORMAL_ID\tTUMOR_ID\nNORMAL-DMP-R\tTUMOR-DMP-R\n",
        logic="DMP",
    )
    imported_id = body["rows"][0]["pairing_id"]
    assert imported_id

    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 409
    assert "fan-out" in res.json()["detail"]
    # The imported row is untouched.
    assert db.get(PairingTable, UUID(imported_id)).latest is True
