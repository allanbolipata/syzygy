"""Unit tests for the DMP pairing strategy (``strategies["dmp"]``).

DMP 3/4: automatic DMP pairing is strictly DMP-group only and keys on
``patient__dmp`` (the "P-" id) with ``gene_panel`` as a second hard key.
LIMS / pooled-normal / cross-group candidates never match here — they stay
manual under the argos strategy.
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
) -> Sample:
    """Build a sample (DMP by default — the group under test); DMP rows
    arrive without LIMS metadata."""
    kwargs = dict(
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
    sample = Sample(**kwargs)
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_dmp_strategy_registered():
    assert "dmp" in strategies
    dmp = strategies["dmp"]
    assert isinstance(dmp, DmpPairingStrategy)
    assert dmp.HARD_KEYS == ("patient__dmp", "gene_panel")
    assert dmp.HAS_POOLED_TIER is False


def test_dmp_logic_row_routes_to_dmp_strategy(dmp_logic):
    # Generic dispatch: a PairingLogic row with strategy_name "dmp" resolves
    # to the DMP strategy via the shared registry (no service special-casing).
    assert get_pairing_strategy(dmp_logic) is strategies["dmp"]
    # Unknown strategy names still raise (dispatch is not silently permissive).
    with pytest.raises(Exception):
        get_pairing_strategy(SimpleNamespace(strategy_name="nope", config=None))


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------


def test_match_same_patient_dmp_and_panel(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    normal = _sample(db, "DMP-N1", patient__dmp="P-1")
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is True
    assert dmp.mismatch_reasons(tumor, normal, db) == []
    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
    # Deterministic winner note from the cascade tier.
    assert any(n.startswith("tier=dmp") for n in (pairing.notes or []))


def test_keys_are_case_insensitive(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1",
                    gene_panel="IMPACT468")
    normal = _sample(db, "DMP-N1", patient__dmp="p-1", gene_panel="impact468")
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is True


def test_no_match_different_patient_dmp(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    normal = _sample(db, "DMP-N1", patient__dmp="P-2")
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is False
    assert "patient__dmp mismatch (P-1 vs P-2)" in dmp.mismatch_reasons(
        tumor, normal, db
    )
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_no_match_missing_patient_dmp(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp=None)
    normal = _sample(db, "DMP-N1", patient__dmp="P-1")
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is False
    assert any(
        r.startswith("patient__dmp mismatch") for r in dmp.mismatch_reasons(tumor, normal, db)
    )
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is None
    assert any("patient__dmp missing" in note for note in sel.notes)
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_no_match_different_gene_panel(db, dmp_logic):
    # One P- patient can carry both IMPACT and ACCESS DMP samples; cross-assay
    # pairing is invalid, so gene_panel is a hard key.
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1",
                    gene_panel="IMPACT341")
    normal = _sample(db, "DMP-N1", patient__dmp="P-1", gene_panel="ACCESS")
    dmp = strategies["dmp"]
    assert dmp.matches(tumor, normal, db) is False
    assert "gene_panel mismatch (IMPACT341 vs ACCESS)" in dmp.mismatch_reasons(
        tumor, normal, db
    )
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_lims_candidate_rejected(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    lims_normal = _sample(db, "LIMS-N1", sample_type="tissue", patient__cmo="C-9")
    dmp = strategies["dmp"]
    assert dmp.mismatch_reasons(tumor, lims_normal) == ["not a DMP sample"]
    assert dmp.matches(tumor, lims_normal) is False
    assert dmp.candidate_scope(db, tumor) == []


def test_pooled_normal_rejected(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    pooled = _sample(db, "POOL-N1", sample_type="Pooled Normal")
    dmp = strategies["dmp"]
    assert dmp.mismatch_reasons(tumor, pooled) == ["not a DMP sample"]
    scope = dmp.candidate_scope(db, tumor)
    assert all(s.sample_type != "Pooled Normal" for s in scope)


def test_non_dmp_tumor_rejected(db, dmp_logic):
    tumor = _sample(db, "LIMS-T1", sample_type="tissue", tumor_or_normal=TumorOrNormal.TUMOR.value)
    _sample(db, "DMP-N1", patient__dmp="P-1")
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is None
    assert any("not a DMP sample" in note for note in sel.notes)
    assert trigger_pairing(db, tumor, dmp_logic, "automatic") is None


def test_normal_side_auto_pairing(db, dmp_logic):
    # Normal arrives second: the generic normal-side dispatch pairs it back
    # to its unpaired DMP tumor through the same strategy.
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    normal = _sample(db, "DMP-N1", patient__dmp="P-1")
    trigger_pairing_for_normal(db, normal, dmp_logic)
    pairing = db.scalar(
        select(PairingTable).where(PairingTable.tumor_id == tumor.id)
    )
    assert pairing is not None
    assert pairing.normal_id == normal.id


# ---------------------------------------------------------------------------
# Deterministic candidate selection
# ---------------------------------------------------------------------------


def test_two_candidate_normals_exactly_one_picked(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    n_b = _sample(db, "DMP-N-B", patient__dmp="P-1")
    n_a = _sample(db, "DMP-N-A", patient__dmp="P-1")
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    assert sel.normal is not None
    assert sel.normal.primary_id == "DMP-N-A"  # min by primary_id
    assert any("2 candidate(s)" in note for note in sel.notes)
    pairing = trigger_pairing(db, tumor, dmp_logic, "automatic")
    assert pairing is not None
    assert pairing.normal_id == n_a.id
    # Exactly one pairing row for the tumor.
    rows = db.scalars(
        select(PairingTable).where(PairingTable.tumor_id == tumor.id)
    ).all()
    assert len(rows) == 1


def test_winner_is_lexicographic_min_not_numeric(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    _sample(db, "DMP-N2", patient__dmp="P-1")
    n10 = _sample(db, "DMP-N10", patient__dmp="P-1")
    dmp = strategies["dmp"]
    sel = dmp.select_latest_normal(db, tumor)
    # "DMP-N10" < "DMP-N2" in string order — same mechanism as argos.
    assert sel.normal is not None
    assert sel.normal.primary_id == "DMP-N10"
    assert n10.id is not None


# ---------------------------------------------------------------------------
# possible_matches: tier keying
# ---------------------------------------------------------------------------


def _endpoints(db: Session, sample: Sample, logic_name: str = "DMP"):
    return possible_matches_endpoint(
        sample_id=sample.id, pairing_logic_name=logic_name, db=db
    )


def test_dmp_logic_possible_matches_marks_winner(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    n_b = _sample(db, "DMP-N-B", patient__dmp="P-1")
    n_a = _sample(db, "DMP-N-A", patient__dmp="P-1")
    result = _endpoints(db, tumor, "DMP")
    assert result["pairing_logic"] == "DMP"
    assert result["current_match"] is None
    by_id = {c["sample"].primary_id: c for c in result["other_matches"]}
    assert set(by_id) == {"DMP-N-A", "DMP-N-B"}
    for cand in by_id.values():
        assert cand["tier"] == "dmp"
        assert cand["matched"] is True
    # Exactly one candidate is flagged as the deterministic winner.
    assert by_id["DMP-N-A"]["would_be_selected"] is True
    assert by_id["DMP-N-B"]["would_be_selected"] is False


def test_dmp_logic_possible_matches_excludes_lims_and_pooled(db, dmp_logic):
    tumor = _sample(
        db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient__dmp="P-1", patient__cmo="C-1",
    )
    _sample(db, "LIMS-N1", sample_type="tissue", patient__cmo="C-1")  # same patient, other group
    _sample(db, "POOL-N1", sample_type="Pooled Normal")
    result = _endpoints(db, tumor, "DMP")
    assert result["other_matches"] == []


def test_dmp_logic_possible_matches_normal_side(db, dmp_logic):
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, patient__dmp="P-1")
    normal = _sample(db, "DMP-N1", patient__dmp="P-1")
    result = _endpoints(db, normal, "DMP")
    assert result["current_match"] is None
    by_id = {c["sample"].primary_id: c for c in result["other_matches"]}
    assert set(by_id) == {"DMP-T1"}
    assert by_id["DMP-T1"]["tier"] == "dmp"
    assert by_id["DMP-T1"]["matched"] is True


def test_argos_dmp_tier_keys_on_patient_dmp(db, argos_logic):
    # New keying: a DMP sample with a P- id lists opposite-type samples by
    # P- id even when the CMO differs...
    t1 = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, sample_type="DMP",
                 patient__dmp="P-1", patient__cmo="C-1")
    n1 = _sample(db, "DMP-N1", sample_type="DMP",
                 patient__dmp="P-1", patient__cmo="C-2")
    # ...but NOT by CMO when the P- id differs.
    _sample(db, "DMP-N2", sample_type="DMP",
            patient__dmp="P-2", patient__cmo="C-1")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"DMP-N1"}
    cand = result["other_matches"][0]
    assert cand["tier"] == "dmp"
    assert cand["matched"] is False
    assert any("DMP: manual pairing only" in r for r in cand["mismatched_keys"])


def test_argos_dmp_tier_falls_back_to_cmo_for_legacy(db, argos_logic):
    # Legacy DMP rows without a P- id keep the CMO keying.
    t1 = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, sample_type="DMP",
                 patient__dmp=None, patient__cmo="C-1")
    n1 = _sample(db, "DMP-N1", sample_type="DMP",
                 patient__dmp=None, patient__cmo="C-1")
    _sample(db, "DMP-N2", sample_type="DMP",
            patient__dmp="P-2", patient__cmo="C-2")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"DMP-N1"}


def test_lims_tumor_dmp_tier_falls_back_to_cmo(db, argos_logic):
    # LIMS samples never carry a P- id: their DMP tier keys on CMO and still
    # lists DMP candidates that do carry one.
    t1 = _sample(db, "LIMS-T1", sample_type="tissue", tumor_or_normal=TumorOrNormal.TUMOR.value,
                 patient__cmo="C-1")
    d1 = _sample(db, "DMP-N1", sample_type="DMP",
                 patient__dmp="P-1", patient__cmo="C-1")
    _sample(db, "DMP-N2", sample_type="DMP",
            patient__dmp="P-1", patient__cmo="C-2")
    result = _endpoints(db, t1, "ARGOS")
    other_ids = {c["sample"].primary_id for c in result["other_matches"]}
    assert other_ids == {"DMP-N1"}


def test_argos_keeps_dmp_manual_reason(db, argos_logic):
    # Guard: the argos strategy is unchanged — any pair involving a DMP
    # sample is manual-pairing only, even when the dmp keys line up.
    tumor = _sample(db, "DMP-T1",    tumor_or_normal=TumorOrNormal.TUMOR.value, sample_type="DMP",
                    patient__dmp="P-1", patient__cmo="C-1")
    normal = _sample(db, "DMP-N1", sample_type="DMP",
                     patient__dmp="P-1", patient__cmo="C-1")
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
