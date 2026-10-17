import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import database
import pairing_service
from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    _create_pairing,
    _get_latest_pairing,
    resolve_pairing_logic,
    trigger_pairing,
    trigger_pairing_for_normal,
)
from pairing_strategies import NormalSelection, get_pairing_strategy, register_pairing_strategy, strategies
from routers.pairing_logic import create_pairing_logic, list_pairing_logic
from routers.pairings import list_pairings, trigger_pairing_endpoint
from routers.samples import create_sample, list_samples
from schemas import PairingLogicCreate, PairingTriggerCreate, SampleCreate, TumorOrNormal


class CustomPairingStrategy:
    def matches(self, tumor: Sample, normal: Sample) -> bool:
        return tumor.patient_id == normal.patient_id and tumor.gene_panel == normal.gene_panel

    def mismatch_reasons(self, tumor: Sample, normal: Sample) -> list:
        reasons = []
        if tumor.patient_id != normal.patient_id:
            reasons.append(f"patient_id mismatch ({tumor.patient_id} vs {normal.patient_id})")
        if tumor.gene_panel != normal.gene_panel:
            reasons.append(f"gene_panel mismatch ({tumor.gene_panel} vs {normal.gene_panel})")
        return reasons

    def select_latest_normal(
        self, db: Session, tumor: Sample, logic: Optional[PairingLogic] = None
    ) -> NormalSelection:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
            Sample.patient_id == tumor.patient_id,
            Sample.gene_panel == tumor.gene_panel,
        )
        return NormalSelection(db.scalar(stmt))

    def select_matching_tumors(
        self, db: Session, normal: Sample, logic: Optional[PairingLogic] = None
    ) -> list:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
            Sample.patient_id == normal.patient_id,
            Sample.gene_panel == normal.gene_panel,
        )
        return db.scalars(stmt).all()

    def with_config(self, config: dict) -> "CustomPairingStrategy":
        return self


def _try_docker_engine():
    """Attempt to create a PostgreSQL engine via testcontainers. Returns None on failure."""
    try:
        from testcontainers.postgres import PostgresContainer
        pg = PostgresContainer("postgres:16")
        pg.start()
        engine = create_engine(
            pg.get_url(driver="psycopg2"),
            echo=False,
            pool_pre_ping=True,
        )
        Base.metadata.create_all(bind=engine)
        return engine, pg
    except Exception as e:
        print(f"  [WARNING] Docker/testcontainers unavailable ({e}); falling back to SQLite")
        return None, None


@pytest.fixture(scope="module")
def pg_engine():
    engine, pg = _try_docker_engine()
    if engine is not None:
        yield engine
        engine.dispose()
        pg.stop()
    else:
        # Single shared connection so multiple sessions (including the
        # cross-thread race tests) see the same in-memory database.
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        yield engine
        engine.dispose()


@pytest.fixture
def db(pg_engine):
    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with pg_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def pairing_logic(db):
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


