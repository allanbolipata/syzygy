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
    body = res.json()
    # The response IS the new (compensating) event, linked to the old.
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
    assert res.json()["actor"] == ADMIN
    assert UUID(res.json()["reverse_of"]) == event.id
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
    assert res.json()["action"] == "sample.unredacted"
    assert UUID(res.json()["reverse_of"]) == event.id
    db.refresh(sample)
    assert sample.redacted is False

    # Reversing again: the sample is no longer redacted — 409.
    res = c.post(f"/v1/audit/{event.id}/reverse")
    assert res.status_code == 409
