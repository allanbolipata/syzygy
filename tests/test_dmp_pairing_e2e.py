"""E2E tests for automatic DMP pairing (DMP 4/4).

Covers the full trigger path end-to-end under the DMP logic:
  * a DMP TUMOR + DMP NORMAL with equal patient__dmp and gene_panel
    auto-pair when triggered (tumor side and normal side);
  * a DMP tumor with no same-patient__dmp same-gene-panel counterpart
    stays unpaired;
  * a LIMS tumor does NOT auto-pair to a DMP normal (cross-group stays
    manual even when patient__cmo matches) — under both the DMP logic
    and the ARGOS logic;
  * a manually created DMP pairing survives an automatic re-trigger
    (manual wins; the automatic run returns the existing pairing and
    never replaces it);
  * a DMP tumor with two candidate normals ends up with exactly one
    pairing (deterministic primary_id pick), and re-triggering is
    idempotent.

Same in-memory SQLite fixture style as tests/test_pairing_service.py.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    _get_latest_pairing,
    trigger_pairing,
    trigger_pairing_for_normal,
)
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
def dmp_logic(db):
    logic = PairingLogic(name="DMP", strategy_name="dmp")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


@pytest.fixture
def argos_logic(db):
    logic = PairingLogic(name="ARGOS", strategy_name="argos")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _dmp_sample(
    db,
    primary_id,
    patient__dmp,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    gene_panel="IMPACT468",
    patient__cmo=None,
    anon_id=None,
    sequencing_date=None,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient__cmo=patient__cmo,
        patient__dmp=patient__dmp,
        anon_id=anon_id,
        sample_type="DMP",
        gene_panel=gene_panel,
        tumor_or_normal=tumor_or_normal,
        sequencing_date=sequencing_date,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _lims_sample(
    db,
    primary_id,
    patient__cmo,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    gene_panel="IMPACT468",
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient__cmo=patient__cmo,
        sample_type="tissue",
        gene_panel=gene_panel,
        bait_set="BAIT-1",
        tumor_or_normal=tumor_or_normal,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _pairings_for(db, tumor_id, logic_id):
    return db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_id,
            PairingTable.pairing_logic_id == logic_id,
        )
    ).all()


# ---------------------------------------------------------------------------
# Automatic DMP pairing: the happy path
# ---------------------------------------------------------------------------


def test_dmp_tumor_and_normal_auto_pair_under_dmp_logic(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-1", "P-1", tumor_or_normal=TumorOrNormal.TUMOR.value)
    normal = _dmp_sample(db, "DMP-N-1", "P-1")

    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")

    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
    assert pairing.trigger_type == "automatic"
    assert pairing.latest is True
    # Notes record the single dmp tier.
    assert pairing.notes is not None
    assert any("tier=dmp" in note for note in pairing.notes)


def test_dmp_normal_arriving_later_auto_pairs(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-2", "P-2", tumor_or_normal=TumorOrNormal.TUMOR.value)
    # Tumor arrives first with no normal: nothing to pair.
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None
    assert _get_latest_pairing(db, tumor.id, dmp_logic.id) is None

    # Normal arrives later: the normal-side trigger pairs it up.
    normal = _dmp_sample(db, "DMP-N-2", "P-2")
    trigger_pairing_for_normal(db, normal, dmp_logic)

    pairing = _get_latest_pairing(db, tumor.id, dmp_logic.id)
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
    assert pairing.trigger_type == "automatic"


def test_dmp_gene_panel_mismatch_stays_unpaired(db, dmp_logic):
    tumor = _dmp_sample(
        db, "DMP-T-3", "P-3", tumor_or_normal=TumorOrNormal.TUMOR.value,
        gene_panel="IMPACT468",
    )
    # Same DMP patient but a different assay (ACCESS): invalid pair.
    _dmp_sample(db, "DMP-N-3", "P-3", gene_panel="ACCESS327")

    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None
    assert _get_latest_pairing(db, tumor.id, dmp_logic.id) is None


def test_dmp_tumor_without_counterpart_stays_unpaired(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-4", "P-4", tumor_or_normal=TumorOrNormal.TUMOR.value)
    # A DMP normal of a different patient exists, but no match.
    _dmp_sample(db, "DMP-N-4", "P-9")

    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None
    assert _pairings_for(db, tumor.id, dmp_logic.id) == []


# ---------------------------------------------------------------------------
# Cross-group stays manual: LIMS vs DMP
# ---------------------------------------------------------------------------


def test_lims_tumor_does_not_auto_pair_to_dmp_normal(db, dmp_logic):
    # Same CMO patient on both sides, but the normal is a DMP sample.
    lims_tumor = _lims_sample(
        db, "LIMS-T-1", "C-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
    )
    _dmp_sample(db, "DMP-N-5", "P-5", patient__cmo="C-1")

    # Under the DMP logic the tumor side is not DMP: never paired.
    assert trigger_pairing(db, lims_tumor, dmp_logic, "automatic") is None
    assert _pairings_for(db, lims_tumor.id, dmp_logic.id) == []

    # And under the ARGOS logic, DMP candidates are manual-only: also
    # never auto-paired.
    argos = PairingLogic(name="ARGOS", strategy_name="argos")
    db.add(argos)
    db.commit()
    db.refresh(argos)
    assert trigger_pairing(db, lims_tumor, argos, "automatic") is None
    assert _pairings_for(db, lims_tumor.id, argos.id) == []


def test_dmp_normal_side_ignores_lims_tumor(db, dmp_logic):
    # A DMP normal must not sweep in a LIMS tumor on the same CMO patient.
    _lims_sample(db, "LIMS-T-2", "C-2", tumor_or_normal=TumorOrNormal.TUMOR.value)
    normal = _dmp_sample(db, "DMP-N-6", "P-6", patient__cmo="C-2")

    trigger_pairing_for_normal(db, normal, dmp_logic)

    # No pairing for the LIMS tumor under the DMP logic.
    lims_tumor = db.scalar(select(Sample).where(Sample.primary_id == "LIMS-T-2"))
    assert _pairings_for(db, lims_tumor.id, dmp_logic.id) == []


# ---------------------------------------------------------------------------
# Manual precedence and idempotency
# ---------------------------------------------------------------------------


def test_manual_dmp_pairing_survives_auto_retrigger(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-7", "P-7", tumor_or_normal=TumorOrNormal.TUMOR.value)
    normal_a = _dmp_sample(db, "DMP-N-7A", "P-7")
    # A second candidate normal so the deterministic winner is well
    # defined (normal_a, the first in primary_id string order).
    _dmp_sample(db, "DMP-N-7B", "P-7")

    # A human pins the pairing manually (an explicit pin to normal_a).
    manual = trigger_pairing(db, tumor, dmp_logic, "manual", normal_a.id)
    assert manual is not None
    assert manual.normal_id == normal_a.id
    assert manual.trigger_type == "manual"

    # The automatic phase re-runs (as on a second import pass): it must
    # return the existing manual pairing unchanged — never replaced,
    # never re-picked.
    retrigger = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert retrigger is not None
    assert retrigger.id == manual.id
    assert retrigger.normal_id == normal_a.id
    assert retrigger.trigger_type == "manual"
    latest = _get_latest_pairing(db, tumor.id, dmp_logic.id)
    assert latest.id == manual.id

    # Still exactly one pairing row: no duplicate, no replacement row.
    assert len(_pairings_for(db, tumor.id, dmp_logic.id)) == 1


def test_two_candidate_normals_exactly_one_deterministic(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-8", "P-8", tumor_or_normal=TumorOrNormal.TUMOR.value)
    # Create the higher primary_id first: the winner is the FIRST in
    # primary_id string order, independent of insert order.
    _dmp_sample(
        db, "DMP-N-8B", "P-8",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    normal_a = _dmp_sample(
        db, "DMP-N-8A", "P-8",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    assert pairing.normal_id == normal_a.id

    rows = _pairings_for(db, tumor.id, dmp_logic.id)
    assert len(rows) == 1
    assert [p for p in rows if p.latest] == rows


def test_auto_retrigger_is_idempotent(db, dmp_logic):
    tumor = _dmp_sample(db, "DMP-T-9", "P-9", tumor_or_normal=TumorOrNormal.TUMOR.value)
    _dmp_sample(db, "DMP-N-9A", "P-9")
    normal_newer = _dmp_sample(
        db, "DMP-N-9B", "P-9",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )

    first = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert first is not None

    # A second automatic run (re-import) returns the same pairing even
    # though a newer matching normal now exists.
    second = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert second.id == first.id
    assert second.normal_id == first.normal_id
    assert len(_pairings_for(db, tumor.id, dmp_logic.id)) == 1
    # The newer normal is untouched.
    assert _pairings_for(db, normal_newer.id, dmp_logic.id) == []


def test_dmp_pairing_does_not_leak_into_other_logic(db, dmp_logic, argos_logic):
    # A DMP T/N pair under the DMP logic creates no pairing row under
    # the ARGOS logic (pairings are per logic).
    tumor = _dmp_sample(db, "DMP-T-10", "P-10", tumor_or_normal=TumorOrNormal.TUMOR.value)
    _dmp_sample(db, "DMP-N-10", "P-10")
    trigger_pairing(db, tumor, dmp_logic, "automatic")

    assert _pairings_for(db, tumor.id, dmp_logic.id) != []
    assert _pairings_for(db, tumor.id, argos_logic.id) == []
