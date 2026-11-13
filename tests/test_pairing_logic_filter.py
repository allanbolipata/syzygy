"""Tests for the ``pairing_logic`` listing filter on GET /v1/sample/,
/v1/patient/ and /v1/request/ (card t_39cb840f — logic-family browsing).

Direct-call convention (repo standard): endpoint functions are called
with an in-memory SQLite session and limit/offset passed explicitly
(Query() defaults are unsafe in direct calls); the new ``pairing_logic``
param keeps its plain ``None`` default and is passed as a plain kwarg.
A plain ``Response()`` captures the X-Total-Count header. HTTP-level
TestClient tests confirm the new query param reaches the wire (filtered
200, unknown logic 400).

Key semantics under test:
- Sample membership: the sample's ``bait_set`` matches a *registered*
  ``bait_sets`` assignment for the logic (case-insensitive SQL join);
  the DMP logic additionally owns ``bait_set``-NULL samples of
  ``sample_type`` ``'DMP'``.
- Samples whose bait set carries no registered assignment belong to no
  family: visible only when the filter is omitted/ALL (flagged wrinkle —
  ``classify_bait_set`` name rules are NOT applied SQL-side).
- Omitted / blank / ``ALL`` (any case) behave exactly as before.
- Patients/requests: a group is included when >=1 of its samples belongs
  to the family, but its counts stay ALL-samples (2026-11-11 counting
  decision, reaffirmed 2026-11-12).
- Hard rule: the listing filter never narrows pairing candidate scope —
  /sample/{id}/possible-matches still lists an unregistered-bait normal
  that the listing filter hides.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app
from syzygy.models import BaitSet, PairingLogic, Sample
from syzygy.routers.patients import list_patients
from syzygy.routers.requests import list_requests
from syzygy.routers.samples import list_samples, possible_matches_endpoint
from syzygy.schemas import TumorOrNormal

TOTAL_HEADER = "X-Total-Count"

BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)


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


def _logic(db, name, *, strategy_name="default", active=True):
    logic = PairingLogic(name=name, strategy_name=strategy_name, active=active)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _assign(db, bait_set, logic):
    row = BaitSet(bait_set=bait_set, pairing_logic_id=logic.id)
    db.add(row)
    db.commit()
    return row


def _sample(
    db,
    primary_id,
    *,
    patient__cmo="C-1",
    request_id=None,
    tumor_or_normal=TumorOrNormal.NORMAL,
    gene_panel="IMPACT468",
    sample_type="tissue",
    bait_set="impact468",
    n=0,
):
    """Seed a sample; ``n`` staggers created_date so 'newest first' order
    is deterministic (higher n = newer)."""
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_name",
        patient__cmo=patient__cmo,
        request_id=request_id,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal.value,
        created_date=BASE_TIME + timedelta(minutes=n),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _total(response: Response):
    return response.headers.get(TOTAL_HEADER)


def _seed_family(db):
    """ARGOS (bait IMPACT468), TEMPO (bait ONCOGRAM), DMP (bait DMP).

    Samples:
    - A1/A2: bait impact468 / IMPACT468 (case variants) -> ARGOS family
    - T1: bait oncogram -> TEMPO family
    - U1: bait mystery-bait -> NOT registered in bait_sets (no family)
    - D1: bait NULL, sample_type 'DMP' -> DMP family via the DMP rule
    - D2: bait 'dmp' (registered -> DMP), sample_type cfDNA -> DMP family
    """
    argos = _logic(db, "ARGOS")
    tempo = _logic(db, "TEMPO")
    dmp = _logic(db, "DMP")
    _assign(db, "IMPACT468", argos)
    _assign(db, "ONCOGRAM", tempo)
    _assign(db, "DMP", dmp)
    a1 = _sample(db, "A1", patient__cmo="P-1", request_id="R-1", bait_set="impact468", n=1)
    a2 = _sample(db, "A2", patient__cmo="P-1", request_id="R-2", bait_set="IMPACT468", n=2)
    t1 = _sample(db, "T1", patient__cmo="P-2", request_id="R-2", bait_set="oncogram", n=3)
    u1 = _sample(db, "U1", patient__cmo="P-2", request_id="R-3", bait_set="mystery-bait", n=4)
    d1 = _sample(db, "D1", patient__cmo="P-3", request_id="R-3", bait_set=None,
                 sample_type="DMP", n=5)
    d2 = _sample(db, "D2", patient__cmo="P-3", request_id="R-1", bait_set="dmp",
                 sample_type="cfDNA", n=6)
    return {"ARGOS": argos, "TEMPO": tempo, "DMP": dmp,
            "A1": a1, "A2": a2, "T1": t1, "U1": u1, "D1": d1, "D2": d2}


# ---------- samples: membership semantics ----------

def test_samples_filter_returns_only_matching_logic(db):
    s = _seed_family(db)
    r = Response()
    rows = list_samples(limit=None, offset=None, pairing_logic="ARGOS", db=db, response=r)
    assert {x.primary_id for x in rows} == {"A1", "A2"}
    assert _total(r) == "2"

    rows = list_samples(limit=None, offset=None, pairing_logic="TEMPO", db=db)
    assert [x.primary_id for x in rows] == ["T1"]

    # DMP family = null-bait_set 'DMP'-type samples + registered DMP bait.
    rows = list_samples(limit=None, offset=None, pairing_logic="DMP", db=db)
    assert {x.primary_id for x in rows} == {"D1", "D2"}


def test_samples_unregistered_bait_only_visible_under_all(db):
    s = _seed_family(db)
    rows = list_samples(limit=None, offset=None, db=db)
    assert "U1" in {x.primary_id for x in rows}
    for name in ("ARGOS", "TEMPO", "DMP"):
        rows = list_samples(limit=None, offset=None, pairing_logic=name, db=db)
        assert "U1" not in {x.primary_id for x in rows}, f"U1 leaked under {name}"


def test_samples_all_and_omitted_behave_as_before(db):
    s = _seed_family(db)
    plain = Response()
    rows_plain = list_samples(limit=None, offset=None, db=db, response=plain)
    assert _total(plain) == "6"
    for value in ("ALL", "all", "All", "", "   "):
        r = Response()
        rows = list_samples(limit=None, offset=None, pairing_logic=value, db=db, response=r)
        assert [x.primary_id for x in rows] == [x.primary_id for x in rows_plain]
        assert _total(r) == _total(plain)


def test_samples_filter_pagination_total(db):
    _seed_family(db)
    r = Response()
    rows = list_samples(limit=1, offset=0, pairing_logic="ARGOS", db=db, response=r)
    assert len(rows) == 1
    # Newest-first page slice, but the total covers the whole filter set.
    assert rows[0].primary_id == "A2"
    assert _total(r) == "2"


def test_samples_filter_combines_with_other_filters(db):
    _seed_family(db)
    r = Response()
    rows = list_samples(limit=None, offset=None, pairing_logic="ARGOS",
                        patient__cmo="P-1", db=db, response=r)
    assert {x.primary_id for x in rows} == {"A1", "A2"}
    assert _total(r) == "2"
    rows = list_samples(limit=None, offset=None, pairing_logic="ARGOS",
                        patient__cmo="P-2", db=db)
    assert rows == []


def test_samples_case_insensitive_logic_name(db):
    _seed_family(db)
    rows = list_samples(limit=None, offset=None, pairing_logic="argos", db=db)
    assert {x.primary_id for x in rows} == {"A1", "A2"}


def test_samples_inactive_logic_still_filters(db):
    # Browse filters are read-only: an inactive logic may scope a listing
    # (unlike resolve_pairing_logic, which 400s on inactive logic).
    argos = _logic(db, "ARGOS", active=False)
    _assign(db, "IMPACT468", argos)
    _sample(db, "A1", bait_set="impact468")
    _sample(db, "U1", bait_set="mystery-bait")
    rows = list_samples(limit=None, offset=None, pairing_logic="ARGOS", db=db)
    assert [x.primary_id for x in rows] == ["A1"]


def test_samples_unknown_logic_400(db):
    _seed_family(db)
    with pytest.raises(HTTPException) as exc_info:
        list_samples(limit=None, offset=None, pairing_logic="NOPE", db=db)
    assert exc_info.value.status_code == 400


# ---------- patients: group membership, all-samples counts ----------

def test_patients_logic_filter_membership_counts_stay_all_samples(db):
    s = _seed_family(db)
    r = Response()
    rows = list_patients(limit=None, offset=None, pairing_logic="ARGOS", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-1"]
    assert _total(r) == "1"
    # P-1's counts cover ALL its samples even though only its bait match
    # selected it (P-1 has exactly its 2 ARGOS samples here).
    assert rows[0].sample_count == 2 and rows[0].tumor_count == 0

    # P-2 = TEMPO sample + unregistered sample: included under TEMPO,
    # counts cover BOTH samples (unregistered one included).
    rows = list_patients(limit=None, offset=None, pairing_logic="TEMPO", db=db)
    assert [p.patient__cmo for p in rows] == ["P-2"]
    assert rows[0].sample_count == 2

    # DMP: P-3 included; its requests list is also all-samples.
    rows = list_patients(limit=None, offset=None, pairing_logic="DMP", db=db)
    assert [p.patient__cmo for p in rows] == ["P-3"]
    assert rows[0].sample_count == 2
    assert set(rows[0].requests) == {"R-1", "R-3"}


def test_patients_all_behaves_as_before(db):
    _seed_family(db)
    plain = Response()
    rows_plain = list_patients(limit=None, offset=None, db=db, response=plain)
    assert _total(plain) == "3"
    r = Response()
    rows = list_patients(limit=None, offset=None, pairing_logic="ALL", db=db, response=r)
    assert [p.patient__cmo for p in rows] == [p.patient__cmo for p in rows_plain]
    assert _total(r) == _total(plain)


def test_patients_unknown_logic_400(db):
    _seed_family(db)
    with pytest.raises(HTTPException) as exc_info:
        list_patients(limit=None, offset=None, pairing_logic="NOPE", db=db)
    assert exc_info.value.status_code == 400


# ---------- requests: group membership, all-samples counts ----------

def test_requests_logic_filter_membership_counts_stay_all_samples(db):
    _seed_family(db)
    # R-1: A1 (ARGOS) + D2 (DMP) -> appears under ARGOS and DMP.
    # R-2: A2 (ARGOS) + T1 (TEMPO). R-3: U1 (none) + D1 (DMP).
    r = Response()
    rows = list_requests(limit=None, offset=None, pairing_logic="ARGOS", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1", "R-2"]
    assert _total(r) == "2"
    by_id = {x.request_id: x for x in rows}
    # Counts cover ALL samples of the group, not only the family's.
    assert by_id["R-1"].sample_count == 2
    assert by_id["R-2"].sample_count == 2

    rows = list_requests(limit=None, offset=None, pairing_logic="DMP", db=db)
    assert [x.request_id for x in rows] == ["R-1", "R-3"]

    rows = list_requests(limit=None, offset=None, pairing_logic="TEMPO", db=db)
    assert [x.request_id for x in rows] == ["R-2"]


def test_requests_all_behaves_as_before(db):
    _seed_family(db)
    plain = Response()
    rows_plain = list_requests(limit=None, offset=None, db=db, response=plain)
    assert _total(plain) == "3"
    r = Response()
    rows = list_requests(limit=None, offset=None, pairing_logic="ALL", db=db, response=r)
    assert [x.request_id for x in rows] == [x.request_id for x in rows_plain]
    assert _total(r) == _total(plain)


def test_requests_unknown_logic_400(db):
    _seed_family(db)
    with pytest.raises(HTTPException) as exc_info:
        list_requests(limit=None, offset=None, pairing_logic="NOPE", db=db)
    assert exc_info.value.status_code == 400


# ---------- hard rule: listing filters never narrow pairing candidates ----------

def test_listing_filter_does_not_narrow_possible_matches(db):
    # ARGOS logic on the argos strategy: browse-filtering the Samples tab
    # to ARGOS must not hide the same-patient DMP normal from the
    # possible-matches candidate view (argos lists same-patient DMP
    # samples as overridable candidates by patient only).
    argos = _logic(db, "ARGOS", strategy_name="argos")
    _logic(db, "DMP")
    _assign(db, "IMPACT468", argos)
    tumor = _sample(db, "T1", patient__cmo="P-1", request_id="R-1",
                    tumor_or_normal=TumorOrNormal.TUMOR, bait_set="impact468", n=1)
    _sample(db, "N1", patient__cmo="P-1", request_id="R-1", bait_set="impact468", n=2)
    dmp_normal = _sample(db, "D1", patient__cmo="P-1", request_id=None,
                         bait_set=None, sample_type="DMP", n=3)

    # The ARGOS browse filter hides the DMP normal from the samples list
    # (it belongs to the DMP family only)...
    listed = list_samples(limit=None, offset=None, pairing_logic="ARGOS", db=db)
    assert "D1" not in {x.primary_id for x in listed}
    assert {x.primary_id for x in listed} == {"T1", "N1"}
    # ...it is visible under its own family, as expected.
    listed_dmp = list_samples(limit=None, offset=None, pairing_logic="DMP", db=db)
    assert {x.primary_id for x in listed_dmp} == {"D1"}

    # ...but possible-matches still lists it as an other candidate —
    # candidate scope is untouched by listing filters.
    res = possible_matches_endpoint(tumor.id, limit=100, offset=0, db=db)
    assert res["pairing_logic"] == "ARGOS"
    candidate_ids = {c["sample"].primary_id for c in res["other_matches"]}
    assert "D1" in candidate_ids
    assert res["other_matches_total"] >= 2


# ---------- HTTP level ----------

def test_client_endpoints_wire_param(client, db):
    _seed_family(db)
    res = client.get("/v1/sample/", params={"limit": 50, "offset": 0, "pairing_logic": "ARGOS"})
    assert res.status_code == 200
    assert {x["primary_id"] for x in res.json()} == {"A1", "A2"}
    assert res.headers.get(TOTAL_HEADER) == "2"

    res = client.get("/v1/sample/", params={"pairing_logic": "ALL"})
    assert res.status_code == 200 and len(res.json()) == 6

    res = client.get("/v1/sample/", params={"pairing_logic": "NOPE"})
    assert res.status_code == 400

    res = client.get("/v1/patient/", params={"pairing_logic": "ARGOS"})
    assert res.status_code == 200
    assert [p["patient__cmo"] for p in res.json()] == ["P-1"]

    res = client.get("/v1/request/", params={"pairing_logic": "DMP"})
    assert res.status_code == 200
    assert [x["request_id"] for x in res.json()] == ["R-1", "R-3"]

    res = client.get("/v1/patient/", params={"pairing_logic": "NOPE"})
    assert res.status_code == 400
