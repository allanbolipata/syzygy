from datetime import datetime, timezone
from typing import Optional

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import trigger_pairing
from routers.samples import possible_matches_endpoint
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


@pytest.fixture
def argos(db):
    logic = PairingLogic(name="argos", strategy_name="argos")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _sample(
    db,
    primary_id,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    patient_id: Optional[str] = "C-1",
    request_id: Optional[str] = None,
    sample_type: str = "tissue",
    gene_panel: str = "IMPACT468",
    bait_set: Optional[str] = "agilent",
    run_mode: Optional[str] = None,
    sequencing_date: Optional[datetime] = None,
    preservation: Optional[list] = None,
    sample_origin: Optional[list] = None,
):
    # Real samples carry preservation data; the ARGOS gate requires
    # resolvable, agreeing categories, so fixtures default to FFPE
    # (callers can override to test the gate itself).
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient_id=patient_id,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        request_id=request_id,
        run_mode=run_mode,
        preservation=preservation if preservation is not None else ["FFPE"],
        sample_origin=sample_origin if sample_origin is not None else ["FFPE"],
        sequencing_date=sequencing_date,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _endpoints(db, sample: Sample, logic_name="argos"):
    res = possible_matches_endpoint(sample.id, pairing_logic_name=logic_name, db=db)
    return res


def _others(res):
    return {c["sample"].primary_id: c for c in res["other_matches"]}


# ---------------------------------------------------------------------------
# Tumor side
# ---------------------------------------------------------------------------


def test_tumor_current_and_other_matches(db, argos):
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="NovaSeq X 1000",
                 sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    n1 = _sample(db, "N1", request_id="R1", run_mode="NovaSeq X 1000",
                 sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc))
    n2 = _sample(db, "N2", request_id="R2", run_mode="HiSeq 4000",
                 sequencing_date=datetime(2023, 1, 1, tzinfo=timezone.utc))
    n3 = _sample(db, "N3", request_id="R1", bait_set="idt",
                 run_mode="NovaSeq X 1000")
    n4 = _sample(db, "N4", patient_id="C-2", request_id="R1")
    p1 = _sample(db, "P1", patient_id=None, sample_type="Pooled Normal",
                 run_mode=None,
                 sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc))
    p2 = _sample(db, "P2", patient_id=None, sample_type="Pooled Normal",
                 gene_panel="IMPACT505")

    trigger_pairing(db, t1, argos, "automatic")

    res = _endpoints(db, t1)
    assert res["pairing_logic"] == "argos"

    current = res["current_match"]
    assert current is not None
    assert current["sample"].primary_id == "N1"
    assert current["trigger_type"] == "automatic"

    others = _others(res)
    # The current match is not repeated among the other matches.
    assert "N1" not in others
    # Out-of-scope candidates: different patient (N4), different bait set
    # (N3), different pooled panel (P2) — no cascade tier can reach them.
    assert "N4" not in others
    assert "N3" not in others
    assert "P2" not in others

    assert others["N2"]["tier"] == "other_request"
    assert others["N2"]["matched"] is False
    assert any("run_mode mismatch" in r for r in others["N2"]["mismatched_keys"])
    assert others["N2"]["would_be_selected"] is False

    assert others["P1"]["tier"] == "pooled"
    assert others["P1"]["matched"] is True

    # Ordered by tier (other_request before pooled), matched first.
    assert [c["sample"].primary_id for c in res["other_matches"]] == [
        "N2", "P1",
    ]

    # Whole-scope summary, independent of the page.
    assert res["other_matches_total"] == 2
    assert res["other_matches_matched"] == 1
    assert res["other_matches_tiers"] == {"other_request": 1, "pooled": 1}
    assert res["other_matches_breakdown"] == [
        {"mismatched_keys": ["run_mode mismatch"], "count": 1},
    ]


