"""Tests for the advanced filter params on the Patients / Requests listings
(GET /v1/patient/, GET /v1/request/) — the backend half of the advanced
filter panels.

Direct-call convention (repo standard): endpoint functions are called with
an in-memory SQLite session and limit/offset passed explicitly (Query()
defaults are unsafe in direct calls); the new advanced params use their
plain ``None`` defaults and are passed as plain kwargs. A plain
``Response()`` captures the X-Total-Count header. HTTP-level TestClient
tests confirm the new query params reach the wire.

Key semantics under test (Allan's 2026-11-11 decision, no logic scoping):
advanced filters select which *groups* appear; every returned row's counts
still cover ALL of the group's samples, and X-Total-Count always equals
the number of filtered rows.
"""
from datetime import datetime, timezone

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import get_current_user
from database import Base, get_db
from main import app
from models import Sample
from routers.patients import list_patients
from routers.requests import list_requests
from schemas import TumorOrNormal

TOTAL_HEADER = "X-Total-Count"


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


def _sample(
    db,
    primary_id,
    *,
    patient__cmo="C-1",
    request_id=None,
    tumor_or_normal=TumorOrNormal.NORMAL,
    gene_panel="IMPACT468",
    sample_type="tissue",
    bait_set="agilent",
    additional_metadata=None,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_name",
        patient__cmo=patient__cmo,
        request_id=request_id,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal.value,
        additional_metadata=additional_metadata,
        created_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _total(response: Response):
    return response.headers.get(TOTAL_HEADER)


# ---------- patients: metadata filters ----------

def _seed_metadata_patients(db):
    # P-HUMAN: tumor species Human + normal species Mouse — the derived
    # value is Human (tumor priority). 2 samples total.
    _sample(db, "T-H", patient__cmo="P-HUMAN", tumor_or_normal=TumorOrNormal.TUMOR,
            additional_metadata={"species": "Human"})
    _sample(db, "N-H", patient__cmo="P-HUMAN",
            additional_metadata={"species": "Mouse"})
    # P-MOUSE: normal-only Mouse — no tumor carries the key.
    _sample(db, "N-M", patient__cmo="P-MOUSE",
            additional_metadata={"species": "Mouse"})
    # P-NONE: no metadata at all.
    _sample(db, "N-0", patient__cmo="P-NONE", additional_metadata=None)


def test_patients_species_filter_tumor_priority(db):
    _seed_metadata_patients(db)

    # Case-insensitive partial match on the derived value.
    r = Response()
    rows = list_patients(limit=None, offset=None, species="human", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-HUMAN"]
    assert _total(r) == "1"
    # Counts cover ALL samples of the group, not only the matching ones.
    assert rows[0].sample_count == 2 and rows[0].tumor_count == 1

    # MOUSE must NOT match P-HUMAN: its derived value is Human because a
    # tumor carries the key (tumor priority, same derivation as PatientRead).
    r = Response()
    rows = list_patients(limit=None, offset=None, species="MOUSE", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-MOUSE"]
    assert _total(r) == "1"

    # Partial match inside the value.
    r = Response()
    rows = list_patients(limit=None, offset=None, species="hum", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-HUMAN"]

    # No match: total 0, empty page.
    r = Response()
    rows = list_patients(limit=10, offset=0, species="fish", db=db, response=r)
    assert rows == [] and _total(r) == "0"

    # Plain defaults (no advanced params) keep the old behavior:
    # 4 samples across 3 patient groups.
    r = Response()
    rows = list_patients(limit=None, offset=None, db=db, response=r)
    assert len(rows) == 3 and _total(r) == "3"


def test_patients_sex_oncotree_diagnosis_filters(db):
    _sample(db, "T-A", patient__cmo="P-A", tumor_or_normal=TumorOrNormal.TUMOR,
            additional_metadata={"sex": "F", "oncotreeCode": "DLBCL",
                                 "diagnosisDate": "2024-05-17"})
    _sample(db, "T-B", patient__cmo="P-B", tumor_or_normal=TumorOrNormal.TUMOR,
            additional_metadata={"sex": "M", "oncotreeCode": "GLI",
                                 "diagnosisDate": "2023-11-02"})

    r = Response()
    rows = list_patients(limit=None, offset=None, sex="f", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-A"] and _total(r) == "1"

    r = Response()
    rows = list_patients(limit=None, offset=None, oncotree_code="gli", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-B"] and _total(r) == "1"

    # Partial match on the date string.
    r = Response()
    rows = list_patients(limit=None, offset=None, diagnosis_date="2024-05", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-A"] and _total(r) == "1"

    r = Response()
    rows = list_patients(limit=None, offset=None, diagnosis_date="17", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-A"] and _total(r) == "1"

    # AND-combine: mutually exclusive filters yield nothing, total 0.
    r = Response()
    rows = list_patients(limit=None, offset=None, sex="f", oncotree_code="gli",
                         db=db, response=r)
    assert rows == [] and _total(r) == "0"

    # AND-combine: compatible filters match exactly one patient.
    r = Response()
    rows = list_patients(limit=None, offset=None, sex="m", oncotree_code="GLI",
                         diagnosis_date="2023", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-B"] and _total(r) == "1"


def test_patients_request_id_partial_match(db):
    # P-1: samples across two requests plus one request-less sample.
    _sample(db, "S1", patient__cmo="P-1", request_id="R-2024-001")
    _sample(db, "S2", patient__cmo="P-1", request_id="R-2024-002")
    _sample(db, "S3", patient__cmo="P-1", request_id=None)
    # P-2: only in a different request.
    _sample(db, "S4", patient__cmo="P-2", request_id="R-2025-100")

    r = Response()
    rows = list_patients(limit=None, offset=None, request_id="2024-00", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-1"]
    # >=1 sample in the request matches, but the count covers ALL samples.
    assert rows[0].sample_count == 3

    # One matching sample is enough.
    r = Response()
    rows = list_patients(limit=None, offset=None, request_id="2025", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-2"] and _total(r) == "1"

    r = Response()
    rows = list_patients(limit=None, offset=None, request_id="R-2024-002", db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["P-1"]

    r = Response()
    rows = list_patients(limit=None, offset=None, request_id="9999", db=db, response=r)
    assert rows == [] and _total(r) == "0"


def test_patients_advanced_filters_combine_with_q_and_totals_match_rows(db):
    _sample(db, "T-A", patient__cmo="C-EXAMPLE1", tumor_or_normal=TumorOrNormal.TUMOR,
            request_id="R-2024-001", additional_metadata={"species": "Human", "sex": "F"})
    _sample(db, "N-A", patient__cmo="C-EXAMPLE1")
    _sample(db, "T-B", patient__cmo="C-EXAMPLE2", tumor_or_normal=TumorOrNormal.TUMOR,
            request_id="R-2024-002", additional_metadata={"species": "Mouse", "sex": "M"})
    _sample(db, "T-C", patient__cmo="OTHER-9", tumor_or_normal=TumorOrNormal.TUMOR,
            additional_metadata={"species": "Human", "sex": "F"})

    # q + metadata + request_id AND-combined.
    r = Response()
    rows = list_patients(q="example", species="human", request_id="2024",
                         limit=None, offset=None, db=db, response=r)
    assert [p.patient__cmo for p in rows] == ["C-EXAMPLE1"] and _total(r) == "1"

    # Add a contradicting filter: the AND set empties.
    r = Response()
    rows = list_patients(q="example", species="mouse", sex="f",
                         limit=None, offset=None, db=db, response=r)
    assert rows == [] and _total(r) == "0"

    # Bigger AND set where pagination matters: total == filtered row count
    # across pages (species=human OR-combined? no — AND: use sex=f which
    # matches EXAMPLE1 and OTHER-9 => 2 groups, page size 1).
    r = Response()
    page1 = list_patients(sex="f", limit=1, offset=0, db=db, response=r)
    assert len(page1) == 1 and _total(r) == "2"
    r = Response()
    page2 = list_patients(sex="f", limit=1, offset=1, db=db, response=r)
    assert len(page2) == 1 and _total(r) == "2"
    r = Response()
    page3 = list_patients(sex="f", limit=1, offset=2, db=db, response=r)
    assert page3 == [] and _total(r) == "2"
    # Unpaged call under the same filter: rows == total.
    r = Response()
    all_rows = list_patients(sex="f", limit=None, offset=None, db=db, response=r)
    assert len(all_rows) == int(_total(r)) == 2


# ---------- requests: advanced filters ----------

def _seed_mixed_requests(db):
    # R-1: mixed panels/types/bait sets/patients (4 samples).
    _sample(db, "S1", request_id="R-1", patient__cmo="C-ALPHA",
            gene_panel="IMPACT468", sample_type="tissue", bait_set="agilent",
            tumor_or_normal=TumorOrNormal.TUMOR)
    _sample(db, "S2", request_id="R-1", patient__cmo="C-BETA",
            gene_panel="WEB", sample_type="ddPCR", bait_set="other",
            tumor_or_normal=TumorOrNormal.NORMAL)
    _sample(db, "S3", request_id="R-1", patient__cmo=None,
            gene_panel="IMPACT468", sample_type="tissue", bait_set="agilent")
    _sample(db, "S4", request_id="R-1", patient__cmo="C-BETA",
            gene_panel="WEB", sample_type="cultured_cells", bait_set="agilent")
    # R-2: single kind.
    _sample(db, "S5", request_id="R-2", patient__cmo="C-GAMMA",
            gene_panel="IMPACT468", sample_type="tissue", bait_set="twist")


def test_requests_gene_panel_sample_type_bait_set_filters(db):
    _seed_mixed_requests(db)

    r = Response()
    rows = list_requests(limit=None, offset=None, gene_panel="impact", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1", "R-2"]
    # >=1 matching sample selects the group; counts cover ALL its samples.
    assert rows[0].sample_count == 4

    r = Response()
    rows = list_requests(limit=None, offset=None, gene_panel="web", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1"] and _total(r) == "1"

    r = Response()
    rows = list_requests(limit=None, offset=None, sample_type="ddpcr", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1"]

    r = Response()
    rows = list_requests(limit=None, offset=None, bait_set="twist", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-2"]

    # AND-combine over distinct sample attributes of the SAME request: R-1
    # has WEB samples and ddPCR samples (different samples) — both match.
    r = Response()
    rows = list_requests(limit=None, offset=None, gene_panel="web", sample_type="ddpcr",
                         db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1"] and rows[0].sample_count == 4

    # AND-combining with a non-matching field empties the set.
    r = Response()
    rows = list_requests(limit=None, offset=None, bait_set="twist", gene_panel="web",
                         db=db, response=r)
    assert rows == [] and _total(r) == "0"


def test_requests_patient_cmo_filter(db):
    _seed_mixed_requests(db)

    r = Response()
    rows = list_requests(limit=None, offset=None, patient__cmo="alpha", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1"]
    assert rows[0].sample_count == 4  # all samples, incl. non-matching ones

    r = Response()
    rows = list_requests(limit=None, offset=None, patient__cmo="c-", db=db, response=r)
    assert [x.request_id for x in rows] == ["R-1", "R-2"]

    r = Response()
    rows = list_requests(limit=None, offset=None, patient__cmo="delta", db=db, response=r)
    assert rows == [] and _total(r) == "0"


def test_requests_advanced_filters_combine_with_q_and_totals_match_rows(db):
    _seed_mixed_requests(db)

    # q + advanced AND-combined.
    r = Response()
    rows = list_requests(q="r-2", bait_set="twist", limit=None, offset=None,
                         db=db, response=r)
    assert [x.request_id for x in rows] == ["R-2"] and _total(r) == "1"

    r = Response()
    rows = list_requests(q="r-2", bait_set="agilent", limit=None, offset=None,
                         db=db, response=r)
    assert rows == [] and _total(r) == "0"

    # Filtered pagination: total stays the filtered group count across pages.
    r = Response()
    page1 = list_requests(gene_panel="impact", limit=1, offset=0, db=db, response=r)
    assert len(page1) == 1 and _total(r) == "2"
    r = Response()
    page2 = list_requests(gene_panel="impact", limit=1, offset=1, db=db, response=r)
    assert len(page2) == 1 and _total(r) == "2"
    r = Response()
    rows = list_requests(gene_panel="impact", limit=None, offset=None, db=db, response=r)
    assert len(rows) == int(_total(r)) == 2

    # Plain defaults keep the old behavior.
    r = Response()
    rows = list_requests(limit=None, offset=None, db=db, response=r)
    assert len(rows) == 2 and _total(r) == "2"


# ---------- HTTP surface: new params reach the wire ----------

def test_http_patients_advanced_params(client, db):
    _seed_metadata_patients(db)

    res = client.get("/v1/patient/", params={"limit": 1, "offset": 0, "species": "human"})
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["patient__cmo"] == "P-HUMAN"
    assert body[0]["sample_count"] == 2
    assert res.headers.get(TOTAL_HEADER) == "1"

    res = client.get("/v1/patient/", params={"species": "fish"})
    assert res.json() == [] and res.headers.get(TOTAL_HEADER) == "0"

    res = client.get("/v1/patient/", params={"request_id": "nope"})
    assert res.json() == [] and res.headers.get(TOTAL_HEADER) == "0"


def test_http_requests_advanced_params(client, db):
    _seed_mixed_requests(db)

    res = client.get("/v1/request/", params={"limit": 1, "offset": 0, "gene_panel": "web"})
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["request_id"] == "R-1"
    assert body[0]["sample_count"] == 4  # all samples, not just matching ones
    assert res.headers.get(TOTAL_HEADER) == "1"

    res = client.get(
        "/v1/request/",
        params={"sample_type": "tissue", "patient__cmo": "gamma"},
    )
    assert [x["request_id"] for x in res.json()] == ["R-2"]
    assert res.headers.get(TOTAL_HEADER) == "1"
