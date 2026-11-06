"""Unit tests for the DMP pairing strategy (``strategies["dmp"]``).

DMP Reference Alignment 3/8: the strategy mirrors the reference
``pair_tumor_with_normal.R`` two-tier pairing (docs/
dmp_reference_alignment.md, sections b and g):

* **Tier 1** — every DMP normal with the same remapped ``patient`` and
  the same ``pool_id`` (``additional_metadata.pool_id`` falling back to
  ``additional_metadata.project_name``);
* **Tier 2 (IM_ver rescue)** — ONLY for tumors with no tier-1 match:
  every DMP normal with the same ``patient`` and the same IM_ver (the
  last dash-segment of the sample name).

There is NO winner selection: every matching (tumor, normal) pair
becomes its own pairing row (``multi_pair=True``), a normal may appear
in several rows, and a missing (NA) key never matches — dplyr
left_join semantics, exact/case-sensitive. ``gene_panel`` is no longer
a pairing key. LIMS / pooled-normal / cross-group candidates never
match here — they stay manual under the argos strategy.
"""

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    get_pairing_strategy,
    trigger_pairing,
    trigger_pairing_for_normal,
)
from pairing_strategies import DmpPairingStrategy, strategies
from routers.samples import possible_matches_endpoint
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


@pytest.fixture
def dmp_logic(db: Session) -> PairingLogic:
    logic = PairingLogic(name="DMP", strategy_name="dmp")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


@pytest.fixture
def argos_logic(db: Session) -> PairingLogic:
    logic = PairingLogic(name="ARGOS", strategy_name="argos")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _sample(
    db: Session,
    primary_id: str,
    *,
    tumor_or_normal: str = TumorOrNormal.NORMAL.value,
    patient__cmo: Optional[str] = "C-1",
    patient__dmp: Optional[str] = None,
    sample_type: str = "DMP",
    gene_panel: str = "IMPACT468",
    bait_set: str = "agilent",
    additional_metadata: Optional[dict] = None,
) -> Sample:
    """Build a sample (DMP by default — the group under test); DMP rows
    arrive without LIMS metadata. ``additional_metadata`` carries the
    reference join keys (``pool_id`` / ``project_name``)."""
    kwargs: dict[str, object] = dict(
        primary_id=primary_id,
        cmo_sample_name=primary_id,
        tumor_or_normal=tumor_or_normal,
        patient__cmo=patient__cmo,
        patient__dmp=patient__dmp,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        run_mode="NovaSeq X",
        sample_origin=["FFPE"],
        preservation=["FFPE"],
    )
    if sample_type == "DMP":
        kwargs["preservation"] = None
        kwargs["sample_origin"] = None
        kwargs["run_mode"] = None
        kwargs["bait_set"] = None
    if additional_metadata is not None:
        kwargs["additional_metadata"] = additional_metadata
    sample = Sample(**kwargs)
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _latest_rows(db: Session, logic: PairingLogic, tumor: Sample) -> list[PairingTable]:
    """The tumor's latest pairing rows under ``logic`` (one per matched
    normal for the multi_pair DMP logic)."""
    return list(
        db.scalars(
            select(PairingTable).where(
                PairingTable.tumor_id == tumor.id,
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest == True,  # noqa: E712
            )
        ).all()
    )