def test_tumor_without_current_match_marks_cascade_winner(db, argos):
    # DMP-style tumor: no patient normals, unknown bait set and run mode.
    t2 = _sample(db, "T2", TumorOrNormal.TUMOR.value, patient_id="C-9",
                 sample_type="DMP", bait_set=None, run_mode=None)
    p1 = _sample(db, "P1", patient_id=None, sample_type="Pooled Normal")
    p3 = _sample(db, "P3", patient_id=None, sample_type="Pooled Normal",
                 bait_set=None)

    res = _endpoints(db, t2)
    assert res["current_match"] is None

    others = _others(res)
    # P1's bait set differs from the tumor's (null), so the pooled tier
    # can never reach T2 via P1: it is out of scope.
    assert "P1" not in others
    assert others["P3"]["matched"] is True
    assert others["P3"]["would_be_selected"] is True


# ---------------------------------------------------------------------------
# Normal side
# ---------------------------------------------------------------------------


def test_normal_current_and_other_matches(db, argos):
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="NovaSeq X 1000")
    n1 = _sample(db, "N1", request_id="R1", run_mode="NovaSeq X 1000")
    t3 = _sample(db, "T3", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="HiSeq 4000")
    t4 = _sample(db, "T4", TumorOrNormal.TUMOR.value, run_mode="NovaSeq X 1000")

    trigger_pairing(db, t1, argos, "automatic")

    res = _endpoints(db, n1)
    current = res["current_match"]
    assert current is not None
    assert current["sample"].primary_id == "T1"
    assert current["trigger_type"] == "automatic"

    others = _others(res)
    assert "T1" not in others
    assert others["T3"]["tier"] == "same_request"
    assert others["T3"]["matched"] is False
    assert any("run_mode mismatch" in r for r in others["T3"]["mismatched_keys"])
    assert others["T4"]["tier"] == "other_request"
    assert others["T4"]["matched"] is True
    # would_be_selected is only defined for tumor inputs.
    assert others["T3"]["would_be_selected"] is False
    assert others["T4"]["would_be_selected"] is False


def test_pooled_normal_lists_cross_patient_tumors(db, argos):
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="NovaSeq X 1000")
    t2 = _sample(db, "T2", TumorOrNormal.TUMOR.value, patient_id="C-9",
                 sample_type="DMP", bait_set=None, run_mode=None)
    p3 = _sample(db, "P3", patient_id=None, sample_type="Pooled Normal",
                 bait_set=None)

    res = _endpoints(db, p3)
    assert res["current_match"] is None

    others = _others(res)
    assert others["T2"]["tier"] == "pooled"
    assert others["T2"]["matched"] is True
    # T1's bait set differs from the pooled normal's, so no tier can
    # reach it: it is out of the candidate scope.
    assert "T1" not in others


def test_normal_with_multiple_tumors_lists_all_current_matches(db, argos):
    # A normal matches 1-to-many. Pair it to two different tumors and keep
    # both rows latest (this is a state the auto-pair service never produces
    # on its own, so the rows are inserted directly). possible_matches must
    # surface both current tumors and must not repeat them as other matches.
    n1 = _sample(db, "N1", request_id="R1", run_mode="NovaSeq X 1000")
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="NovaSeq X 1000")
    t2 = _sample(db, "T2", TumorOrNormal.TUMOR.value, request_id="R2",
                 run_mode="NovaSeq X 1000")
    for tumor in (t1, t2):
        db.add(PairingTable(
            pairing_logic_id=argos.id,
            tumor_id=tumor.id,
            normal_id=n1.id,
            trigger_type="automatic",
            latest=True,
        ))
    db.commit()

    res = _endpoints(db, n1)
    current_ids = sorted(c["sample"].primary_id for c in res["current_matches"])
    assert current_ids == ["T1", "T2"]
    # ``current_match`` mirrors the first current match (backward compat).
    assert res["current_match"]["sample"].primary_id == \
        res["current_matches"][0]["sample"].primary_id
    # Neither current tumor is repeated among the other matches.
    others = _others(res)
    assert "T1" not in others
    assert "T2" not in others


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_sample_404(db, argos):
    with pytest.raises(HTTPException) as exc_info:
        possible_matches_endpoint(uuid.uuid4(), pairing_logic_name="argos", db=db)
    assert exc_info.value.status_code == 404