@pytest.fixture
def tumor_sample(db):
    sample = Sample(
        primary_id="TUMOR-001",
        cmo_sample_name="Tumor Sample 1",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


@pytest.fixture
def normal_sample(db):
    sample = Sample(
        primary_id="NORMAL-001",
        cmo_sample_name="Normal Sample 1",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


@pytest.fixture
def older_normal_sample(db):
    sample = Sample(
        primary_id="NORMAL-002",
        cmo_sample_name="Normal Sample 2",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


@pytest.fixture
def non_matching_normal(db):
    sample = Sample(
        primary_id="NORMAL-003",
        cmo_sample_name="Normal Sample 3",
        patient_id="PATIENT-002",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


@pytest.fixture
def custom_strategy(monkeypatch):
    monkeypatch.setitem(strategies, "custom", CustomPairingStrategy())
    return strategies["custom"]


def test_resolve_pairing_logic_by_id(db, pairing_logic):
    logic = resolve_pairing_logic(db, pairing_logic.id)
    assert logic is not None
    assert logic.id == pairing_logic.id
    assert logic.name == "TEMPO"


def test_resolve_pairing_logic_by_name(db, pairing_logic):
    logic = resolve_pairing_logic(db, pairing_logic_name="TEMPO")
    assert logic is not None
    assert logic.id == pairing_logic.id
    assert logic.name == "TEMPO"


def test_resolve_pairing_logic_not_found_by_id(db):
    with pytest.raises(HTTPException) as exc_info:
        resolve_pairing_logic(db, pairing_logic_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


def test_resolve_pairing_logic_not_found_by_name(db):
    with pytest.raises(HTTPException) as exc_info:
        resolve_pairing_logic(db, pairing_logic_name="NONEXISTENT")
    assert exc_info.value.status_code == 404


def test_resolve_pairing_logic_none(db):
    logic = resolve_pairing_logic(db)
    assert logic is None


def test_matches_true(tumor_sample, normal_sample):
    assert strategies["default"].matches(tumor_sample, normal_sample) is True


def test_matches_false_different_patient(tumor_sample, non_matching_normal):
    assert strategies["default"].matches(tumor_sample, non_matching_normal) is False


def test_matches_false_different_gene_panel(db, tumor_sample):
    normal = Sample(
        primary_id="NORMAL-004",
        cmo_sample_name="Normal Sample 4",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-B",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    db.add(normal)
    db.commit()
    db.refresh(normal)
    assert strategies["default"].matches(tumor_sample, normal) is False


def test_matches_false_different_bait_set(db, tumor_sample):
    normal = Sample(
        primary_id="NORMAL-005",
        cmo_sample_name="Normal Sample 5",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-2",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    db.add(normal)
    db.commit()
    db.refresh(normal)
    assert strategies["default"].matches(tumor_sample, normal) is False

def test_select_latest_normal_returns_most_recent(db, tumor_sample, normal_sample, older_normal_sample):
    assert (
        strategies["default"].select_latest_normal(db, tumor_sample).normal.id
        == normal_sample.id
    )


def test_select_latest_normal_no_match(db, tumor_sample, non_matching_normal):
    assert strategies["default"].select_latest_normal(db, tumor_sample).normal is None


def test_select_matching_tumors_returns_matches(db, tumor_sample, normal_sample):
    result = strategies["default"].select_matching_tumors(db, normal_sample)
    assert len(result) == 1
    assert result[0].id == tumor_sample.id


def test_select_matching_tumors_no_match(db, normal_sample):
    non_matching = Sample(
        primary_id="NORMAL-006",
        cmo_sample_name="Normal Sample 6",
        patient_id="PATIENT-999",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    db.add(non_matching)
    db.commit()
    db.refresh(non_matching)
    result = strategies["default"].select_matching_tumors(db, non_matching)
    assert len(result) == 0


def test_get_latest_pairing_returns_pairing(db, tumor_sample, normal_sample, pairing_logic):
    pairing = _create_pairing(db, tumor_sample, normal_sample, pairing_logic)
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is not None
    assert result.id == pairing.id
    assert result.latest is True


def test_get_latest_pairing_none(db, tumor_sample, pairing_logic):
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is None


def test_trigger_pairing_automatic_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_automatic_does_not_replace_existing(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing(db, tumor_sample, pairing_logic, "automatic")

    newer_normal = Sample(
        primary_id="NORMAL-007",
        cmo_sample_name="Normal Sample 7",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.add(newer_normal)
    db.commit()
    db.refresh(newer_normal)

    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    assert result is not None
    assert result.normal_id == normal_sample.id


def test_trigger_pairing_manual_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    result = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_manual_replaces_existing(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    newer_normal = Sample(
        primary_id="NORMAL-008",
        cmo_sample_name="Normal Sample 8",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.add(newer_normal)
    db.commit()
    db.refresh(newer_normal)

    result = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    assert result is not None
    assert result.normal_id == newer_normal.id
    assert result.latest is True

    all_pairings = db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_sample.id,
            PairingTable.pairing_logic_id == pairing_logic.id,
        )
    ).all()
    assert len(all_pairings) == 2
    latest_pairings = [p for p in all_pairings if p.latest]
    assert len(latest_pairings) == 1
    assert latest_pairings[0].normal_id == newer_normal.id


def test_trigger_pairing_manual_no_duplicate(db, tumor_sample, normal_sample, pairing_logic):
    result1 = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    result2 = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    assert result1.id == result2.id


def test_trigger_pairing_override_with_normal_id(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    override_normal = Sample(
        primary_id="NORMAL-009",
        cmo_sample_name="Normal Sample 9",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.add(override_normal)
    db.commit()
    db.refresh(override_normal)

    result = trigger_pairing(db, tumor_sample, pairing_logic, "override", override_normal.id)
    assert result is not None
    assert result.normal_id == override_normal.id
    assert result.latest is True


def test_trigger_pairing_override_strict_normal_does_not_match(
    db, tumor_sample, non_matching_normal, pairing_logic
):
    """A strict override rejects a pinned normal with mismatched keys (400)."""
    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing(db, tumor_sample, pairing_logic, "override", non_matching_normal.id, strict=True)
    assert exc_info.value.status_code == 400
    assert "patient_id mismatch" in exc_info.value.detail


def test_trigger_pairing_override_force_records_mismatched_keys(
    db, tumor_sample, non_matching_normal, pairing_logic
):
    """A non-strict override forces the pair and records the mismatches."""
    result = trigger_pairing(db, tumor_sample, pairing_logic, "override", non_matching_normal.id)
    assert result is not None
    assert result.normal_id == non_matching_normal.id
    assert result.latest is True
    assert result.trigger_type == "override"
    assert any("patient_id mismatch" in key for key in (result.mismatched_keys or []))


def test_trigger_pairing_no_pairing_logic(db, tumor_sample, normal_sample):
    result = trigger_pairing(db, tumor_sample, None, "automatic")
    assert result is None


def test_trigger_pairing_no_matching_normal(db, tumor_sample, pairing_logic):
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    assert result is None


# ---------------------------------------------------------------------------
# Request-first cascade (default strategy)
# ---------------------------------------------------------------------------


def _sample(
    db,
    primary_id,
    patient_id="PATIENT-001",
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    request_id=None,
    run_mode=None,
    preservation=None,
    sample_origin=None,
    sample_type="tissue",
    gene_panel="PANEL-A",
    bait_set="BAIT-1",
    sequencing_date=None,
):
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
        preservation=preservation,
        sample_origin=sample_origin,
        sequencing_date=sequencing_date,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_default_prefers_same_request_normal(db):
    tumor = _sample(
        db, "TUMOR-REQ-1", tumor_or_normal=TumorOrNormal.TUMOR.value, request_id="REQ-1"
    )
    older_same = _sample(
        db, "NORM-REQ-1", request_id="REQ-1",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    newer_other = _sample(
        db, "NORM-REQ-2", request_id="REQ-2",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    selection = strategies["default"].select_latest_normal(db, tumor)
    assert selection.normal.id == older_same.id
    assert any("tier=same_request" in note for note in selection.notes)


def test_default_falls_back_to_other_requests(db):
    tumor = _sample(
        db, "TUMOR-REQ-2", tumor_or_normal=TumorOrNormal.TUMOR.value, request_id="REQ-1"
    )
    other = _sample(db, "NORM-REQ-3", request_id="REQ-3")
    selection = strategies["default"].select_latest_normal(db, tumor)
    assert selection.normal.id == other.id
    assert any("tier=other_requests" in note for note in selection.notes)


def test_default_patient_wide_when_tumor_has_no_request(db, tumor_sample, normal_sample):
    selection = strategies["default"].select_latest_normal(db, tumor_sample)
    assert selection.normal.id == normal_sample.id
    assert any("tier=patient-wide" in note for note in selection.notes)


def test_default_cascade_notes_record_empty_tiers(db):
    tumor = _sample(
        db, "TUMOR-REQ-4", tumor_or_normal=TumorOrNormal.TUMOR.value, request_id="REQ-9"
    )
    # A blood-type normal under REQ-9 is not a candidate (base-key filter).
    _sample(db, "NORM-REQ-4A", request_id="REQ-9", sample_type="blood")
    other = _sample(db, "NORM-REQ-4B", request_id="REQ-8")
    selection = strategies["default"].select_latest_normal(db, tumor)
    assert selection.normal.id == other.id
    notes = " | ".join(selection.notes)
    assert "tier=same_request: no candidate normals" in notes
    assert "tier=other_requests: 1 candidate(s), 1 matched" in notes


# ---------------------------------------------------------------------------
# Notes and trigger_type recording on pairings
# ---------------------------------------------------------------------------


def test_trigger_pairing_records_trigger_type_and_notes(
    db, tumor_sample, normal_sample, pairing_logic
):
    pairing = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    assert pairing.trigger_type == "manual"
    assert pairing.notes is not None
    assert any("tier=patient-wide" in note for note in pairing.notes)


def test_override_records_mismatched_keys_not_selection_notes(
    db, tumor_sample, non_matching_normal, pairing_logic
):
    pairing = trigger_pairing(
        db, tumor_sample, pairing_logic, "override", non_matching_normal.id
    )
    assert pairing.trigger_type == "override"
    assert pairing.mismatched_keys
    assert pairing.notes is None


def test_trigger_pairing_endpoint_strict_override_rejects(
    db, tumor_sample, non_matching_normal, pairing_logic
):
    payload = PairingTriggerCreate(
        tumor_id=tumor_sample.id,
        trigger_type="override",
        pairing_logic_name="TEMPO",
        normal_id=non_matching_normal.id,
        strict=True,
    )
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 400
    assert "patient_id mismatch" in exc.value.detail


def test_trigger_pairing_endpoint_404_includes_selection_notes(db):
    tumor = _sample(
        db, "TUMOR-404", tumor_or_normal=TumorOrNormal.TUMOR.value,
        run_mode="NovaSeq X",
    )
    # Hiseq normal fails the ARGOS run-mode gate, so no pairing results.
    _sample(
        db, "NORM-404-1", run_mode="Hiseq 3000",
    )
    logic = PairingLogic(name="ARGOS", strategy_name="argos")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    payload = PairingTriggerCreate(
        tumor_id=tumor.id, trigger_type="manual", pairing_logic_name="ARGOS"
    )
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 404
    assert "run_mode mismatch" in exc.value.detail


# ---------------------------------------------------------------------------
# ARGOS strategy
# ---------------------------------------------------------------------------


def test_argos_matches_on_run_mode_bucket(db):
    tumor = _sample(
        db, "TUMOR-NSX", tumor_or_normal=TumorOrNormal.TUMOR.value,
        run_mode="NovaSeq X 1000", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    argos = strategies["argos"]
    assert argos.matches(
        tumor, _sample(db, "NORM-NSX-1", run_mode="novaSeq x 10",
                       preservation=["FFPE"], sample_origin=["FFPE"])
    )
    assert not argos.matches(
        tumor, _sample(db, "NORM-HS-1", run_mode="Hiseq 3000",
                       preservation=["FFPE"], sample_origin=["FFPE"])
    )
    assert not argos.matches(
        tumor, _sample(db, "NORM-NS-1", run_mode="NovaSeq 6000",
                       preservation=["FFPE"], sample_origin=["FFPE"])
    )
    assert not argos.matches(
        tumor, _sample(db, "NORM-UNK-1", run_mode="PromethION",
                       preservation=["FFPE"], sample_origin=["FFPE"])
    )
    unknown_tumor = _sample(
        db, "TUMOR-UNK", tumor_or_normal=TumorOrNormal.TUMOR.value,
        run_mode="PromethION", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    assert argos.matches(
        unknown_tumor, _sample(db, "NORM-UNK-2", run_mode="promethion",
                              preservation=["FFPE"], sample_origin=["FFPE"])
    )


def test_argos_run_mode_gate_skips_newer_mismatching_candidate(db):
    tumor = _sample(
        db, "TUMOR-NSX-2", tumor_or_normal=TumorOrNormal.TUMOR.value,
        run_mode="NovaSeq X", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    newer_hiseq = _sample(
        db, "NORM-HS-2", run_mode="Hiseq 3000",
        preservation=["FFPE"], sample_origin=["FFPE"],
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    older_novaseq = _sample(
        db, "NORM-NSX-2", run_mode="NovaSeq X 1000",
        preservation=["FFPE"], sample_origin=["FFPE"],
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal.id == older_novaseq.id
    assert any("run_mode mismatch" in note for note in selection.notes)


def test_argos_ignores_sample_type(db):
    """Beagle v2.3.0 never keys on specimen/sample type (a Resection tumor
    pairs with a Blood normal); sample_type is not a hard key for argos."""
    tumor = _sample(
        db, "TUMOR-ST-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
        sample_type="resection", run_mode="NovaSeq X",
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    normal = _sample(
        db, "NORM-ST-1", sample_type="blood", run_mode="NovaSeq X",
        preservation=["EDTA-Streck"], sample_origin=["Plasma"],
    )
    argos = strategies["argos"]
    assert argos.matches(tumor, normal)
    selection = argos.select_latest_normal(db, tumor)
    assert selection.normal.id == normal.id


def test_argos_preservation_is_not_a_gate(db):
    """Beagle v2.3.0 does not gate on preservation: a differently-preserved
    (even unresolvable) normal still pairs, latest run date wins."""
    tumor = _sample(
        db, "TUMOR-FROZEN", tumor_or_normal=TumorOrNormal.TUMOR.value,
        preservation=["Fresh or Frozen"], sample_origin=["Fresh or Frozen"],
        run_mode="NovaSeq X",
    )
    newer_ffpe = _sample(
        db, "NORM-FFPE-2", preservation=["FFPE"], sample_origin=["FFPE"],
        run_mode="NovaSeq X",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    older_frozen = _sample(
        db, "NORM-FROZEN-2", preservation=["Fresh or Frozen"],
        sample_origin=["Fresh or Frozen"], run_mode="NovaSeq X",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal.id == newer_ffpe.id


def test_argos_unresolvable_preservation_no_longer_blocks(db):
    tumor = _sample(
        db, "TUMOR-UNK-PRES", tumor_or_normal=TumorOrNormal.TUMOR.value,
        preservation=["FFPE"], sample_origin=["Frozen"],
        run_mode="NovaSeq X",
    )
    normal = _sample(
        db, "NORM-UNK-PRES-1", preservation=["FFPE"], sample_origin=["FFPE"],
        run_mode="NovaSeq X",
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal.id == normal.id


def test_argos_pooled_normal_tier(db):
    tumor = _sample(
        db, "TUMOR-POOL-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
        preservation=["FFPE"], sample_origin=["FFPE"], run_mode="NovaSeq X",
    )
    pooled = _sample(
        db, "NORM-POOL-1", patient_id=None, sample_type="Pooled Normal",
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal.id == pooled.id
    assert any("tier=pooled" in note for note in selection.notes)


def test_argos_pooled_respects_keys_and_run_mode(db):
    tumor = _sample(
        db, "TUMOR-POOL-2", tumor_or_normal=TumorOrNormal.TUMOR.value,
        preservation=["FFPE"], sample_origin=["FFPE"], run_mode="NovaSeq X",
    )
    _sample(
        db, "NORM-POOL-2A", patient_id=None, sample_type="Pooled Normal",
        bait_set="BAIT-2", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    _sample(
        db, "NORM-POOL-2B", patient_id=None, sample_type="Pooled Normal",
        run_mode="Hiseq 3000", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal is None
    assert any("run_mode mismatch" in note for note in selection.notes)


def test_argos_pooled_no_preservation_gate(db):
    """Pooled tier no longer requires resolvable preservation."""
    tumor = _sample(db, "TUMOR-POOL-3", tumor_or_normal=TumorOrNormal.TUMOR.value)
    pooled = _sample(
        db, "NORM-POOL-3", patient_id=None, sample_type="Pooled Normal",
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    selection = strategies["argos"].select_latest_normal(db, tumor)
    assert selection.normal.id == pooled.id
    assert any("tier=pooled" in note for note in selection.notes)


def test_argos_pooled_sample_types_configurable(db):
    logic = PairingLogic(
        name="ARGOS-CFG", strategy_name="argos",
        config={"pooled_sample_types": ["PoolN"]},
    )
    db.add(logic)
    db.commit()
    db.refresh(logic)
    strategy = get_pairing_strategy(logic)
    tumor = _sample(
        db, "TUMOR-POOL-4", tumor_or_normal=TumorOrNormal.TUMOR.value,
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    pooled = _sample(
        db, "NORM-POOLN-1", patient_id=None, sample_type="PoolN",
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    selection = strategy.select_latest_normal(db, tumor)
    assert selection.normal.id == pooled.id


def test_get_pairing_strategy_applies_run_mode_map_config(db):
    logic = PairingLogic(
        name="ARGOS-CFG2", strategy_name="argos",
        config={"run_mode_map": {"nova": "nova"}},
    )
    db.add(logic)
    db.commit()
    db.refresh(logic)
    strategy = get_pairing_strategy(logic)
    assert strategy is not strategies["argos"]
    tumor = _sample(
        db, "TUMOR-CFG-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
        run_mode="Nova 1234", preservation=["FFPE"], sample_origin=["FFPE"],
    )
    normal = _sample(
        db, "NORM-CFG-1", run_mode="nova",
        preservation=["FFPE"], sample_origin=["FFPE"],
    )
    assert strategy.matches(tumor, normal)


# ---------------------------------------------------------------------------
# List endpoint: request_id / run_mode filters
# ---------------------------------------------------------------------------


def test_list_pairings_filter_by_request_id_and_run_mode(db, pairing_logic):
    tumor = _sample(
        db, "TUMOR-FILT-1", tumor_or_normal=TumorOrNormal.TUMOR.value,
        request_id="REQ-11", run_mode="Hiseq 3000",
    )
    _sample(db, "NORM-FILT-1", request_id="REQ-11", run_mode="Hiseq 3000")
    pairing = trigger_pairing(db, tumor, pairing_logic, "manual")
    assert pairing is not None

    assert [
        p.id for p in list_pairings(
            latest=False, limit=None, offset=None, request_id="REQ-11", db=db
        )
    ] == [pairing.id]
    assert [
        p.id for p in list_pairings(
            latest=False, limit=None, offset=None, run_mode="Hiseq 3000", db=db
        )
    ] == [pairing.id]
    assert list_pairings(latest=False, limit=None, offset=None, request_id="REQ-99", db=db) == []
    assert list_pairings(latest=False, limit=None, offset=None, run_mode="NovaSeq X", db=db) == []


def test_trigger_pairing_for_normal_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing_for_normal(db, normal_sample, pairing_logic)
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_for_normal_does_not_replace_existing(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing_for_normal(db, normal_sample, pairing_logic)

    newer_normal = Sample(
        primary_id="NORMAL-010",
        cmo_sample_name="Normal Sample 10",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.add(newer_normal)
    db.commit()
    db.refresh(newer_normal)

    trigger_pairing_for_normal(db, newer_normal, pairing_logic)
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is not None
    assert result.normal_id == normal_sample.id


def test_trigger_pairing_for_normal_no_pairing_logic(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing_for_normal(db, normal_sample, None)
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is None


def test_get_pairing_strategy_default(db):
    logic = PairingLogic(name="DEFAULT")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    strategy = get_pairing_strategy(logic)
    assert strategy is strategies["default"]


def test_get_pairing_strategy_known_aliases(db):
    for strategy_name, expected in (
        ("IMPACT", "default"),
        ("ACCESS", "default"),
        ("TEMPO", "tempo"),
        ("CHRONOS", "tempo"),
    ):
        logic = PairingLogic(name=f"ALIAS-{strategy_name}", strategy_name=strategy_name)
        db.add(logic)
        db.commit()
        db.refresh(logic)

        strategy = get_pairing_strategy(logic)
        assert strategy is strategies[expected]


def test_get_pairing_strategy_unknown(db):
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    with pytest.raises(HTTPException) as exc_info:
        get_pairing_strategy(logic)
    assert exc_info.value.status_code == 400


def test_trigger_pairing_unknown_strategy(db, tumor_sample, normal_sample):
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing(db, tumor_sample, logic, "manual", normal_sample.id)
    assert exc_info.value.status_code == 400


def test_trigger_pairing_for_normal_unknown_strategy(db, tumor_sample, normal_sample):
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing_for_normal(db, normal_sample, logic)
    assert exc_info.value.status_code == 400


def test_custom_strategy_changes_matching(db, tumor_sample, custom_strategy):
    normal = Sample(
        primary_id="NORMAL-CUSTOM-1",
        cmo_sample_name="Normal Custom 1",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-2",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    db.add(normal)
    db.commit()
    db.refresh(normal)

    logic = PairingLogic(name="CUSTOM", strategy_name="custom")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    result = trigger_pairing(db, tumor_sample, logic, "manual", normal.id)
    assert result is not None
    assert result.normal_id == normal.id


def test_custom_strategy_selects_normal_ignoring_bait_set(db, tumor_sample, custom_strategy):
    normal = Sample(
        primary_id="NORMAL-CUSTOM-2",
        cmo_sample_name="Normal Custom 2",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-2",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    db.add(normal)
    db.commit()
    db.refresh(normal)

    logic = PairingLogic(name="CUSTOM", strategy_name="custom")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    result = trigger_pairing(db, tumor_sample, logic, "automatic")
    assert result is not None
    assert result.normal_id == normal.id


def test_custom_strategy_select_matching_tumors(db, tumor_sample, custom_strategy):
    normal = Sample(
        primary_id="NORMAL-CUSTOM-3",
        cmo_sample_name="Normal Custom 3",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-2",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    db.add(normal)
    db.commit()
    db.refresh(normal)

    result = custom_strategy.select_matching_tumors(db, normal)
    assert len(result) == 1
    assert result[0].id == tumor_sample.id


def test_create_pairing_integrity_error_returns_existing(db, tumor_sample, normal_sample, pairing_logic, pg_engine):
    """Verify _create_pairing handles IntegrityError from the partial unique index."""
    pairing1 = _create_pairing(db, tumor_sample, normal_sample, pairing_logic)
    assert pairing1.latest is True

    normal2 = Sample(
        primary_id="NORMAL-IE-1",
        cmo_sample_name="Normal IE 1",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    db.add(normal2)
    db.commit()
    db.refresh(normal2)

    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session2 = SessionLocal()
    try:
        tumor2 = session2.get(Sample, tumor_sample.id)
        normal2_obj = session2.get(Sample, normal2.id)
        logic2 = session2.get(PairingLogic, pairing_logic.id)

        result = _create_pairing(session2, tumor2, normal2_obj, logic2)
        assert result.id == pairing1.id
        assert result.normal_id == normal_sample.id
    finally:
        session2.close()


def test_trigger_pairing_override_conflict_raises_409(db, tumor_sample, normal_sample, pairing_logic, pg_engine, monkeypatch):
    """Verify an override raises 409 when a concurrent override commits a different latest pairing.

    The race is emulated with two sessions interlocking around the commit:
      1. Baseline: the tumor is paired to normal_sample (latest).
      2. Session A (worker thread) starts an override toward normal_b: it reads
         the current latest pairing but has not committed yet.
      3. Meanwhile the main session commits its own override toward normal_a.
      4. Session A's commit then violates the partial unique index (two latest
         pairings for the same tumor + pairing_logic), so _create_pairing rolls
         back and returns the winner, and the override path surfaces a 409.
    """
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    normal_a = Sample(
        primary_id="NORMAL-OC-A",
        cmo_sample_name="Normal OC A",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    normal_b = Sample(
        primary_id="NORMAL-OC-B",
        cmo_sample_name="Normal OC B",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
    )
    db.add(normal_a)
    db.add(normal_b)
    db.commit()
    db.refresh(normal_a)
    db.refresh(normal_b)

    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session_a = SessionLocal()
    read_event = threading.Event()
    go_event = threading.Event()
    outcome: dict = {}

    real_get_latest = pairing_service._get_latest_pairing

    def hooked_get_latest(db_session, tumor_id, pairing_logic_id):
        result = real_get_latest(db_session, tumor_id, pairing_logic_id)
        if db_session is session_a:
            # Session A has read the current latest pairing; let the main
            # session commit its competing override before A continues.
            read_event.set()
            assert go_event.wait(10)
        return result

    monkeypatch.setattr(pairing_service, "_get_latest_pairing", hooked_get_latest)

    def worker():
        tumor_a = session_a.get(Sample, tumor_sample.id)
        logic_a = session_a.get(PairingLogic, pairing_logic.id)
        try:
            pairing = trigger_pairing(session_a, tumor_a, logic_a, "override", normal_b.id)
            outcome["pairing_id"] = pairing.id if pairing is not None else None
        except HTTPException as exc:
            outcome["status_code"] = exc.status_code

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert read_event.wait(10)
        winner = trigger_pairing(db, tumor_sample, pairing_logic, "override", normal_a.id)
        assert winner.normal_id == normal_a.id
        go_event.set()
        thread.join(timeout=10)
    finally:
        session_a.close()

    assert not thread.is_alive()
    assert outcome.get("status_code") == 409
    assert "pairing_id" not in outcome

    # The winner remains the only latest pairing; session A's override was rolled back.
    latest = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert latest is not None
    assert latest.normal_id == normal_a.id


# ---------------------------------------------------------------------------
# Pairing logic "active" enforcement
# ---------------------------------------------------------------------------


def test_resolve_pairing_logic_inactive_by_name(db):
    logic = PairingLogic(name="IMPACT", active=False)
    db.add(logic)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        resolve_pairing_logic(db, pairing_logic_name="IMPACT")
    assert exc.value.status_code == 400
    assert "not active" in exc.value.detail


def test_resolve_pairing_logic_inactive_by_id(db):
    logic = PairingLogic(name="IMPACT", active=False)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    with pytest.raises(HTTPException) as exc:
        resolve_pairing_logic(db, pairing_logic_id=logic.id)
    assert exc.value.status_code == 400
    assert "not active" in exc.value.detail


def test_trigger_pairing_endpoint_inactive_logic(db, tumor_sample):
    logic = PairingLogic(name="IMPACT", active=False)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    payload = PairingTriggerCreate(
        tumor_id=tumor_sample.id,
        trigger_type="manual",
        pairing_logic_name="IMPACT",
    )
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 400


def test_create_sample_default_logic_active_pairs(db, normal_sample, monkeypatch):
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    monkeypatch.setattr(database.settings, "default_pairing_logic_name", "TEMPO")
    payload = SampleCreate(
        primary_id="TUMOR-010",
        cmo_sample_name="Tumor Sample 10",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR,
    )
    sample = create_sample(payload, db=db)
    assert sample.id is not None
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == sample.id))
    assert pairing is not None
    assert pairing.normal_id == normal_sample.id


def test_create_sample_inactive_default_logic_skips_pairing(db, monkeypatch):
    logic = PairingLogic(name="TEMPO", active=False)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    monkeypatch.setattr(database.settings, "default_pairing_logic_name", "TEMPO")
    payload = SampleCreate(
        primary_id="TUMOR-011",
        cmo_sample_name="Tumor Sample 11",
        patient_id="PATIENT-011",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR,
    )
    sample = create_sample(payload, db=db)
    assert sample.id is not None
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == sample.id))
    assert pairing is None


# ---------------------------------------------------------------------------
# List endpoint ordering and pagination
# ---------------------------------------------------------------------------


def _sample_with_created_date(db, primary_id, patient_id, created_date):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient_id=patient_id,
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        created_date=created_date,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_list_samples_ordered_and_paginated(db):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    d3 = datetime(2024, 1, 3, tzinfo=timezone.utc)
    s1 = _sample_with_created_date(db, "N-001", "PAT-001", d1)
    s2 = _sample_with_created_date(db, "N-002", "PAT-002", d2)
    s3 = _sample_with_created_date(db, "N-003", "PAT-003", d3)

    assert [s.id for s in list_samples(limit=None, offset=None, db=db)] == [s3.id, s2.id, s1.id]
    assert [s.id for s in list_samples(limit=2, offset=0, db=db)] == [s3.id, s2.id]
    assert [s.id for s in list_samples(limit=1, offset=1, db=db)] == [s2.id]
    assert [s.id for s in list_samples(limit=1, offset=2, db=db)] == [s1.id]


def test_list_samples_ties_broken_by_id(db):
    d = datetime(2024, 1, 1, tzinfo=timezone.utc)
    s1 = _sample_with_created_date(db, "N-101", "PAT-101", d)
    s2 = _sample_with_created_date(db, "N-102", "PAT-102", d)

    result = list_samples(limit=None, offset=None, db=db)
    assert [s.id for s in result] == sorted([s1.id, s2.id], reverse=True)


def test_list_pairings_ordered_and_paginated(db, tumor_sample, normal_sample, pairing_logic):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    d3 = datetime(2024, 1, 3, tzinfo=timezone.utc)
    p_old = PairingTable(
        tumor_id=tumor_sample.id,
        normal_id=normal_sample.id,
        pairing_logic_id=pairing_logic.id,
        latest=False,
        created_date=d1,
    )
    p_mid = PairingTable(
        tumor_id=tumor_sample.id,
        normal_id=normal_sample.id,
        pairing_logic_id=pairing_logic.id,
        latest=False,
        created_date=d2,
    )
    p_new = PairingTable(
        tumor_id=tumor_sample.id,
        normal_id=normal_sample.id,
        pairing_logic_id=pairing_logic.id,
        latest=True,
        created_date=d3,
    )
    db.add_all([p_old, p_mid, p_new])
    db.commit()

    assert [p.id for p in list_pairings(latest=False, limit=None, offset=None, db=db)] == [p_new.id, p_mid.id, p_old.id]
    assert [p.id for p in list_pairings(latest=True, limit=None, offset=None, db=db)] == [p_new.id]
    assert [p.id for p in list_pairings(latest=False, limit=1, offset=1, db=db)] == [p_mid.id]


# ---------------------------------------------------------------------------
# List endpoint filtering
# ---------------------------------------------------------------------------


def _make_pairing(db, tumor, normal, logic, latest=True, created_date=None):
    pairing = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        latest=latest,
    )
    if created_date is not None:
        pairing.created_date = created_date
    db.add(pairing)
    db.commit()
    db.refresh(pairing)
    return pairing


def _extra_tumor(db, primary_id, patient_id):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient_id=patient_id,
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_list_pairings_filter_by_sample_id(db, tumor_sample, normal_sample, pairing_logic):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    tumor_b = _extra_tumor(db, "TUMOR-002", "PATIENT-002")
    p_a = _make_pairing(db, tumor_sample, normal_sample, pairing_logic, latest=True, created_date=d1)
    p_b = _make_pairing(db, tumor_b, normal_sample, pairing_logic, latest=True, created_date=d2)

    # Sample on the tumor side.
    assert [p.id for p in list_pairings(sample_id=tumor_sample.id, limit=None, offset=None, db=db)] == [p_a.id]
    assert [p.id for p in list_pairings(sample_id=tumor_b.id, limit=None, offset=None, db=db)] == [p_b.id]
    # Sample on the normal side matches both pairings.
    assert [p.id for p in list_pairings(sample_id=normal_sample.id, limit=None, offset=None, db=db)] == [p_b.id, p_a.id]


def test_list_pairings_filter_by_pairing_logic(db, tumor_sample, normal_sample, pairing_logic):
    other = PairingLogic(name="IMPACT")
    db.add(other)
    db.commit()
    db.refresh(other)
    tumor_b = _extra_tumor(db, "TUMOR-003", "PATIENT-003")
    p_t = _make_pairing(db, tumor_sample, normal_sample, pairing_logic)
    p_i = _make_pairing(db, tumor_b, normal_sample, other)

    assert [p.id for p in list_pairings(pairing_logic_id=pairing_logic.id, limit=None, offset=None, db=db)] == [p_t.id]
    assert [p.id for p in list_pairings(pairing_logic_name="IMPACT", limit=None, offset=None, db=db)] == [p_i.id]
    assert [p.id for p in list_pairings(pairing_logic_name="TEMPO", limit=None, offset=None, db=db)] == [p_t.id]


def test_list_pairings_combined_filters(db, tumor_sample, normal_sample, pairing_logic):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    tumor_b = _extra_tumor(db, "TUMOR-004", "PATIENT-004")
    p_stale = _make_pairing(db, tumor_sample, normal_sample, pairing_logic, latest=False, created_date=d1)
    p_current = _make_pairing(db, tumor_b, normal_sample, pairing_logic, latest=True, created_date=d2)

    assert [p.id for p in list_pairings(sample_id=normal_sample.id, latest=True, limit=None, offset=None, db=db)] == [p_current.id]
    assert [p.id for p in list_pairings(sample_id=tumor_b.id, pairing_logic_name="TEMPO", limit=None, offset=None, db=db)] == [p_current.id]
    assert list_pairings(sample_id=tumor_sample.id, latest=True, limit=None, offset=None, db=db) == []
    assert [p.id for p in list_pairings(sample_id=normal_sample.id, latest=True, limit=1, offset=1, db=db)] == []
    # The stale pairing is still reachable without the latest filter.
    assert [p.id for p in list_pairings(sample_id=tumor_sample.id, latest=False, limit=None, offset=None, db=db)] == [p_stale.id]


def test_list_pairings_unknown_sample_id_404(db):
    with pytest.raises(HTTPException) as exc:
        list_pairings(sample_id=uuid.uuid4(), limit=None, offset=None, db=db)
    assert exc.value.status_code == 404


def test_list_pairings_unknown_pairing_logic_id_404(db):
    with pytest.raises(HTTPException) as exc:
        list_pairings(pairing_logic_id=uuid.uuid4(), limit=None, offset=None, db=db)
    assert exc.value.status_code == 404


def test_list_pairings_unknown_pairing_logic_name_404(db):
    with pytest.raises(HTTPException) as exc:
        list_pairings(pairing_logic_name="NOPE", limit=None, offset=None, db=db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# List endpoint sample-field filters
# ---------------------------------------------------------------------------


def _make_sample(db, primary_id, patient_id, tumor_or_normal, gene_panel="PANEL-A", bait_set="BAIT-1", sample_type="tissue"):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient_id=patient_id,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_list_pairings_filter_by_sample_fields(db, tumor_sample, normal_sample, pairing_logic):
    tumor_b = _make_sample(db, "TUMOR-002", "PATIENT-002", TumorOrNormal.TUMOR.value, gene_panel="PANEL-B", bait_set="BAIT-2", sample_type="blood")
    normal_b = _make_sample(db, "NORMAL-002", "PATIENT-002", TumorOrNormal.NORMAL.value, gene_panel="PANEL-B", bait_set="BAIT-2", sample_type="blood")
    p1 = _make_pairing(db, tumor_sample, normal_sample, pairing_logic)
    p2 = _make_pairing(db, tumor_b, normal_b, pairing_logic)

    # Each sample field matches on either side of the pairing.
    assert [p.id for p in list_pairings(primary_id="TUMOR-001", limit=None, offset=None, db=db)] == [p1.id]
    assert [p.id for p in list_pairings(primary_id="NORMAL-001", limit=None, offset=None, db=db)] == [p1.id]
    assert [p.id for p in list_pairings(patient_id="PATIENT-001", limit=None, offset=None, db=db)] == [p1.id]
    assert [p.id for p in list_pairings(patient_id="PATIENT-002", limit=None, offset=None, db=db)] == [p2.id]
    assert [p.id for p in list_pairings(gene_panel="PANEL-B", limit=None, offset=None, db=db)] == [p2.id]
    assert [p.id for p in list_pairings(bait_set="BAIT-2", limit=None, offset=None, db=db)] == [p2.id]
    assert [p.id for p in list_pairings(sample_type="blood", limit=None, offset=None, db=db)] == [p2.id]
    assert [p.id for p in list_pairings(cmo_sample_name="Normal Sample 1", limit=None, offset=None, db=db)] == [p1.id]


def test_list_pairings_field_filters_match_same_side(db, tumor_sample, normal_sample, pairing_logic):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    tumor_b = _make_sample(db, "TUMOR-003", "PATIENT-003", TumorOrNormal.TUMOR.value)
    p1 = _make_pairing(db, tumor_sample, normal_sample, pairing_logic, created_date=d1)
    # p2 shares normal_sample (PATIENT-001) but its tumor belongs to PATIENT-003.
    p2 = _make_pairing(db, tumor_b, normal_sample, pairing_logic, created_date=d2)

    # Fields must all match one sample; TUMOR-003 is PATIENT-003.
    assert list_pairings(primary_id="TUMOR-003", patient_id="PATIENT-001", limit=None, offset=None, db=db) == []
    assert [p.id for p in list_pairings(primary_id="TUMOR-003", patient_id="PATIENT-003", limit=None, offset=None, db=db)] == [p2.id]
    # Both fields match the normal side, so both pairings qualify.
    assert [p.id for p in list_pairings(primary_id="NORMAL-001", patient_id="PATIENT-001", limit=None, offset=None, db=db)] == [p2.id, p1.id]
    # Sample-field filters AND-combine with the logic filter.
    assert [p.id for p in list_pairings(primary_id="TUMOR-003", pairing_logic_name="TEMPO", limit=None, offset=None, db=db)] == [p2.id]


def test_list_pairings_filter_role_scoped(db, tumor_sample, normal_sample, pairing_logic):
    p1 = _make_pairing(db, tumor_sample, normal_sample, pairing_logic)

    assert [p.id for p in list_pairings(primary_id="TUMOR-001", tumor_or_normal=TumorOrNormal.TUMOR, limit=None, offset=None, db=db)] == [p1.id]
    # TUMOR-001 sits on the tumor side only.
    assert list_pairings(primary_id="TUMOR-001", tumor_or_normal=TumorOrNormal.NORMAL, limit=None, offset=None, db=db) == []
    assert [p.id for p in list_pairings(primary_id="NORMAL-001", tumor_or_normal=TumorOrNormal.NORMAL, limit=None, offset=None, db=db)] == [p1.id]


def test_list_pairings_filter_created_date_range(db, tumor_sample, normal_sample, pairing_logic):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    p1 = _make_pairing(db, tumor_sample, normal_sample, pairing_logic, latest=True, created_date=d1)
    p2 = _make_pairing(db, tumor_sample, normal_sample, pairing_logic, latest=False, created_date=d2)

    # Bounds are inclusive.
    assert [p.id for p in list_pairings(created_after=d1, limit=None, offset=None, db=db)] == [p2.id, p1.id]
    assert [p.id for p in list_pairings(created_before=d1, limit=None, offset=None, db=db)] == [p1.id]
    assert [p.id for p in list_pairings(created_after=d1, created_before=d1, limit=None, offset=None, db=db)] == [p1.id]
    assert list_pairings(created_after=d2, created_before=d1, limit=None, offset=None, db=db) == []


# ---------------------------------------------------------------------------
# Sample creation with explicit / default pairing logic
# ---------------------------------------------------------------------------


def _sample_payload(primary_id, patient_id, tumor_or_normal, **kwargs):
    return SampleCreate(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient_id=patient_id,
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=tumor_or_normal,
        **kwargs,
    )


def test_create_sample_explicit_logic_by_name_pairs(db, tumor_sample, normal_sample, pairing_logic):
    payload = _sample_payload("TUMOR-EXP-1", "PATIENT-001", TumorOrNormal.TUMOR, pairing_logic_name="TEMPO")
    sample = create_sample(payload, db=db)
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == sample.id))
    assert pairing is not None
    assert pairing.normal_id == normal_sample.id
    assert pairing.pairing_logic_id == pairing_logic.id


def test_create_sample_explicit_logic_by_id_pairs(db, tumor_sample, normal_sample, pairing_logic):
    payload = _sample_payload("TUMOR-EXP-2", "PATIENT-001", TumorOrNormal.TUMOR, pairing_logic_id=pairing_logic.id)
    sample = create_sample(payload, db=db)
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == sample.id))
    assert pairing is not None
    assert pairing.normal_id == normal_sample.id
    assert pairing.pairing_logic_id == pairing_logic.id


def test_create_sample_explicit_inactive_logic_rejected(db, tumor_sample):
    logic = PairingLogic(name="IMPACT", active=False)
    db.add(logic)
    db.commit()
    payload = _sample_payload("TUMOR-EXP-3", "PATIENT-001", TumorOrNormal.TUMOR, pairing_logic_name="IMPACT")
    with pytest.raises(HTTPException) as exc:
        create_sample(payload, db=db)
    assert exc.value.status_code == 400
    assert "not active" in exc.value.detail


def test_create_sample_normal_with_default_logic_pairs_unpaired_tumor(db, tumor_sample, monkeypatch):
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    db.commit()
    monkeypatch.setattr(database.settings, "default_pairing_logic_name", "TEMPO")
    payload = _sample_payload("NORMAL-DEF-1", "PATIENT-001", TumorOrNormal.NORMAL)
    sample = create_sample(payload, db=db)
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == tumor_sample.id))
    assert pairing is not None
    assert pairing.normal_id == sample.id


def test_create_sample_normal_without_logic_creates_no_pairing(db, tumor_sample):
    payload = _sample_payload("NORMAL-NONE-1", "PATIENT-001", TumorOrNormal.NORMAL)
    sample = create_sample(payload, db=db)
    pairing = db.scalar(select(PairingTable).where(PairingTable.tumor_id == tumor_sample.id))
    assert pairing is None


# ---------------------------------------------------------------------------
# Pairing logic endpoint
# ---------------------------------------------------------------------------


def test_create_pairing_logic_happy_path(db):
    payload = PairingLogicCreate(name="IMPACT", description="IMPACT panel logic", strategy_name="default")
    logic = create_pairing_logic(payload, db=db)
    assert logic.id is not None
    assert logic.name == "IMPACT"
    assert logic.active is True
    assert logic.strategy_name == "default"
    fetched = db.scalar(select(PairingLogic).where(PairingLogic.name == "IMPACT"))
    assert fetched is not None
    assert fetched.id == logic.id


def test_create_pairing_logic_duplicate_rejected(db, pairing_logic):
    payload = PairingLogicCreate(name="TEMPO")
    with pytest.raises(HTTPException) as exc:
        create_pairing_logic(payload, db=db)
    assert exc.value.status_code == 409


def test_create_pairing_logic_unknown_strategy_rejected(db):
    payload = PairingLogicCreate(name="WEIRD", strategy_name="nope")
    with pytest.raises(HTTPException) as exc:
        create_pairing_logic(payload, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Trigger endpoint behavior
# ---------------------------------------------------------------------------


def test_trigger_pairing_endpoint_manual_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    payload = PairingTriggerCreate(tumor_id=tumor_sample.id, trigger_type="manual", pairing_logic_name="TEMPO")
    pairing = trigger_pairing_endpoint(payload, db=db)
    assert pairing is not None
    assert pairing.tumor_id == tumor_sample.id
    assert pairing.normal_id == normal_sample.id
    assert pairing.pairing_logic == "TEMPO"


def test_trigger_pairing_endpoint_override_replaces_latest(db, tumor_sample, normal_sample, pairing_logic):
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    other_normal = Sample(
        primary_id="NORMAL-TRG-1",
        cmo_sample_name="Normal Trigger 1",
        patient_id="PATIENT-001",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.add(other_normal)
    db.commit()
    db.refresh(other_normal)

    payload = PairingTriggerCreate(
        tumor_id=tumor_sample.id,
        trigger_type="override",
        pairing_logic_name="TEMPO",
        normal_id=other_normal.id,
    )
    pairing = trigger_pairing_endpoint(payload, db=db)
    assert pairing.normal_id == other_normal.id
    assert pairing.latest is True
    latest = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert latest.normal_id == other_normal.id


def test_trigger_pairing_endpoint_tumor_not_found(db, pairing_logic):
    payload = PairingTriggerCreate(tumor_id=uuid.uuid4(), trigger_type="manual", pairing_logic_name="TEMPO")
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 404


def test_trigger_pairing_endpoint_tumor_is_normal(db, normal_sample, pairing_logic):
    payload = PairingTriggerCreate(tumor_id=normal_sample.id, trigger_type="manual", pairing_logic_name="TEMPO")
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 404


def test_trigger_pairing_endpoint_missing_logic(db, tumor_sample):
    payload = PairingTriggerCreate(tumor_id=tumor_sample.id, trigger_type="manual")
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 400


def test_trigger_pairing_endpoint_no_matching_normal(db, tumor_sample, pairing_logic):
    payload = PairingTriggerCreate(tumor_id=tumor_sample.id, trigger_type="manual", pairing_logic_name="TEMPO")
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 404


def test_trigger_pairing_override_normal_id_missing_is_404(db, tumor_sample, pairing_logic):
    with pytest.raises(HTTPException) as exc:
        trigger_pairing(db, tumor_sample, pairing_logic, "override", uuid.uuid4())
    assert exc.value.status_code == 404
    assert "Normal sample not found" in exc.value.detail


def test_trigger_pairing_manual_normal_id_missing_is_404(db, tumor_sample, pairing_logic):
    with pytest.raises(HTTPException) as exc:
        trigger_pairing(db, tumor_sample, pairing_logic, "manual", uuid.uuid4())
    assert exc.value.status_code == 404


def test_trigger_pairing_normal_id_is_tumor_is_404(db, tumor_sample, pairing_logic):
    with pytest.raises(HTTPException) as exc:
        trigger_pairing(db, tumor_sample, pairing_logic, "override", tumor_sample.id)
    assert exc.value.status_code == 404
    assert "Normal sample not found" in exc.value.detail


def test_trigger_pairing_override_normal_not_found_endpoint(db, tumor_sample, pairing_logic):
    payload = PairingTriggerCreate(
        tumor_id=tumor_sample.id,
        trigger_type="override",
        pairing_logic_name="TEMPO",
        normal_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as exc:
        trigger_pairing_endpoint(payload, db=db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Pairing logic list ordering and pagination
# ---------------------------------------------------------------------------


def test_list_pairing_logic_ordered_and_paginated(db):
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    l_old = PairingLogic(name="L-OLD", created_date=d1)
    l_new = PairingLogic(name="L-NEW", created_date=d2)
    db.add_all([l_old, l_new])
    db.commit()

    assert [l.name for l in list_pairing_logic(limit=None, offset=None, db=db)] == ["L-NEW", "L-OLD"]
    assert [l.name for l in list_pairing_logic(limit=1, offset=0, db=db)] == ["L-NEW"]
    assert [l.name for l in list_pairing_logic(limit=1, offset=1, db=db)] == ["L-OLD"]


def test_list_pairing_logic_ties_broken_by_id(db):
    d = datetime(2024, 1, 1, tzinfo=timezone.utc)
    l1 = PairingLogic(name="L-TIE-1", created_date=d)
    l2 = PairingLogic(name="L-TIE-2", created_date=d)
    db.add_all([l1, l2])
    db.commit()

    result = list_pairing_logic(limit=None, offset=None, db=db)
    assert [l.id for l in result] == sorted([l1.id, l2.id], reverse=True)


# ---------------------------------------------------------------------------
# App health endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint():
    from main import health

    assert health() == {"status": "ok"}


# ---------------------------------------------------------------------------
# tempo strategy (beagle TEMPO/CHRONOS alignment)
# ---------------------------------------------------------------------------


def _tempo_logic(db, name="TEMPO"):
    logic = PairingLogic(name=name, strategy_name="tempo")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _tempo_pair(db, tumor, normal, logic, trigger_type="historical"):
    pairing = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        trigger_type=trigger_type,
        latest=True,
    )
    db.add(pairing)
    db.commit()
    db.refresh(pairing)
    return pairing


def test_tempo_historical_pairing_bypasses_bait_run_mode_checks(db):
    """beagle pre_pairing: the historical normal is returned by identity,
    bait/run-mode checks skipped, and a newer fully-matching normal is
    never considered."""
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-HIST-T", tumor_or_normal=TumorOrNormal.TUMOR.value, run_mode="HiSeq 3000"
    )
    # Historical normal: bait and run mode both mismatch on purpose.
    old_normal = _sample(
        db, "TMP-HIST-N-OLD",
        bait_set="BAIT-X", run_mode="NovaSeq 6000",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    # Newer normal that matches everything.
    new_normal = _sample(
        db, "TMP-HIST-N-NEW",
        run_mode="HiSeq 3000",
        sequencing_date=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    _tempo_pair(db, tumor, old_normal, logic)

    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal is not None
    assert selection.normal.id == old_normal.id
    assert any("historical pairing retained" in note for note in selection.notes)
    assert any("bypassed" in note for note in selection.notes)


def test_tempo_historical_pairing_no_fallback_when_normal_missing(db):
    """beagle: pre_pairing pointing at a missing normal -> tumor stays
    unpaired; there is no patient-wide fallback."""
    logic = _tempo_logic(db)
    tumor = _sample(db, "TMP-GONE-T", tumor_or_normal=TumorOrNormal.TUMOR.value)
    normal = _sample(db, "TMP-GONE-N")
    _tempo_pair(db, tumor, normal, logic)
    db.delete(normal)
    db.commit()

    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal is None
    assert any("missing" in note for note in selection.notes)


def test_tempo_patient_wide_latest_run_date_wins(db):
    logic = _tempo_logic(db)
    tumor = _sample(db, "TMP-LATEST-T", tumor_or_normal=TumorOrNormal.TUMOR.value)
    older = _sample(
        db, "TMP-LATEST-N1",
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    newer = _sample(
        db, "TMP-LATEST-N2",
        sequencing_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal.id == newer.id


def test_tempo_bait_and_run_mode_mismatch_leaves_tumor_unpaired(db):
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-GATE-T", tumor_or_normal=TumorOrNormal.TUMOR.value, run_mode="HiSeq 3000"
    )
    _sample(db, "TMP-GATE-N-BAD-BAIT", bait_set="BAIT-2")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal is None

    _sample(db, "TMP-GATE-N-BAD-RM", run_mode="NovaSeq 6000")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal is None
    assert any("bait_set mismatch" in note for note in selection.notes)
    assert any("run_mode mismatch" in note for note in selection.notes)


def test_tempo_bait_compared_case_insensitively(db):
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-CASE-T", tumor_or_normal=TumorOrNormal.TUMOR.value, bait_set="Agilent"
    )
    normal = _sample(db, "TMP-CASE-N", bait_set="agilent")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal.id == normal.id


def test_tempo_run_mode_buckets_novaseq_family(db):
    """beagle buckets any 'novaseq' substring into one bucket, so
    'NovaSeq X 1000' and 'NovaSeq 6000' pair (unlike the argos map)."""
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-BUCKET-T", tumor_or_normal=TumorOrNormal.TUMOR.value, run_mode="NovaSeq X 1000"
    )
    normal = _sample(db, "TMP-BUCKET-N", run_mode="NovaSeq 6000")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal.id == normal.id


def test_tempo_gene_panel_is_not_a_key(db):
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-PANEL-T", tumor_or_normal=TumorOrNormal.TUMOR.value, gene_panel="PANEL-A"
    )
    normal = _sample(db, "TMP-PANEL-N", gene_panel="PANEL-B")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal.id == normal.id


def test_tempo_sample_type_is_not_a_key(db):
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-TYPE-T", tumor_or_normal=TumorOrNormal.TUMOR.value, sample_type="Tumor"
    )
    normal = _sample(db, "TMP-TYPE-N", sample_type="Blood")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal.id == normal.id


def test_tempo_scoped_to_patient(db):
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-SCOPE-T",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        patient_id="PATIENT-1",
    )
    _sample(db, "TMP-SCOPE-N", patient_id="PATIENT-2")
    selection = strategies["tempo"].select_latest_normal(db, tumor, logic)
    assert selection.normal is None


def test_tempo_select_matching_tumors_respects_soft_keys(db):
    logic = _tempo_logic(db)
    matching = _sample(
        db, "TMP-MATCH-T1", tumor_or_normal=TumorOrNormal.TUMOR.value
    )
    wrong_bait = _sample(
        db, "TMP-MATCH-T2", tumor_or_normal=TumorOrNormal.TUMOR.value, bait_set="BAIT-9"
    )
    normal = _sample(db, "TMP-MATCH-N")
    tumors = strategies["tempo"].select_matching_tumors(db, normal, logic)
    assert [t.id for t in tumors] == [matching.id]


def test_tempo_trigger_pairing_automatic_keeps_historical(db):
    """Service-level: an existing (historical) pairing is returned
    unchanged on an automatic trigger."""
    logic = _tempo_logic(db)
    tumor = _sample(
        db, "TMP-SVC-T", tumor_or_normal=TumorOrNormal.TUMOR.value, bait_set="BAIT-X"
    )
    normal = _sample(db, "TMP-SVC-N")
    _tempo_pair(db, tumor, normal, logic)

    result = trigger_pairing(db, tumor, logic, "automatic")
    assert result is not None
    assert result.normal_id == normal.id
    assert result.trigger_type == "historical"
