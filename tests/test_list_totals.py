"""Tests for the X-Total-Count headers on the Samples / Patients /
Requests listings (GET /v1/sample/, /patient/, /request/).

Direct-call convention (repo standard): endpoint functions are called
with an in-memory SQLite session and limit/offset passed explicitly
(Query() defaults are unsafe in direct calls); a plain ``Response()``
is passed as the ``response`` kwarg to capture the header. HTTP-level
TestClient tests (bare client, auth + get_db overridden — same pattern
as tests/test_harmony_endpoint.py) confirm the header actually reaches
the wire and the JSON body stays a plain array.
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
from routers.samples import list_samples
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
    created_date=None,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_name",
        patient__cmo=patient__cmo,
        request_id=request_id,
        sample_type="tissue",
        gene_panel=gene_panel,
        bait_set="agilent",
        tumor_or_normal=tumor_or_normal.value,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        created_date=created_date or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _total(response: Response):
    return response.headers.get(TOTAL_HEADER)


# ---------- samples ----------

def test_samples_total_is_pre_pagination_and_page_independent(db):
    for i in range(5):
        _sample(db, f"T{i}", patient__cmo=f"C-{i}")

    r = Response()
    rows = list_samples(limit=2, offset=0, db=db, response=r)
    assert len(rows) == 2 and _total(r) == "5"

    r = Response()
    rows = list_samples(limit=2, offset=2, db=db, response=r)
    assert len(rows) == 2 and _total(r) == "5"

    r = Response()
    rows = list_samples(limit=2, offset=4, db=db, response=r)
    assert len(rows) == 1 and _total(r) == "5"

    # No pagination: same total.
    r = Response()
    rows = list_samples(limit=None, offset=None, db=db, response=r)
    assert len(rows) == 5 and _total(r) == "5"

    # Offset past the end: empty page, unchanged total.
    r = Response()
    rows = list_samples(limit=2, offset=99, db=db, response=r)
    assert rows == [] and _total(r) == "5"


def test_samples_total_shrinks_with_filters(db):
    _sample(db, "TUMOR-1", tumor_or_normal=TumorOrNormal.TUMOR, gene_panel="PANEL-A")
    _sample(db, "TUMOR-2", tumor_or_normal=TumorOrNormal.TUMOR, gene_panel="PANEL-B")
    _sample(db, "NORMAL-1", tumor_or_normal=TumorOrNormal.NORMAL, gene_panel="PANEL-A")

    r = Response()
    list_samples(limit=None, offset=None, db=db, response=r)
    assert _total(r) == "3"

    r = Response()
    list_samples(
        tumor_or_normal=TumorOrNormal.TUMOR, limit=None, offset=None, db=db, response=r
    )
    assert _total(r) == "2"

    r = Response()
    # Case-insensitive substring filter also shrinks the total.
    list_samples(gene_panel="panel-a", limit=None, offset=None, db=db, response=r)
    assert _total(r) == "2"

    r = Response()
    list_samples(q="normal", limit=None, offset=None, db=db, response=r)
    assert _total(r) == "1"

    r = Response()
    rows = list_samples(q="does-not-exist", limit=2, offset=0, db=db, response=r)
    assert rows == [] and _total(r) == "0"


def test_samples_total_no_patient_filter(db):
    _sample(db, "PATED", patient__cmo="C-1")
    _sample(db, "POOLED-1", patient__cmo=None)
    _sample(db, "POOLED-2", patient__cmo=None)

    r = Response()
    list_samples(limit=None, offset=None, db=db, response=r)
    assert _total(r) == "3"

    r = Response()
    list_samples(no_patient=True, limit=None, offset=None, db=db, response=r)
    assert _total(r) == "2"

    r = Response()
    list_samples(no_patient=False, limit=None, offset=None, db=db, response=r)
    assert _total(r) == "3"


# ---------- patients ----------

def test_patients_total_counts_distinct_patients_across_pages(db):
    _sample(db, "S1", patient__cmo="P-A")
    _sample(db, "S2", patient__cmo="P-A")
    _sample(db, "S3", patient__cmo="P-B")
    _sample(db, "S4", patient__cmo="P-C")
    _sample(db, "S5", patient__cmo=None)  # pooled normal: no patient group

    r = Response()
    rows = list_patients(limit=2, offset=0, db=db, response=r)
    assert len(rows) == 2 and _total(r) == "3"

    r = Response()
    rows = list_patients(limit=2, offset=2, db=db, response=r)
    assert len(rows) == 1 and _total(r) == "3"

    r = Response()
    rows = list_patients(limit=None, offset=None, db=db, response=r)
    assert len(rows) == 3 and _total(r) == "3"


def test_patients_total_shrinks_with_q_filter(db):
    _sample(db, "S1", patient__cmo="C-EXAMPLE1")
    _sample(db, "S2", patient__cmo="C-EXAMPLE2")
    _sample(db, "S3", patient__cmo="OTHER-9")

    r = Response()
    list_patients(q="c-example", limit=None, offset=None, db=db, response=r)
    assert _total(r) == "2"

    r = Response()
    list_patients(q="EXAMPLE1", limit=None, offset=None, db=db, response=r)
    assert _total(r) == "1"

    r = Response()
    rows = list_patients(q="zzz", limit=10, offset=0, db=db, response=r)
    assert rows == [] and _total(r) == "0"


# ---------- requests ----------

def test_requests_total_counts_distinct_requests_across_pages(db):
    _sample(db, "S1", request_id="R-1")
    _sample(db, "S2", request_id="R-1")
    _sample(db, "S3", request_id="R-2")
    _sample(db, "S4", request_id="R-3")
    _sample(db, "S5", request_id=None)  # no request group

    r = Response()
    rows = list_requests(limit=2, offset=0, db=db, response=r)
    assert len(rows) == 2 and _total(r) == "3"

    r = Response()
    rows = list_requests(limit=2, offset=2, db=db, response=r)
    assert len(rows) == 1 and _total(r) == "3"

    r = Response()
    rows = list_requests(limit=None, offset=None, db=db, response=r)
    assert len(rows) == 3 and _total(r) == "3"

    r = Response()
    rows = list_requests(q="r-2", limit=None, offset=None, db=db, response=r)
    assert len(rows) == 1 and _total(r) == "1"

    r = Response()
    rows = list_requests(q="nope", limit=None, offset=None, db=db, response=r)
    assert rows == [] and _total(r) == "0"


# ---------- HTTP surface ----------

def test_http_sample_listing_total_count_header(client, db):
    for i in range(5):
        _sample(db, f"H{i}", patient__cmo=f"C-{i}")

    res = client.get("/v1/sample/", params={"limit": 2, "offset": 0})
    assert res.status_code == 200
    assert isinstance(res.json(), list) and len(res.json()) == 2
    assert res.headers.get(TOTAL_HEADER) == "5"

    # Page 2 (and beyond-the-end) report the same total.
    res = client.get("/v1/sample/", params={"limit": 2, "offset": 2})
    assert len(res.json()) == 2 and res.headers.get(TOTAL_HEADER) == "5"
    res = client.get("/v1/sample/", params={"limit": 2, "offset": 99})
    assert res.json() == [] and res.headers.get(TOTAL_HEADER) == "5"

    # Filters shrink the reported total.
    res = client.get("/v1/sample/", params={"q": "H3"})
    assert res.headers.get(TOTAL_HEADER) == "1"
    res = client.get("/v1/sample/", params={"q": "does-not-exist"})
    assert res.json() == [] and res.headers.get(TOTAL_HEADER) == "0"


def test_http_patient_listing_total_count_header(client, db):
    _sample(db, "S1", patient__cmo="P-A")
    _sample(db, "S2", patient__cmo="P-A")
    _sample(db, "S3", patient__cmo="P-B")

    res = client.get("/v1/patient/", params={"limit": 1, "offset": 0})
    assert res.status_code == 200
    assert isinstance(res.json(), list) and len(res.json()) == 1
    assert res.headers.get(TOTAL_HEADER) == "2"

    res = client.get("/v1/patient/", params={"limit": 1, "offset": 1})
    assert res.headers.get(TOTAL_HEADER) == "2"

    res = client.get("/v1/patient/", params={"q": "P-A"})
    assert res.headers.get(TOTAL_HEADER) == "1"


def test_http_request_listing_total_count_header(client, db):
    _sample(db, "S1", request_id="R-1")
    _sample(db, "S2", request_id="R-2")
    _sample(db, "S3", request_id="R-2")

    res = client.get("/v1/request/", params={"limit": 1, "offset": 0})
    assert res.status_code == 200
    assert isinstance(res.json(), list) and len(res.json()) == 1
    assert res.headers.get(TOTAL_HEADER) == "2"

    res = client.get("/v1/request/", params={"limit": 1, "offset": 1})
    assert res.headers.get(TOTAL_HEADER) == "2"

    res = client.get("/v1/request/", params={"q": "R-2"})
    assert res.headers.get(TOTAL_HEADER) == "1"