def test_unknown_logic_404(db):
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value)
    with pytest.raises(HTTPException) as exc_info:
        possible_matches_endpoint(t1.id, pairing_logic_name="NOPE", db=db)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_other_matches_paging(db, argos):
    # Six same-patient normals: three on the tumor's request, three on
    # another. No pairing is triggered, so all six are candidates.
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, request_id="R1",
                 run_mode="NovaSeq X 1000")
    for i in range(1, 4):
        _sample(db, f"N{i}", request_id="R1", run_mode="NovaSeq X 1000")
    for i in range(4, 7):
        _sample(db, f"N{i}", request_id="R2", run_mode="NovaSeq X 1000")

    first = possible_matches_endpoint(
        t1.id, pairing_logic_name="argos", limit=2, offset=0, db=db
    )
    second = possible_matches_endpoint(
        t1.id, pairing_logic_name="argos", limit=2, offset=2, db=db
    )
    # The page is a bounded slice; the totals describe the whole scope,
    # not the page, so they are identical for both pages.
    assert first["other_matches_total"] == second["other_matches_total"] == 6
    assert first["other_matches_matched"] == second["other_matches_matched"] == 6
    assert first["other_matches_tiers"] == second["other_matches_tiers"] == {
        "same_request": 3, "other_request": 3,
    }
    assert first["other_matches_breakdown"] == []
    assert len(first["other_matches"]) == 2
    assert len(second["other_matches"]) == 2
    pids_first = {c["sample"].primary_id for c in first["other_matches"]}
    pids_second = {c["sample"].primary_id for c in second["other_matches"]}
    assert not pids_first & pids_second

    # A page past the end is empty, but the scope totals are unchanged.
    beyond = possible_matches_endpoint(
        t1.id, pairing_logic_name="argos", limit=2, offset=100, db=db
    )
    assert beyond["other_matches"] == []
    assert beyond["other_matches_total"] == 6


def test_limit_validation(db, argos):
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value)
    for bad in (0, 5001):
        with pytest.raises(HTTPException) as exc_info:
            possible_matches_endpoint(
                t1.id, pairing_logic_name="argos", limit=bad, db=db
            )
        assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        possible_matches_endpoint(t1.id, pairing_logic_name="argos", offset=-1, db=db)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Case-insensitive hard keys
# ---------------------------------------------------------------------------


def test_hard_keys_compared_case_insensitively(db, argos):
    # Production data stores the same values in different cases across
    # sample types; matching keys must compare case-insensitively.
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, patient_id="C-1",
                 gene_panel="IMPACT468", bait_set="Agilent",
                 run_mode="NovaSeq X 1000")
    n1 = _sample(db, "N1", patient_id="c-1", gene_panel="impact468",
                 bait_set="AGILENT", run_mode="NovaSeq X 1000")

    res = _endpoints(db, t1)
    others = _others(res)
    assert "N1" in others
    assert others["N1"]["matched"] is True
    assert others["N1"]["mismatched_keys"] is None


def test_pooled_tier_matches_across_case(db, argos):
    # The pooled tier was dead in production data: pooled normals store
    # e.g. "impact505" while tumors store "IMPACT505".
    t1 = _sample(db, "T1", TumorOrNormal.TUMOR.value, patient_id="C-1",
                 gene_panel="IMPACT468", bait_set="agilent")
    p1 = _sample(db, "P1", patient_id=None, sample_type="Pooled Normal",
                 gene_panel="impact468", bait_set="agilent")

    res = _endpoints(db, t1)
    others = _others(res)
    assert others["P1"]["tier"] == "pooled"
    assert others["P1"]["matched"] is True
