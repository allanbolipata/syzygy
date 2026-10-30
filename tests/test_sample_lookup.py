"""Direct-call tests for GET /v1/sample/lookup (repo convention: endpoint
functions are called directly with an in-memory SQLite session, no
TestClient; new optional params have plain defaults so direct calls
pass them explicitly)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import Sample
from routers.samples import lookup_samples_endpoint
from schemas import TumorOrNormal


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


def _sample(db, primary_id, cmo=None, created_date=None,
            patient__cmo="C-1"):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo or f"{primary_id}_name",
        patient__cmo=patient__cmo,
        sample_type="tissue",
        gene_panel="IMPACT468",
        bait_set="agilent",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        created_date=created_date
        or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_lookup_by_primary_id(db):
    _sample(db, "10075_D_1")
    res = lookup_samples_endpoint(q="10075", db=db)
    assert [s.primary_id for s in res] == ["10075_D_1"]


def test_lookup_by_cmo_sample_name(db):
    _sample(db, "T1", cmo="cmo_lookup_target_1")
    res = lookup_samples_endpoint(q="cmo_lookup_target", db=db)
    assert [s.primary_id for s in res] == ["T1"]


def test_lookup_is_case_insensitive(db):
    _sample(db, "10075_D_1", cmo="Cmo_Mixed_Case_Name")
    assert lookup_samples_endpoint(q="10075_d_1", db=db)
    assert lookup_samples_endpoint(q="CMO_mixed", db=db)
    assert lookup_samples_endpoint(q="cmo_MIXED_case", db=db)


def test_lookup_matches_both_columns(db):
    _sample(db, "SHARED_id", cmo="alpha_name")
    _sample(db, "beta", cmo="SHARED_id_name")
    res = lookup_samples_endpoint(q="SHARED_id", db=db)
    assert {s.primary_id for s in res} == {"SHARED_id", "beta"}


def test_lookup_no_match(db):
    _sample(db, "T1")
    assert lookup_samples_endpoint(q="does-not-exist", db=db) == []


def test_lookup_ordering_newest_first(db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        _sample(db, f"T{i}", created_date=base + timedelta(days=i))
    res = lookup_samples_endpoint(q="T", db=db)
    assert [s.primary_id for s in res] == ["T2", "T1", "T0"]


def test_lookup_limit_and_offset(db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        _sample(db, f"T{i}", cmo="same_name",
                created_date=base + timedelta(days=i))
    res = lookup_samples_endpoint(q="same_name", limit=2, offset=1, db=db)
    assert [s.primary_id for s in res] == ["T3", "T2"]


def test_lookup_invalid_limit(db):
    _sample(db, "T1")
    for bad in (0, -1, 10001):
        with pytest.raises(HTTPException) as exc:
            lookup_samples_endpoint(q="T1", limit=bad, db=db)
        assert exc.value.status_code == 400


def test_lookup_invalid_offset(db):
    _sample(db, "T1")
    with pytest.raises(HTTPException) as exc:
        lookup_samples_endpoint(q="T1", offset=-1, db=db)
    assert exc.value.status_code == 400
