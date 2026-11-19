"""POST /v1/pairing/import — ticket AL3a (AUDIT_LOG_TICKETS.md, AL3
import half, decisions 8a + 9).

The endpoint runs the shared import core (``syzygy/pairing_import.py``)
in-process (``DbTransport``): the same row loop, statuses and pairing
log that ``scripts/import_tempo_pairs.py`` runs over HTTP, so these
tests pin the endpoint half of the parity contract:

- per-row statuses (OK / MISMATCH / MULTI / FAIL) and the summary
  counts (decision 8a: the endpoint returns the script's per-row
  statuses),
- dry-run (resolves and validates, creates nothing),
- the downloadable pairing log (``log_tsv``, the script's ``--log``
  file content — FAIL/MISMATCH/MULTI rows only),
- the decision-3 audit events (one ``pair_assignment.imported`` per
  file, plus the per-pairing ``pairing.created`` events with the
  caller as actor),
- the decision-8 access gate (admin or a logic-group member).

A live CLI-vs-endpoint parity receipt (same TSV, same fixture, both
paths) is run separately against a real server; the unit tests here
cover the endpoint's in-process contract.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app
from syzygy.models import AllowedUser, AuditLog, PairingLogic, PairingTable, Sample
from syzygy.pairing_import import LOG_HEADER
from syzygy.routers.pairings import pairing_import_endpoint
from syzygy.schemas import PairingImportCreate, TumorOrNormal

OPS = {"sub": "ops-1", "email": "ops@example.com", "preferred_username": "ops"}

TUMOR = TumorOrNormal.TUMOR.value
NORMAL = TumorOrNormal.NORMAL.value


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
def tempo(db):
    logic = PairingLogic(name="TEMPO", strategy_name="tempo")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _sample(
    db,
    primary_id,
    cmo,
    tumor_or_normal,
    patient__cmo,
    bait_set="agilent",
    run_mode="NovaSeq X 1000",
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo,
        patient__cmo=patient__cmo,
        sample_type="tissue",
        gene_panel="IMPACT468",
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        run_mode=run_mode,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _import(db, text, viewer=OPS, **flags):
    flags.setdefault("logic", "TEMPO")
    payload = PairingImportCreate(text=text, **flags)
    return pairing_import_endpoint(payload, db=db, current_user=viewer)


def _audit_rows(db, action):
    return db.scalars(
        select(AuditLog).where(AuditLog.action == action)
    ).all()


# ---------------------------------------------------------------------------
# Core import: OK / MISMATCH / FAIL + audit + log (decision 8a)
# ---------------------------------------------------------------------------


def test_import_endpoint_statuses_pairings_and_log(db, tempo, admin):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _sample(db, "P-T2", "T2-T", TUMOR, "C-2")
    _sample(db, "P-N2", "N2-N", NORMAL, "C-3")  # different patient -> mismatch
    _sample(db, "P-T3", "T3-T", TUMOR, "C-4")
    text = "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\nN2-N\tT2-T\nGHOST-N\tT3-T\n"

    res = _import(db, text)

    # Per-row statuses, in file order (the CLI's exact statuses).
    assert [(r["status"], r["normal_name"], r["tumor_name"]) for r in res["rows"]] == [
        ("OK", "N1-N", "T1-T"),
        ("MISMATCH", "N2-N", "T2-T"),
        ("FAIL", "GHOST-N", "T3-T"),
    ]
    assert res["total"] == 3
    assert res["pinned"] == 1
    assert res["mismatched"] == 1
    assert res["failed"] == 1
    assert res["dry_run"] is False
    assert res["logic"] == "TEMPO"

    # OK row: clean pairing, no mismatched keys.
    pairings = db.scalars(select(PairingTable)).all()
    assert len(pairings) == 2  # FAIL row created nothing
    by_tumor = {}
    for p in pairings:
        tumor = db.get(Sample, p.tumor_id)
        normal = db.get(Sample, p.normal_id)
        by_tumor[tumor.cmo_sample_name] = (p, tumor, normal)
    p1, _, n1 = by_tumor["T1-T"]
    assert n1.cmo_sample_name == "N1-N"
    assert p1.trigger_type == "override"
    assert not p1.mismatched_keys
    p2, _, n2 = by_tumor["T2-T"]
    assert n2.cmo_sample_name == "N2-N"
    assert p2.trigger_type == "override"
    # The hard-key mismatch is recorded on the pairing (not hidden).
    assert any("patient__cmo" in k for k in (p2.mismatched_keys or []))
    # The MISMATCH row's detail carries the same key(s).
    assert "patient__cmo" in res["rows"][1]["detail"]

    # The FAIL row's detail says why (the resolve error).
    assert "GHOST-N" in res["rows"][2]["detail"]
    assert res["rows"][2]["pairing_id"] is None
    assert res["rows"][0]["pairing_id"] == str(p1.id)

    # Decision 3: one pair_assignment.imported event per file, actor =
    # the caller, with the summary counts + per-row ids/names only.
    imported = _audit_rows(db, "pair_assignment.imported")
    assert len(imported) == 1
    event = imported[0]
    assert event.actor == OPS["email"]
    assert event.entity_type == "pairing_import"
    assert event.pairing_logic == "TEMPO"
    assert event.payload["total"] == 3
    assert event.payload["pinned"] == 1
    assert event.payload["mismatched"] == 1
    assert event.payload["failed"] == 1
    assert [r["status"] for r in event.payload["rows"]] == ["OK", "MISMATCH", "FAIL"]
    assert event.payload["rows"][0]["pairing_id"] == str(p1.id)
    # Each created pairing also carries its own pairing.created event
    # with the caller as actor.
    created = _audit_rows(db, "pairing.created")
    assert len(created) == 2
    assert {e.actor for e in created} == {OPS["email"]}
    assert {e.payload.get("trigger_type") for e in created} == {"override"}

    # The downloadable pairing log: the CLI --log file content — the
    # clean OK row is excluded, the others carry their detail.
    lines = res["log_tsv"].split("\n")
    assert lines[0].split("\t") == list(LOG_HEADER)
    assert lines[1].split("\t")[0] == "MISMATCH"
    assert lines[1].split("\t")[1:3] == ["N2-N", "T2-T"]
    assert lines[2].split("\t")[0] == "FAIL"
    assert lines[2].split("\t")[1:3] == ["GHOST-N", "T3-T"]
    assert res["log_tsv"].endswith("\n")
    assert len(lines) == 4  # header + 2 non-clean rows + trailing


def test_import_endpoint_multi_candidate(db, tempo, admin):
    # A tumor with two matching candidate normals: the curated pair is
    # still pinned, and the multiplicity surfaces as MULTI (CLI parity).
    _sample(db, "P-T3", "T3-T", TUMOR, "C-4")
    _sample(db, "P-N3A", "N3A-N", NORMAL, "C-4")
    _sample(db, "P-N3B", "N3B-N", NORMAL, "C-4")
    text = "NORMAL_ID\tTUMOR_ID\nN3B-N\tT3-T\n"

    res = _import(db, text)

    assert res["rows"][0]["status"] == "MULTI"
    assert res["multi"] == 1
    assert res["pinned"] == 1
    assert "2 matching candidate normals" in res["rows"][0]["detail"]
    assert res["rows"][0]["pairing_id"] is not None
    # The log carries the MULTI row.
    assert res["log_tsv"].split("\n")[1].split("\t")[0] == "MULTI"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_import_endpoint_dry_run_creates_nothing(db, tempo, admin):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _sample(db, "P-T2", "T2-T", TUMOR, "C-2")
    _sample(db, "P-N2", "N2-N", NORMAL, "C-3")
    text = "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\nN2-N\tT2-T\n"

    res = _import(db, text, dry_run=True)

    assert res["dry_run"] is True
    # Dry run resolves and validates only — no triggering, so both rows
    # are OK (the mismatch is only visible after a trigger).
    assert [r["status"] for r in res["rows"]] == ["OK", "OK"]
    assert db.scalars(select(PairingTable)).all() == []
    # The import is still audited, flagged as a dry run.
    imported = _audit_rows(db, "pair_assignment.imported")
    assert len(imported) == 1
    assert imported[0].payload["dry_run"] is True
    assert imported[0].actor == OPS["email"]
    assert _audit_rows(db, "pairing.created") == []


# ---------------------------------------------------------------------------
# Access gate (decision 8)
# ---------------------------------------------------------------------------


def _allowed_user(db, email, role="user", groups=()):
    row = AllowedUser(email=email, role=role, logic_groups=list(groups))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def admin(db):
    """OPS viewer as admin — the access gate is covered separately
    below; the import-behavior tests assume access is granted."""
    return _allowed_user(db, OPS["email"], role="admin", groups=[])


def test_import_endpoint_requires_admin_or_logic_group(db, tempo):
    text = "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\n"
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")

    def _set(role, groups):
        row = db.scalar(select(AllowedUser).where(AllowedUser.email == OPS["email"]))
        if row is None:
            row = AllowedUser(email=OPS["email"])
            db.add(row)
        row.role = role
        row.logic_groups = list(groups)
        db.commit()
        db.refresh(row)
        return row

    # Unknown email -> least privilege (user, no groups) -> 403.
    with pytest.raises(HTTPException) as exc:
        _import(db, text)
    assert exc.value.status_code == 403

    # A user with no logic groups is still locked out.
    _set("user", [])
    with pytest.raises(HTTPException) as exc:
        _import(db, text)
    assert exc.value.status_code == 403

    # A user in another group is still locked out for this logic.
    _set("user", ["OTHER"])
    with pytest.raises(HTTPException) as exc:
        _import(db, text)
    assert exc.value.status_code == 403

    # A user in the import logic's group can import under it.
    _set("user", ["TEMPO", "OTHER"])
    res = _import(db, text)
    assert res["rows"][0]["status"] == "OK"


def test_import_endpoint_admin_can_import(db, tempo):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _allowed_user(db, OPS["email"], role="admin", groups=[])
    res = _import(db, "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\n")
    assert res["rows"][0]["status"] == "OK"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_import_endpoint_rejects_bad_input(db, tempo, admin):
    with pytest.raises(HTTPException) as exc:
        _import(db, "A\tB\nx\ty\n")
    assert exc.value.status_code == 400
    assert "expected header" in str(exc.value.detail)

    # Header only: no data rows.
    with pytest.raises(HTTPException) as exc:
        _import(db, "NORMAL_ID\tTUMOR_ID\n")
    assert exc.value.status_code == 400
    assert "no data rows" in str(exc.value.detail)

    # Empty payload.
    with pytest.raises(HTTPException) as exc:
        _import(db, "")
    assert exc.value.status_code == 400

    # Malformed row width.
    with pytest.raises(HTTPException) as exc:
        _import(db, "NORMAL_ID\tTUMOR_ID\nonly-one-cell\n")
    assert exc.value.status_code == 400

    # Unknown pairing logic.
    with pytest.raises(HTTPException) as exc:
        _import(db, "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\n", logic="NOPE")
    assert exc.value.status_code == 400
    assert "not found" in str(exc.value.detail)


def test_import_endpoint_extended_header_resolves_by_primary_id(db, tempo, admin):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    # The display labels can be anything; the primary_id columns win.
    text = (
        "NORMAL_ID\tTUMOR_ID\tNORMAL_PRIMARY_ID\tTUMOR_PRIMARY_ID\n"
        "whatever\twhatever-else\tP-N1\tP-T1\n"
    )
    res = _import(db, text)
    assert res["rows"][0]["status"] == "OK"
    assert res["rows"][0]["pairing_id"] is not None


# ---------------------------------------------------------------------------
# Route wiring (real HTTP through the FastAPI app)
# ---------------------------------------------------------------------------


def test_import_route_requires_auth(db, tempo):
    # No token -> the router-level auth dependency 401s before the
    # endpoint runs. (No get_db override needed: the dependency never
    # reaches the DB when no token is presented.)
    client = TestClient(app)
    resp = client.post(
        "/v1/pairing/import",
        json={"text": "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\n"},
    )
    assert resp.status_code == 401


def test_import_route_end_to_end(db, tempo):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _allowed_user(db, OPS["email"], role="admin", groups=[])

    client = TestClient(app)

    def _override_get_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: OPS
    app.dependency_overrides[get_db] = _override_get_db
    try:
        resp = client.post(
            "/v1/pairing/import",
            json={
                "text": "NORMAL_ID\tTUMOR_ID\nN1-N\tT1-T\n",
                "logic": "TEMPO",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"][0]["status"] == "OK"
    assert body["pinned"] == 1
    assert body["log_tsv"].split("\n")[0].split("\t") == list(LOG_HEADER)
    # A pairing really exists in the (shared) test DB.
    assert len(db.scalars(select(PairingTable)).all()) == 1
    # ...and the file-level audit event is committed.
    assert len(_audit_rows(db, "pair_assignment.imported")) == 1
