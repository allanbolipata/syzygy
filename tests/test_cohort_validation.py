"""Tests for cohort sample classification (cohort_validation.classify_cohort).

One test per classification branch:
1. tumorNotFound (no Sample row),
2. conflictReason (latest row with mismatched_keys),
3. conflictReason (latest row, requested normal differs),
4. valid (latest row; current match wins over other eligible candidates),
5. unpairedReason not retryable (no bait-set logic / logic inactive; DMP),
6. unpairedReason retryable (0 eligible candidates),
7. conflictReason ambiguity (>= 2 eligible candidates, no current match),
8. valid (exactly 1 eligible candidate -> pair created via pairing_service).

Conventions: in-memory SQLite + StaticPool, seed Sample/PairingTable rows
per test, per-test cleanup, direct calls.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cohort_validation import classify_cohort
from database import Base
from models import BaitSet, PairingLogic, PairingTable, Sample
from schemas import TumorOrNormal


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
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


def _add_sample(
    db,
    primary_id,
    cmo,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    patient__cmo="C-10001",
    gene_panel="PANEL-A",
    bait_set="BAIT-1",
    sample_type="tissue",
    **kw,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo,
        patient__cmo=patient__cmo,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        **kw,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _tumor(db, primary_id="s-10001_A", cmo="C-10001-X001-d01", **kw):
    """A tumor whose DB primary_id carries the legacy ``s-`` prefix, so
    the test exercises id-space normalization (request sends
    ``10001_A``)."""
    return _add_sample(
        db, primary_id, cmo, tumor_or_normal=TumorOrNormal.TUMOR.value, **kw
    )


def _setup_logic(db, bait="BAIT-1", name="TEMPO", active=True):
    """Active pairing logic + BaitSet row mapping ``bait`` to it."""
    logic = PairingLogic(name=name, active=active)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    db.add(BaitSet(bait_set=bait, pairing_logic_id=logic.id))
    db.commit()
    return logic


def _cohort(*samples):
    return {"cohortId": "COH-1", "samples": list(samples)}


def _entry(**kw):
    entry = {"primaryId": "10001_A", "cmoId": "C-10001-X001-d01"}
    entry.update(kw)
    return entry


def _assert_finding_shape(finding, reason_key, primary_id, cmo_id, expected_key_values=None):
    assert set(finding.keys()) == {"primaryId", "cmoId", reason_key}
    assert finding["primaryId"] == primary_id
    assert finding["cmoId"] == cmo_id
    assert isinstance(finding[reason_key], (str, bool))
    assert finding[reason_key]
    if expected_key_values:
        for needle in expected_key_values:
            assert needle in str(finding[reason_key])


def test_tumor_not_found(db):
    _setup_logic(db)
    passes, findings, pairs = classify_cohort(
        db,
        _cohort(_entry(primaryId="99999_A", cmoId="C-99999-X001-d01")),
    )
    assert passes is False
    assert pairs == []
    assert len(findings) == 1
    assert set(findings[0].keys()) == {"primaryId", "cmoId", "tumorNotFound"}
    assert findings[0] == {
        "primaryId": "99999_A",
        "cmoId": "C-99999-X001-d01",
        "tumorNotFound": True,
    }


def test_latest_row_with_mismatched_keys_is_conflict(db):
    logic = _setup_logic(db)
    tumor = _tumor(db)
    normal = _add_sample(db, "NORMAL-1", "C-10001-N001-d01", patient__cmo="C-99999")
    db.add(
        PairingTable(
            tumor_id=tumor.id,
            normal_id=normal.id,
            pairing_logic_id=logic.id,
            latest=True,
            trigger_type="override",
            mismatched_keys=["patient__cmo mismatch (C-10001 vs C-99999)"],
        )
    )
    db.commit()

    passes, findings, pairs = classify_cohort(db, _cohort(_entry()))
    assert passes is False
    assert pairs == []
    assert len(findings) == 1
    _assert_finding_shape(findings[0], "conflictReason", "10001_A", "C-10001-X001-d01", ["mismatch"])


def test_latest_row_requested_normal_differs_is_conflict(db):
    logic = _setup_logic(db)
    tumor = _tumor(db)
    current = _add_sample(db, "NORMAL-A", "C-10001-N001-d01")
    requested = _add_sample(db, "NORMAL-B", "C-10001-N002-d01")
    db.add(
        PairingTable(
            tumor_id=tumor.id,
            normal_id=current.id,
            pairing_logic_id=logic.id,
            latest=True,
            trigger_type="automatic",
        )
    )
    db.commit()

    passes, findings, pairs = classify_cohort(
        db, _cohort(_entry(normalCmoId="C-10001-N002-d01"))
    )
    assert passes is False
    assert pairs == []
    assert len(findings) == 1
    _assert_finding_shape(
        findings[0], "conflictReason", "10001_A", "C-10001-X001-d01",
        ["differs from the tumor's current match"],
    )
    assert "C-10001-N002-d01" in findings[0]["conflictReason"]
    assert requested.id is not None  # seed sanity


def test_latest_row_matching_requested_normal_is_valid_and_current_match_wins(db):
    logic = _setup_logic(db)
    tumor = _tumor(db)
    current = _add_sample(db, "NORMAL-A", "C-10001-N001-d01")
    # A second fully-eligible normal exists, but the assigned current match
    # wins: no ambiguity, no conflict.
    _add_sample(db, "NORMAL-B", "C-10001-N002-d01")
    db.add(
        PairingTable(
            tumor_id=tumor.id,
            normal_id=current.id,
            pairing_logic_id=logic.id,
            latest=True,
            trigger_type="automatic",
        )
    )
    db.commit()

    passes, findings, pairs = classify_cohort(
        db, _cohort(_entry(normalCmoId="C-10001-N001-d01"))
    )
    assert passes is True
    assert findings == []
    assert pairs == [("C-10001-X001-d01", "C-10001-N001-d01")]

    # Same tumor with no requested normal at all: still valid.
    passes, findings, pairs = classify_cohort(db, _cohort(_entry()))
    assert passes is True
    assert findings == []
    assert pairs == [("C-10001-X001-d01", "C-10001-N001-d01")]


def test_no_logic_or_inactive_logic_is_unpaired_not_retryable(db):
    # DMP sample: bait_set is None, so no pairing logic can exist.
    dmp = _tumor(
        db,
        primary_id="10002_D",
        cmo="C-10002-X001-d01",
        sample_type="DMP",
        bait_set=None,
        patient__cmo=None,
        patient__dmp="P-10002",
        anon_id="A-10002",
    )
    # A second tumor whose bait set is registered to an INACTIVE logic.
    _setup_logic(db, bait="BAIT-1", name="TEMPO")
    inactive = PairingLogic(name="OFF-LOGIC", active=False)
    db.add(inactive)
    db.commit()
    db.refresh(inactive)
    db.add(BaitSet(bait_set="BAIT-OFF", pairing_logic_id=inactive.id))
    db.commit()
    _add_sample(
        db, "NORMAL-X", "C-10003-N001-d01",
        patient__cmo="C-10003", bait_set="BAIT-OFF",
    )
    off = _tumor(
        db, primary_id="10003_A", cmo="C-10003-X001-d01",
        patient__cmo="C-10003", bait_set="BAIT-OFF",
    )
    assert dmp.id is not None and off.id is not None  # seed sanity

    passes, findings, pairs = classify_cohort(
        db,
        _cohort(
            _entry(primaryId="10002_D", cmoId="C-10002-X001-d01"),
            _entry(primaryId="10003_A", cmoId="C-10003-X001-d01"),
        ),
    )
    assert passes is False
    assert pairs == []
    assert len(findings) == 2
    dmp_finding = next(f for f in findings if f["cmoId"] == "C-10002-X001-d01")
    off_finding = next(f for f in findings if f["cmoId"] == "C-10003-X001-d01")
    _assert_finding_shape(dmp_finding, "unpairedReason", "10002_D", "C-10002-X001-d01", ["DMP", "not retryable"])
    _assert_finding_shape(off_finding, "unpairedReason", "10003_A", "C-10003-X001-d01", ["not active", "not retryable"])


def test_zero_eligible_candidates_is_unpaired_retryable(db):
    _setup_logic(db)
    _tumor(db)
    # Only a normal for a different patient exists: out of the candidate
    # scope, so 0 eligible candidates.
    _add_sample(db, "NORMAL-OTHER", "C-99999-N001-d01", patient__cmo="C-99999")

    passes, findings, pairs = classify_cohort(db, _cohort(_entry()))
    assert passes is False
    assert pairs == []
    assert len(findings) == 1
    _assert_finding_shape(findings[0], "unpairedReason", "10001_A", "C-10001-X001-d01", ["No eligible normal candidates", "retryable"])
    # No pairing may have been created.
    assert db.scalars(select(PairingTable)).first() is None


def test_two_or_more_eligible_candidates_without_current_match_is_ambiguity(db):
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")
    _add_sample(db, "NORMAL-B", "C-10001-N002-d01")

    passes, findings, pairs = classify_cohort(db, _cohort(_entry()))
    assert passes is False
    assert pairs == []
    assert len(findings) == 1
    _assert_finding_shape(findings[0], "conflictReason", "10001_A", "C-10001-X001-d01", ["2 eligible normal candidates", "ambiguity"])
    # Ambiguity must not create a pairing.
    assert db.scalars(select(PairingTable)).first() is None


def test_single_eligible_candidate_creates_pair_and_passes(db):
    _setup_logic(db)
    tumor = _tumor(db)
    normal = _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    passes, findings, pairs = classify_cohort(db, _cohort(_entry()))
    assert passes is True
    assert findings == []
    assert pairs == [("C-10001-X001-d01", "C-10001-N001-d01")]

    # The pair was actually created via pairing_service (automatic trigger).
    row = db.scalars(select(PairingTable)).first()
    assert row is not None
    assert row.tumor_id == tumor.id
    assert row.normal_id == normal.id
    assert row.latest is True
    assert row.trigger_type == "automatic"
    assert row.mismatched_keys is None