def _all_rows(db: Session, logic: PairingLogic, tumor: Sample) -> list[PairingTable]:
    return list(
        db.scalars(
            select(PairingTable).where(
                PairingTable.tumor_id == tumor.id,
                PairingTable.pairing_logic_id == logic.id,
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_dmp_strategy_registered():
    assert "dmp" in strategies
    dmp = strategies["dmp"]
    assert isinstance(dmp, DmpPairingStrategy)
    # Two-tier reference keys (spec g); gene_panel is NO LONGER a key.
    assert dmp.HARD_KEYS == ()
    assert dmp.HAS_POOLED_TIER is False
    # Fan-out: one pairing row per match, no winner.
    assert dmp.MULTI_PAIR is True


def test_dmp_logic_row_routes_to_dmp_strategy(dmp_logic):
    # Generic dispatch: a PairingLogic row with strategy_name "dmp" resolves
    # to the DMP strategy via the shared registry (no service special-casing).
    assert get_pairing_strategy(dmp_logic) is strategies["dmp"]
    # Unknown strategy names still raise (dispatch is not silently permissive).
    with pytest.raises(Exception):
        get_pairing_strategy(SimpleNamespace(strategy_name="nope", config=None))


def test_single_winner_strategies_are_not_multi_pair():
    for name in ("default", "argos", "tempo"):
        assert strategies[name].MULTI_PAIR is False


# ---------------------------------------------------------------------------
# Tier 1 (pool) matching
# ---------------------------------------------------------------------------


def test_match_same_patient_and_pool(db, dmp_logic):
    # Clean single pair: same patient + same pool (project_name fallback).
    tumor = _sample(
        db, "P-1001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1001", additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-1001-N01-IM6", patient__dmp="P-1001",
        additional_metadata={"project_name": "POOLA"},
    )
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is True
    assert dmp.mismatch_reasons(tumor, normal, db) == []
    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
    assert pairing.multi_pair is True
    rows = _latest_rows(db, dmp_logic, tumor)
    assert [r.normal_id for r in rows] == [normal.id]
    # No-winner note from the cascade.
    assert any(n.startswith("tier=dmp") for n in (pairing.notes or []))


def test_pool_id_metadata_wins_over_project_name(db, dmp_logic):
    # Explicit pool_id metadata wins over project_name (spec g).
    tumor = _sample(
        db, "P-1002-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1002",
        additional_metadata={"pool_id": "POOLID", "project_name": "PROJECT"},
    )
    normal = _sample(
        db, "P-1002-N01-IM6", patient__dmp="P-1002",
        additional_metadata={"pool_id": "POOLID"},
    )
    assert strategies["dmp"].matches(tumor, normal, db) is True


def test_keys_are_case_sensitive(db, dmp_logic):
    # The reference joins are exact/case-sensitive (dplyr) — the former
    # case-insensitive DMP matching is retired.
    tumor = _sample(
        db, "P-1003-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1003", additional_metadata={"project_name": "POOLA"},
    )
    # Lower-cased patient: no match.
    n_lower_patient = _sample(
        db, "P-1003-N01-IM6", patient__dmp="p-1003",
        additional_metadata={"project_name": "POOLA"},
    )
    # Lower-cased pool: no tier-1 match (and no shared IM_ver either).
    n_lower_pool = _sample(
        db, "P-1003-N02-IM9", patient__dmp="P-1003",
        additional_metadata={"project_name": "poola"},
    )
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, n_lower_patient, db) is False
    assert dmp.matches(tumor, n_lower_pool, db) is False
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_gene_panel_is_not_a_pairing_key(db, dmp_logic):
    # The reference joins do not use gene_panel: one P- patient may carry
    # both IMPACT and ACCESS DMP samples in the same pool.
    tumor = _sample(
        db, "P-1004-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1004", gene_panel="IMPACT341",
        additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-1004-N01-IM6", patient__dmp="P-1004", gene_panel="ACCESS",
        additional_metadata={"project_name": "POOLA"},
    )
    assert strategies["dmp"].matches(tumor, normal, db) is True


def test_no_match_different_patient(db, dmp_logic):
    tumor = _sample(
        db, "P-1005-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1005", additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-1006-N01-IM6", patient__dmp="P-1006",
        additional_metadata={"project_name": "POOLA"},
    )
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is False
    assert "patient__dmp mismatch (P-1005 vs P-1006)" in dmp.mismatch_reasons(
        tumor, normal, db
    )
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_no_match_missing_patient(db, dmp_logic):
    # No stored patient__dmp AND no P- id in the name: patient not
    # resolvable, the strategy is patient-scoped.
    tumor = _sample(
        db, "DMP-T1", tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp=None,
        additional_metadata={"project_name": "POOLA"},
    )
    _sample(
        db, "DMP-N1", patient__dmp="P-1",
        additional_metadata={"project_name": "POOLA"},
    )
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is None
    assert any("patient__dmp missing" in note for note in sel.notes)
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_patient_fallback_name_parse(db, dmp_logic):
    # Stored patient__dmp is NULL on both sides: the reference's name
    # parse (P- id in the sample name) is the patient fallback (spec g).
    tumor = _sample(
        db, "P-1007-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp=None, additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-1007-N01-IM6", patient__dmp=None,
        additional_metadata={"project_name": "POOLA"},
    )
    assert strategies["dmp"].matches(tumor, normal, db) is True
    # A different P- id in the name: still no match.
    other = _sample(
        db, "P-9999-N01-IM6", patient__dmp=None,
        additional_metadata={"project_name": "POOLA"},
    )
    assert strategies["dmp"].matches(tumor, other, db) is False


# ---------------------------------------------------------------------------
# Tier 2 (IM_ver rescue)
# ---------------------------------------------------------------------------


def test_rescue_only_different_pool_same_im_ver(db, dmp_logic):
    # No same-pool normal: the IM_ver rescue picks the other-pool normal
    # with the same IM_ver, and only it.
    tumor = _sample(
        db, "P-2001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2001", additional_metadata={"project_name": "POOLA"},
    )
    rescued = _sample(
        db, "P-2001-N01-IM6", patient__dmp="P-2001",
        additional_metadata={"project_name": "POOLB"},
    )
    other_ver = _sample(
        db, "P-2001-N02-IM7", patient__dmp="P-2001",
        additional_metadata={"project_name": "POOLB"},
    )
    dmp = strategies["dmp"]
    assert dmp.mismatch_reasons(tumor, rescued, db) == []
    assert dmp.mismatch_reasons(tumor, other_ver, db) != []
    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    rows = _latest_rows(db, dmp_logic, tumor)
    assert [r.normal_id for r in rows] == [rescued.id]


def test_pool_beats_im_ver_no_rescue_row(db, dmp_logic):
    # A same-pool normal (different IM_ver) wins tier 1: the other-pool
    # normal with the tumor's IM_ver gets NO rescue row.
    tumor = _sample(
        db, "P-2002-T01-IM7", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2002", additional_metadata={"project_name": "POOLA"},
    )
    same_pool = _sample(
        db, "P-2002-N01-IM8", patient__dmp="P-2002",
        additional_metadata={"project_name": "POOLA"},
    )
    same_ver_other_pool = _sample(
        db, "P-2002-N02-IM7", patient__dmp="P-2002",
        additional_metadata={"project_name": "POOLB"},
    )
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, same_pool, db) is True
    # The rescue is not applicable: the tumor has a tier-1 normal.
    assert dmp.matches(tumor, same_ver_other_pool, db) is False
    reasons = dmp.mismatch_reasons(tumor, same_ver_other_pool, db)
    assert any("rescue not applicable" in r for r in reasons)
    trigger_pairing(db, tumor, dmp_logic, "automatic")
    rows = _latest_rows(db, dmp_logic, tumor)
    assert [r.normal_id for r in rows] == [same_pool.id]


def test_im_ver_unparseable_on_tumor_no_tier2(db, dmp_logic):
    # Tumor name without a third dash-segment: no IM_ver, and no pool
    # match — the rescue needs a ver, so nothing pairs.
    tumor = _sample(
        db, "P-2003-T01", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2003",
    )
    normal = _sample(
        db, "P-2003-N01-IM6", patient__dmp="P-2003",
        additional_metadata={"project_name": "POOLB"},
    )
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is None
    assert any("no IM_ver" in note for note in sel.notes)
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_missing_pool_id_both_sides_tier1_impossible(db, dmp_logic):
    # No pool_id on either side: tier 1 is impossible (NA keys never
    # match); the IM_ver rescue still works.
    tumor = _sample(
        db, "P-2004-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2004",
    )
    rescued = _sample(
        db, "P-2004-N01-IM6", patient__dmp="P-2004",
    )
    dmp = strategies["dmp"]
    # No pool on either side: not a tier-1 match, but the rescue applies.
    assert dmp.mismatch_reasons(tumor, rescued, db) == []
    trigger_pairing(db, tumor, dmp_logic, "automatic")
    rows = _latest_rows(db, dmp_logic, tumor)
    assert [r.normal_id for r in rows] == [rescued.id]

    # Different IM_ver with no pool anywhere: nothing pairs.
    tumor2 = _sample(
        db, "P-2005-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2005",
    )
    no_ver_match = _sample(
        db, "P-2005-N01-IM7", patient__dmp="P-2005",
    )
    assert dmp.mismatch_reasons(tumor2, no_ver_match, db) != []
    assert trigger_pairing(db, tumor2, dmp_logic, "automatic") is None


def test_normal_without_pool_id_is_tier2_target(db, dmp_logic):
    # A normal with only a ver (no pool_id) can still be a tier-2 target.
    tumor = _sample(
        db, "P-2006-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-2006", additional_metadata={"project_name": "POOLA"},
    )
    no_pool_normal = _sample(
        db, "P-2006-N01-IM6", patient__dmp="P-2006",
    )
    assert strategies["dmp"].matches(tumor, no_pool_normal, db) is True


# ---------------------------------------------------------------------------
# No winner: fan-out rows
# ---------------------------------------------------------------------------


def test_ambiguous_two_same_pool_normals_two_rows(db, dmp_logic):
    # Two same-pool normals: the tumor appears on TWO rows — no winner.
    tumor = _sample(
        db, "P-3001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-3001", additional_metadata={"project_name": "POOLA"},
    )
    n_a = _sample(
        db, "P-3001-N01-IM6", patient__dmp="P-3001",
        additional_metadata={"project_name": "POOLA"},
    )
    n_b = _sample(
        db, "P-3001-N02-IM7", patient__dmp="P-3001",
        additional_metadata={"project_name": "POOLA"},
    )
    dmp = strategies["dmp"]
    assert {n.id for n in dmp.select_matching_normals(db, tumor)} == {
        n_a.id, n_b.id
    }
    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    rows = _latest_rows(db, dmp_logic, tumor)
    assert {r.normal_id for r in rows} == {n_a.id, n_b.id}
    assert all(r.latest for r in rows)
    assert all(r.multi_pair for r in rows)
    # The returned row is the deterministic representative (first in
    # primary_id order), but it is NOT the sole pairing.
    assert pairing.normal_id == n_a.id


def test_normal_shared_by_two_same_pool_tumors(db, dmp_logic):
    # One normal, two same-pool tumors: the normal appears in TWO pairs
    # (the reference left_join fan-out on the normal side).
    normal = _sample(
        db, "P-3002-N01-IM6", patient__dmp="P-3002",
        additional_metadata={"project_name": "POOLA"},
    )
    t1 = _sample(
        db, "P-3002-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-3002", additional_metadata={"project_name": "POOLA"},
    )
    t2 = _sample(
        db, "P-3002-T02-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-3002", additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing(db, t1, dmp_logic, "automatic")
    trigger_pairing(db, t2, dmp_logic, "automatic")
    rows_t1 = _latest_rows(db, dmp_logic, t1)
    rows_t2 = _latest_rows(db, dmp_logic, t2)
    assert [r.normal_id for r in rows_t1] == [normal.id]
    assert [r.normal_id for r in rows_t2] == [normal.id]
    # Both rows live in the table for the same normal.
    normal_rows = db.scalars(
        select(PairingTable).where(
            PairingTable.normal_id == normal.id,
            PairingTable.pairing_logic_id == dmp_logic.id,
            PairingTable.latest == True,  # noqa: E712
        )
    ).all()
    assert {r.tumor_id for r in normal_rows} == {t1.id, t2.id}


def test_repair_replaces_row_set_without_duplicates(db, dmp_logic):
    # Re-running the cascade with a changed match set replaces the rows
    # cleanly: old rows are retired, no duplicates.
    tumor = _sample(
        db, "P-3003-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-3003", additional_metadata={"project_name": "POOLA"},
    )
    n_a = _sample(
        db, "P-3003-N01-IM6", patient__dmp="P-3003",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing(db, tumor, dmp_logic, "manual")
    assert len(_latest_rows(db, dmp_logic, tumor)) == 1
    # A second same-pool normal arrives; re-trigger.
    n_b = _sample(
        db, "P-3003-N02-IM6", patient__dmp="P-3003",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing(db, tumor, dmp_logic, "manual")
    rows = _latest_rows(db, dmp_logic, tumor)
    assert {r.normal_id for r in rows} == {n_a.id, n_b.id}
    all_rows = _all_rows(db, dmp_logic, tumor)
    # The first row was retired, not duplicated: exactly 3 rows total
    # (1 retired + 2 latest).
    assert len(all_rows) == 3
    # Unchanged match set: a re-trigger is a no-op (no new rows).
    before = len(_all_rows(db, dmp_logic, tumor))
    trigger_pairing(db, tumor, dmp_logic, "manual")
    assert len(_all_rows(db, dmp_logic, tumor)) == before


# ---------------------------------------------------------------------------
# Group scoping (unchanged: DMP-group only)
# ---------------------------------------------------------------------------


def test_lims_candidate_rejected(db, dmp_logic):
    tumor = _sample(
        db, "P-4001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-4001", additional_metadata={"project_name": "POOLA"},
    )
    lims_normal = _sample(
        db, "LIMS-N1", sample_type="tissue", patient__cmo="C-9",
    )
    dmp = strategies["dmp"]
    assert dmp.mismatch_reasons(tumor, lims_normal) == ["not a DMP sample"]
    assert dmp.matches(tumor, lims_normal) is False
    assert dmp.candidate_scope(db, tumor) == []


def test_pooled_normal_rejected(db, dmp_logic):
    tumor = _sample(
        db, "P-4002-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-4002", additional_metadata={"project_name": "POOLA"},
    )
    pooled = _sample(db, "POOL-N1", sample_type="Pooled Normal")
    dmp = strategies["dmp"]
    assert dmp.mismatch_reasons(tumor, pooled) == ["not a DMP sample"]
    scope = dmp.candidate_scope(db, tumor)
    assert all(s.sample_type != "Pooled Normal" for s in scope)


def test_non_dmp_tumor_rejected(db, dmp_logic):
    tumor = _sample(
        db, "LIMS-T1", sample_type="tissue",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
    )
    _sample(
        db, "P-4003-N01-IM6", patient__dmp="P-4003",
        additional_metadata={"project_name": "POOLA"},
    )
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is None
    assert any("not a DMP sample" in note for note in sel.notes)
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


# ---------------------------------------------------------------------------
# Normal-side backfill (mirror of the tumor-driven reference)
# ---------------------------------------------------------------------------


def test_normal_side_auto_pairing(db, dmp_logic):
    # Normal arrives second: the generic normal-side dispatch pairs it back
    # to its unpaired DMP tumor through the same two-tier strategy.
    tumor = _sample(
        db, "P-5001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-5001", additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-5001-N01-IM6", patient__dmp="P-5001",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing_for_normal(db, normal, dmp_logic)
    rows = _latest_rows(db, dmp_logic, tumor)
    assert [r.normal_id for r in rows] == [normal.id]


def test_normal_side_backfill_fan_out_and_idempotence(db, dmp_logic):
    # Backfill produces the SAME row set as the tumor-driven pass: a
    # second same-pool normal re-ingested updates the tumor's row set
    # (3 rows), and re-ingesting the same normal is a no-op (no
    # duplicates).
    tumor = _sample(
        db, "P-5002-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-5002", additional_metadata={"project_name": "POOLA"},
    )
    n_a = _sample(
        db, "P-5002-N01-IM6", patient__dmp="P-5002",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing_for_normal(db, n_a, dmp_logic)
    assert [r.normal_id for r in _latest_rows(db, dmp_logic, tumor)] == [n_a.id]

    # A second same-pool normal arrives (re)ingested.
    n_b = _sample(
        db, "P-5002-N02-IM6", patient__dmp="P-5002",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing_for_normal(db, n_b, dmp_logic)
    assert {r.normal_id for r in _latest_rows(db, dmp_logic, tumor)} == {
        n_a.id, n_b.id
    }

    # Row set now equals what the tumor-driven pass would produce.
    assert {
        n.id for n in strategies["dmp"].select_matching_normals(db, tumor)
    } == {n_a.id, n_b.id}

    # Re-ingest of the same normal: idempotent, no duplicates.
    before = len(_all_rows(db, dmp_logic, tumor))
    trigger_pairing_for_normal(db, n_b, dmp_logic)
    assert len(_all_rows(db, dmp_logic, tumor)) == before
    assert len(_latest_rows(db, dmp_logic, tumor)) == 2


def test_normal_side_backfill_does_not_add_non_matching_tumors(db, dmp_logic):
    # A normal whose patient has tumors in a different pool with their own
    # tier-1 normals must not add a rescue row.
    t1 = _sample(
        db, "P-5003-T01-IM9", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-5003", additional_metadata={"project_name": "POOLA"},
    )
    n_poola = _sample(
        db, "P-5003-N01-IM9", patient__dmp="P-5003",
        additional_metadata={"project_name": "POOLA"},
    )
    # The arriving normal: same patient, same IM_ver as t1, but another
    # pool — and t1 already has a tier-1 normal.
    n_poolb = _sample(
        db, "P-5003-N02-IM9", patient__dmp="P-5003",
        additional_metadata={"project_name": "POOLB"},
    )
    trigger_pairing_for_normal(db, n_poola, dmp_logic)
    trigger_pairing_for_normal(db, n_poolb, dmp_logic)
    rows = _latest_rows(db, dmp_logic, t1)
    assert [r.normal_id for r in rows] == [n_poola.id]


# ---------------------------------------------------------------------------
# possible_matches: tier keying + fan-out markers
# ---------------------------------------------------------------------------


def _endpoints(db: Session, sample: Sample, logic_name: str = "DMP"):
    return possible_matches_endpoint(
        sample_id=sample.id, pairing_logic_name=logic_name, db=db
    )


def test_dmp_logic_possible_marks_all_row_set_candidates(db, dmp_logic):
    # No winner: EVERY candidate in the tumor's two-tier row set carries
    # would_be_selected.
    tumor = _sample(
        db, "P-6001-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-6001", additional_metadata={"project_name": "POOLA"},
    )
    n_a = _sample(
        db, "P-6001-N01-IM6", patient__dmp="P-6001",
        additional_metadata={"project_name": "POOLA"},
    )
    n_b = _sample(
        db, "P-6001-N02-IM7", patient__dmp="P-6001",
        additional_metadata={"project_name": "POOLA"},
    )
    _sample(
        db, "P-6001-N03-IM8", patient__dmp="P-6001",
        additional_metadata={"project_name": "POOLB"},
    )
    result = _endpoints(db, tumor, "DMP")
    assert result["pairing_logic"] == "DMP"
    assert result["current_match"] is None
    by_id = {c["sample"].primary_id: c for c in result["other_matches"]}
    assert set(by_id) == {"P-6001-N01-IM6", "P-6001-N02-IM7", "P-6001-N03-IM8"}
    for cand in by_id.values():
        assert cand["tier"] == "dmp"
    # The two same-pool normals are the row set; both flagged.
    assert by_id["P-6001-N01-IM6"]["matched"] is True
    assert by_id["P-6001-N02-IM7"]["matched"] is True
    assert by_id["P-6001-N01-IM6"]["would_be_selected"] is True
    assert by_id["P-6001-N02-IM7"]["would_be_selected"] is True
    # The other-pool normal with a different IM_ver: no tier, no rescue.
    assert by_id["P-6001-N03-IM8"]["matched"] is False
    assert by_id["P-6001-N03-IM8"]["would_be_selected"] is False


def test_dmp_logic_possible_current_matches_fan_out(db, dmp_logic):
    # After pairing, a fanned-out tumor lists EVERY current row.
    tumor = _sample(
        db, "P-6002-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-6002", additional_metadata={"project_name": "POOLA"},
    )
    n_a = _sample(
        db, "P-6002-N01-IM6", patient__dmp="P-6002",
        additional_metadata={"project_name": "POOLA"},
    )
    n_b = _sample(
        db, "P-6002-N02-IM6", patient__dmp="P-6002",
        additional_metadata={"project_name": "POOLA"},
    )
    trigger_pairing(db, tumor, dmp_logic, "automatic")
    result = _endpoints(db, tumor, "DMP")
    assert len(result["current_matches"]) == 2
    current_ids = {m["sample"].primary_id for m in result["current_matches"]}
    assert current_ids == {"P-6002-N01-IM6", "P-6002-N02-IM6"}
    # Paired candidates drop out of other_matches.
    assert result["other_matches"] == []


def test_dmp_logic_possible_matches_normal_side(db, dmp_logic):
    tumor = _sample(
        db, "P-6003-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-6003", additional_metadata={"project_name": "POOLA"},
    )
    normal = _sample(
        db, "P-6003-N01-IM6", patient__dmp="P-6003",
        additional_metadata={"project_name": "POOLA"},
    )
    result = _endpoints(db, normal, "DMP")
    assert result["current_match"] is None
    by_id = {c["sample"].primary_id: c for c in result["other_matches"]}
    assert set(by_id) == {"P-6003-T01-IM6"}
    assert by_id["P-6003-T01-IM6"]["tier"] == "dmp"
    assert by_id["P-6003-T01-IM6"]["matched"] is True


def test_dmp_logic_possible_matches_excludes_lims_and_pooled(db, dmp_logic):
    tumor = _sample(
        db, "P-6004-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-6004", patient__cmo="C-1",
        additional_metadata={"project_name": "POOLA"},
    )
    _sample(db, "LIMS-N1", sample_type="tissue", patient__cmo="C-1")  # other group
    _sample(db, "POOL-N1", sample_type="Pooled Normal")
    result = _endpoints(db, tumor, "DMP")
    assert result["other_matches"] == []


def test_argos_dmp_tier_keys_on_patient_dmp(db, argos_logic):
    # New keying: a DMP sample with a P- id lists opposite-type samples by
    # P- id even when the CMO differs...
    t1 = _sample(db, "P-6005-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
                 sample_type="DMP", patient__dmp="P-6005", patient__cmo="C-1")
    n1 = _sample(db, "P-6005-N01-IM6", sample_type="DMP",
                 patient__dmp="P-6005", patient__cmo="C-2")
    # ...but NOT by CMO when the P- id differs.
    _sample(db, "P-6006-N01-IM6", sample_type="DMP",
            patient__dmp="P-6006", patient__cmo="C-1")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"P-6005-N01-IM6"}
    cand = result["other_matches"][0]
    assert cand["tier"] == "dmp"
    assert cand["matched"] is False
    assert any("DMP: manual pairing only" in r for r in cand["mismatched_keys"])


def test_argos_dmp_tier_falls_back_to_cmo_for_legacy(db, argos_logic):
    # Legacy DMP rows without a P- id keep the CMO keying.
    t1 = _sample(db, "DMP-T1", tumor_or_normal=TumorOrNormal.TUMOR.value,
                 sample_type="DMP", patient__dmp=None, patient__cmo="C-1")
    n1 = _sample(db, "DMP-N1", sample_type="DMP",
                 patient__dmp=None, patient__cmo="C-1")
    _sample(db, "DMP-N2", sample_type="DMP",
            patient__dmp="P-6007", patient__cmo="C-2")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"DMP-N1"}


def test_lims_tumor_dmp_tier_falls_back_to_cmo(db, argos_logic):
    # LIMS samples never carry a P- id: their DMP tier keys on CMO and still
    # lists DMP candidates that do carry one.
    t1 = _sample(db, "LIMS-T1", sample_type="tissue",
                 tumor_or_normal=TumorOrNormal.TUMOR.value, patient__cmo="C-1")
    d1 = _sample(db, "P-6008-N01-IM6", sample_type="DMP",
                 patient__dmp="P-6008", patient__cmo="C-1")
    _sample(db, "P-6009-N01-IM6", sample_type="DMP",
            patient__dmp="P-6009", patient__cmo="C-2")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"P-6008-N01-IM6"}


def test_argos_keeps_dmp_manual_reason(db, argos_logic):
    # Guard: the argos strategy is unchanged — any pair involving a DMP
    # sample is manual-pairing only, even when the dmp keys line up.
    tumor = _sample(db, "P-6010-T01-IM6", tumor_or_normal=TumorOrNormal.TUMOR.value,
                    sample_type="DMP", patient__dmp="P-6010", patient__cmo="C-1",
                    additional_metadata={"project_name": "POOLA"})
    normal = _sample(db, "P-6010-N01-IM6", sample_type="DMP",
                     patient__dmp="P-6010", patient__cmo="C-1",
                     additional_metadata={"project_name": "POOLA"})
    assert strategies["argos"].mismatch_reasons(tumor, normal) == [
        "DMP: manual pairing only"
    ]


# ---------------------------------------------------------------------------
# App startup seeding
# ---------------------------------------------------------------------------


def test_lifespan_seeds_dmp_logic(tmp_path, monkeypatch):
    import main as main_module

    test_engine = create_engine(f"sqlite:///{tmp_path / 'lifespan.db'}")
    test_session_factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(main_module, "SessionLocal", test_session_factory)

    cm = main_module.lifespan(main_module.app)
    asyncio.run(cm.__aenter__())
    try:
        db = test_session_factory()
        rows = {
            r.name: r.strategy_name
            for r in db.scalars(select(PairingLogic)).all()
        }
        assert rows["DMP"] == "dmp"
        # Pre-existing seeds untouched.
        assert rows["ARGOS"] == "argos"
        assert rows["TEMPO"] == "tempo"
        assert rows["ACCESS"] == "default"
    finally:
        asyncio.run(cm.__aexit__(None, None, None))
        test_engine.dispose()
