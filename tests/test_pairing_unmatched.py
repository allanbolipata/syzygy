"""Tests for GET /pairing/unmatched/ (the DMP-report unmatched set).

The Pairs UI used to download the whole DMP sample population in pages of
1000 and diff it client-side against the pairings; this endpoint computes
the same set server-side in one query. It must match the legacy semantics
the UI had: DMP samples only, excluded when a LATEST pairing under the
SAME logic touches either side of them.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import PairingLogic, PairingTable, Sample
from routers.pairings import list_unmatched_dmp_samples


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def db(engine):
    TestingSessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestingSessionLocal()
    # The engine is module-scoped (repo convention), so wipe rows to give
    # each test a clean slate.
    for model in (PairingTable, PairingLogic, Sample):
        session.execute(model.__table__.delete())
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _sample(db, primary_id, sample_type="DMP", tumor="TUMOR", created=None):
    s = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_name",
        patient__cmo=None,
        patient__dmp=None,
        sample_type=sample_type,
        gene_panel="IMPACT468",
        bait_set="agilent",
        tumor_or_normal=tumor,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        created_date=created or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _logic(db, name):
    l = PairingLogic(name=name)
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


def _pairing(db, logic, tumor, normal, latest=True):
    p = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        latest=latest,
        trigger_type="automatic",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_only_unpaired_dmp_samples_are_returned(db):
    logic = _logic(db, "DMP")
    t1 = _sample(db, "P1_T", tumor="TUMOR")
    n1 = _sample(db, "P1_N", tumor="NORMAL")
    t2 = _sample(db, "P2_T", tumor="TUMOR")
    n2 = _sample(db, "P2_N", tumor="NORMAL")
    _pairing(db, logic, t1, n1)

    rows = list_unmatched_dmp_samples("DMP", None, None, db)
    assert {s.primary_id for s in rows} == {"P2_T", "P2_N"}


def test_non_dmp_samples_are_excluded(db):
    logic = _logic(db, "DMP")
    _sample(db, "LIMS1", sample_type="LIMS", tumor="TUMOR")
    _sample(db, "IGO1", sample_type="IGO", tumor="NORMAL")
    dmp = _sample(db, "DMP1", sample_type="DMP", tumor="TUMOR")

    rows = list_unmatched_dmp_samples("DMP", None, None, db)
    assert [s.primary_id for s in rows] == ["DMP1"]


def test_pairing_under_other_logic_does_not_match(db):
    dmp_logic = _logic(db, "DMP")
    argos_logic = _logic(db, "ARGOS")
    t = _sample(db, "X1_T", tumor="TUMOR")
    n = _sample(db, "X1_N", tumor="NORMAL")
    _pairing(db, argos_logic, t, n)  # paired under ARGOS only

    rows = list_unmatched_dmp_samples("DMP", None, None, db)
    assert {s.primary_id for s in rows} == {"X1_T", "X1_N"}


def test_superseded_pairing_counts_as_unmatched(db):
    logic = _logic(db, "DMP")
    t = _sample(db, "Y1_T", tumor="TUMOR")
    n = _sample(db, "Y1_N", tumor="NORMAL")
    _pairing(db, logic, t, n, latest=False)

    rows = list_unmatched_dmp_samples("DMP", None, None, db)
    assert {s.primary_id for s in rows} == {"Y1_T", "Y1_N"}


def test_newest_first_ordering(db):
    logic = _logic(db, "DMP")
    base = datetime(2024, 5, 1, tzinfo=timezone.utc)
    _sample(db, "OLD", tumor="TUMOR", created=base)
    _sample(db, "NEW", tumor="NORMAL", created=base + timedelta(days=1))

    rows = list_unmatched_dmp_samples("DMP", None, None, db)
    assert [s.primary_id for s in rows] == ["NEW", "OLD"]


def test_unknown_logic_raises_404(db):
    with pytest.raises(HTTPException) as exc:
        list_unmatched_dmp_samples("NOPE", None, None, db)
    assert exc.value.status_code == 404


def test_limit_and_offset(db):
    logic = _logic(db, "DMP")
    for i in range(5):
        _sample(db, f"S{i}", tumor="TUMOR")

    page1 = list_unmatched_dmp_samples("DMP", 2, 0, db)
    page2 = list_unmatched_dmp_samples("DMP", 2, 2, db)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {s.primary_id for s in page1}.isdisjoint({s.primary_id for s in page2})
