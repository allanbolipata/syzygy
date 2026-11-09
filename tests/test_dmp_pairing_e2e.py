"""E2E tests for automatic DMP pairing (DMP 4/4).

Covers the full trigger path end-to-end under the DMP logic. DMP
Reference Alignment 3/8 replaced the single-winner (patient__dmp +
gene_panel) semantics with the reference two-tier pairing
(docs/dmp_reference_alignment.md): tier 1 matches on (patient,
pool_id), tier 2 (IM_ver rescue) only when a tumor has no tier-1
match, and there is NO winner — every matching (tumor, normal) pair
becomes its own pairing row (multi_pair fan-out).

  * a DMP TUMOR + DMP NORMAL with equal patient and pool auto-pair
    when triggered (tumor side and normal side);
  * a DMP tumor with no same-patient pool/IM_ver counterpart stays
    unpaired;
  * a LIMS tumor does NOT auto-pair to a DMP normal (cross-group stays
    manual even when patient__cmo matches) — under both the DMP logic
    and the ARGOS logic;
  * a manually created DMP pairing survives an automatic re-trigger
    (manual wins; the automatic run returns the existing pairing and
    never replaces it);
  * a DMP tumor with two same-pool candidate normals ends up with TWO
    pairing rows (no winner), and re-triggering is idempotent.

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
    additional_metadata=None,
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
        additional_metadata=additional_metadata,
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
    tumor = _dmp_sample(
        db, "DMP-T-1", "P-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-1"},
    )
    normal = _dmp_sample(
        db, "DMP-N-1", "P-1", additional_metadata={"project_name": "POOL-1"},
    )

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
    tumor = _dmp_sample(
        db, "DMP-T-2", "P-2", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-2"},
    )
    # Tumor arrives first with no normal: nothing to pair.
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None
    assert _get_latest_pairing(db, tumor.id, dmp_logic.id) is None

    # Normal arrives later: the normal-side trigger pairs it up.
    normal = _dmp_sample(
        db, "DMP-N-2", "P-2", additional_metadata={"project_name": "POOL-2"},
    )
    trigger_pairing_for_normal(db, normal, dmp_logic)

    pairing = _get_latest_pairing(db, tumor.id, dmp_logic.id)
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
    assert pairing.trigger_type == "automatic"


def test_dmp_gene_panel_mismatch_still_pairs(db, dmp_logic):
    # gene_panel is NO LONGER a pairing key (the reference joins never
    # use it): a same-patient same-pool cross-panel pair matches.
    tumor = _dmp_sample(
        db, "DMP-T-3", "P-3", tumor_or_normal=TumorOrNormal.TUMOR.value,
        gene_panel="IMPACT468", additional_metadata={"project_name": "POOL-3"},
    )
    # Same DMP patient, same pool, but a different assay (ACCESS).
    normal = _dmp_sample(
        db, "DMP-N-3", "P-3", gene_panel="ACCESS327",
        additional_metadata={"project_name": "POOL-3"},
    )

    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    assert pairing.normal_id == normal.id
    assert _get_latest_pairing(db, tumor.id, dmp_logic.id) is not None


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
    tumor = _dmp_sample(
        db, "DMP-T-7", "P-7", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-7"},
    )
    normal_a = _dmp_sample(
        db, "DMP-N-7A", "P-7", additional_metadata={"project_name": "POOL-7"},
    )
    # A second same-pool candidate so the match set is well defined
    # (without the manual pin, the fan-out would yield two rows).
    _dmp_sample(
        db, "DMP-N-7B", "P-7", additional_metadata={"project_name": "POOL-7"},
    )

    # A human pins the pairing manually (an explicit pin to normal_a):
    # the row set is replaced by the single pinned row.
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


def test_two_candidate_normals_fan_out_no_winner(db, dmp_logic):
    # No winner selection: two same-pool candidate normals yield TWO
    # pairing rows (the reference left_join fan-out).
    tumor = _dmp_sample(
        db, "DMP-T-8", "P-8", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-8"},
    )
    # Create the higher primary_id first: the representative row is the
    # FIRST in primary_id string order, independent of insert order.
    normal_b = _dmp_sample(
        db, "DMP-N-8B", "P-8",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        additional_metadata={"project_name": "POOL-8"},
    )
    normal_a = _dmp_sample(
        db, "DMP-N-8A", "P-8",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        additional_metadata={"project_name": "POOL-8"},
    )

    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    # The returned row is the deterministic representative.
    assert pairing.normal_id == normal_a.id

    rows = _pairings_for(db, tumor.id, dmp_logic.id)
    assert len(rows) == 2
    assert {p.normal_id for p in rows} == {normal_a.id, normal_b.id}
    assert [p for p in rows if p.latest] == rows
    assert all(p.multi_pair for p in rows)


def test_auto_retrigger_is_idempotent(db, dmp_logic):
    tumor = _dmp_sample(
        db, "DMP-T-9", "P-9", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-9"},
    )
    _dmp_sample(
        db, "DMP-N-9A", "P-9", additional_metadata={"project_name": "POOL-9"},
    )
    normal_newer = _dmp_sample(
        db, "DMP-N-9B", "P-9",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        additional_metadata={"project_name": "POOL-9"},
    )

    first = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert first is not None
    # No winner: both same-pool normals are in the row set.
    assert len(_pairings_for(db, tumor.id, dmp_logic.id)) == 2

    # A second automatic run (re-import) returns the same representative
    # row and leaves the row set untouched — no duplicates.
    second = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert second.id == first.id
    assert second.normal_id == first.normal_id
    assert len(_pairings_for(db, tumor.id, dmp_logic.id)) == 2
    # The newer normal is part of the row set (fan-out).
    row_normals = {
        p.normal_id for p in _pairings_for(db, tumor.id, dmp_logic.id)
    }
    assert normal_newer.id in row_normals


def test_dmp_pairing_does_not_leak_into_other_logic(db, dmp_logic, argos_logic):
    # A DMP T/N pair under the DMP logic creates no pairing row under
    # the ARGOS logic (pairings are per logic).
    tumor = _dmp_sample(
        db, "DMP-T-10", "P-10", tumor_or_normal=TumorOrNormal.TUMOR.value,
        additional_metadata={"project_name": "POOL-10"},
    )
    _dmp_sample(
        db, "DMP-N-10", "P-10", additional_metadata={"project_name": "POOL-10"},
    )
    trigger_pairing(db, tumor, dmp_logic, "automatic")

    assert _pairings_for(db, tumor.id, dmp_logic.id) != []
    assert _pairings_for(db, tumor.id, argos_logic.id) == []
