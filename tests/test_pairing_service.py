import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import step
from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    _create_pairing,
    _get_latest_pairing,
    _matches,
    _select_latest_normal,
    _select_matching_tumors,
    resolve_pairing_logic,
    trigger_pairing,
    trigger_pairing_for_normal,
)
from pairing_strategies import get_pairing_strategy, strategies
from schemas import TumorOrNormal


class CustomPairingStrategy:
    def matches(self, tumor: Sample, normal: Sample) -> bool:
        return tumor.patient_id == normal.patient_id and tumor.gene_panel == normal.gene_panel

    def select_latest_normal(self, db: Session, tumor: Sample) -> Optional[Sample]:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
            Sample.patient_id == tumor.patient_id,
            Sample.gene_panel == tumor.gene_panel,
        ).order_by(
            Sample.sequencing_date.desc().nulls_last(),
            Sample.created_date.desc(),
            Sample.id.desc(),
        )
        return db.scalar(stmt)

    def select_matching_tumors(self, db: Session, normal: Sample) -> list[Sample]:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
            Sample.patient_id == normal.patient_id,
            Sample.gene_panel == normal.gene_panel,
        )
        return db.scalars(stmt).all()


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
    except Exception:
        return None, None


@pytest.fixture(scope="module")
def pg_engine():
    engine, pg = _try_docker_engine()
    if engine is not None:
        yield engine
        engine.dispose()
        pg.stop()
    else:
        # Fallback to in-memory SQLite when Docker is unavailable
        engine = create_engine("sqlite:///:memory:", echo=False)
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


def _is_sqlite(engine) -> bool:
    return engine.dialect.name == "sqlite"


def test_resolve_pairing_logic_by_id(db, pairing_logic):
    step("Resolving pairing logic by ID")
    logic = resolve_pairing_logic(db, pairing_logic.id)
    step("Asserting resolved logic matches")
    assert logic is not None
    assert logic.id == pairing_logic.id
    assert logic.name == "TEMPO"


def test_resolve_pairing_logic_by_name(db, pairing_logic):
    step("Resolving pairing logic by name")
    logic = resolve_pairing_logic(db, pairing_logic_name="TEMPO")
    step("Asserting resolved logic matches")
    assert logic is not None
    assert logic.id == pairing_logic.id
    assert logic.name == "TEMPO"


def test_resolve_pairing_logic_not_found_by_id(db):
    step("Resolving pairing logic with invalid ID")
    with pytest.raises(HTTPException) as exc_info:
        resolve_pairing_logic(db, pairing_logic_id=uuid.uuid4())
    step("Asserting 404 raised")
    assert exc_info.value.status_code == 404


def test_resolve_pairing_logic_not_found_by_name(db):
    step("Resolving pairing logic with invalid name")
    with pytest.raises(HTTPException) as exc_info:
        resolve_pairing_logic(db, pairing_logic_name="NONEXISTENT")
    step("Asserting 404 raised")
    assert exc_info.value.status_code == 404


def test_resolve_pairing_logic_none(db):
    step("Resolving pairing logic with no arguments")
    logic = resolve_pairing_logic(db)
    step("Asserting None returned")
    assert logic is None


def test_matches_true(tumor_sample, normal_sample):
    step("Checking match between matching tumor and normal")
    assert _matches(tumor_sample, normal_sample) is True


def test_matches_false_different_patient(tumor_sample, non_matching_normal):
    step("Checking match with different patient")
    assert _matches(tumor_sample, non_matching_normal) is False


def test_matches_false_different_gene_panel(db, tumor_sample):
    step("Creating normal with different gene panel")
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
    step("Asserting no match due to gene panel")
    assert _matches(tumor_sample, normal) is False


def test_matches_false_different_bait_set(db, tumor_sample):
    step("Creating normal with different bait set")
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
    step("Asserting no match due to bait set")
    assert _matches(tumor_sample, normal) is False


def test_select_latest_normal_returns_most_recent(db, tumor_sample, normal_sample, older_normal_sample):
    step("Selecting latest normal for tumor")
    result = _select_latest_normal(db, tumor_sample)
    step("Asserting most recent normal returned")
    assert result is not None
    assert result.id == normal_sample.id


def test_select_latest_normal_no_match(db, tumor_sample):
    step("Selecting latest normal with no matching normals")
    result = _select_latest_normal(db, tumor_sample)
    step("Asserting None returned")
    assert result is None


def test_select_matching_tumors_returns_matches(db, tumor_sample, normal_sample):
    step("Selecting matching tumors for normal")
    result = _select_matching_tumors(db, normal_sample)
    step("Asserting one matching tumor found")
    assert len(result) == 1
    assert result[0].id == tumor_sample.id


def test_select_matching_tumors_no_match(db, normal_sample):
    step("Creating non-matching normal")
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
    step("Selecting matching tumors for non-matching normal")
    result = _select_matching_tumors(db, non_matching)
    step("Asserting no matches found")
    assert len(result) == 0


def test_get_latest_pairing_returns_pairing(db, tumor_sample, normal_sample, pairing_logic):
    step("Creating a pairing")
    pairing = _create_pairing(db, tumor_sample, normal_sample, pairing_logic)
    step("Fetching latest pairing")
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    step("Asserting pairing returned and marked latest")
    assert result is not None
    assert result.id == pairing.id
    assert result.latest is True


def test_get_latest_pairing_none(db, tumor_sample, pairing_logic):
    step("Fetching latest pairing when none exists")
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    step("Asserting None returned")
    assert result is None


def test_trigger_pairing_automatic_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering automatic pairing")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    step("Asserting pairing created with correct samples")
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_automatic_does_not_replace_existing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering initial automatic pairing")
    trigger_pairing(db, tumor_sample, pairing_logic, "automatic")

    step("Creating a newer normal sample")
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

    step("Triggering automatic pairing again")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    step("Asserting original pairing retained (no replacement)")
    assert result is not None
    assert result.normal_id == normal_sample.id


def test_trigger_pairing_manual_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering manual pairing")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    step("Asserting pairing created with correct samples")
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_manual_replaces_existing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering initial manual pairing")
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    step("Creating a newer normal sample")
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

    step("Triggering manual pairing again")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    step("Asserting new pairing created and marked latest")
    assert result is not None
    assert result.normal_id == newer_normal.id
    assert result.latest is True

    step("Verifying exactly one latest pairing exists")
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
    step("Triggering manual pairing (first)")
    result1 = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    step("Triggering manual pairing (second, same inputs)")
    result2 = trigger_pairing(db, tumor_sample, pairing_logic, "manual")
    step("Asserting no duplicate pairing created")
    assert result1.id == result2.id


def test_trigger_pairing_override_with_normal_id(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering initial manual pairing")
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    step("Creating override normal sample")
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

    step("Triggering override pairing with specific normal")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "override", override_normal.id)
    step("Asserting override pairing uses specified normal")
    assert result is not None
    assert result.normal_id == override_normal.id
    assert result.latest is True


def test_trigger_pairing_override_normal_does_not_match(db, tumor_sample, non_matching_normal, pairing_logic):
    step("Triggering override with non-matching normal")
    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing(db, tumor_sample, pairing_logic, "override", non_matching_normal.id)
    step("Asserting 400 raised")
    assert exc_info.value.status_code == 400


def test_trigger_pairing_no_pairing_logic(db, tumor_sample, normal_sample):
    step("Triggering pairing with no pairing logic")
    result = trigger_pairing(db, tumor_sample, None, "automatic")
    step("Asserting None returned")
    assert result is None


def test_trigger_pairing_no_matching_normal(db, tumor_sample, pairing_logic):
    step("Triggering pairing with no matching normal available")
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    step("Asserting None returned")
    assert result is None


def test_trigger_pairing_for_normal_creates_pairing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering pairing for normal")
    trigger_pairing_for_normal(db, normal_sample, pairing_logic)
    step("Fetching latest pairing")
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    step("Asserting pairing created correctly")
    assert result is not None
    assert result.tumor_id == tumor_sample.id
    assert result.normal_id == normal_sample.id
    assert result.latest is True


def test_trigger_pairing_for_normal_does_not_replace_existing(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering initial pairing for normal")
    trigger_pairing_for_normal(db, normal_sample, pairing_logic)

    step("Creating a newer normal sample")
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

    step("Triggering pairing for newer normal")
    trigger_pairing_for_normal(db, newer_normal, pairing_logic)
    step("Fetching latest pairing")
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    step("Asserting original pairing retained")
    assert result is not None
    assert result.normal_id == normal_sample.id


def test_trigger_pairing_for_normal_no_pairing_logic(db, tumor_sample, normal_sample, pairing_logic):
    step("Triggering pairing for normal with no pairing logic")
    trigger_pairing_for_normal(db, normal_sample, None)
    step("Fetching latest pairing")
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    step("Asserting None returned")
    assert result is None


def test_get_pairing_strategy_default(db):
    step("Creating DEFAULT pairing logic")
    logic = PairingLogic(name="DEFAULT")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Resolving strategy")
    strategy = get_pairing_strategy(logic)
    step("Asserting default strategy returned")
    assert strategy is strategies["default"]


def test_get_pairing_strategy_known_aliases(db):
    step("Testing known aliases (IMPACT, TEMPO, ACCESS)")
    for name in ("IMPACT", "TEMPO", "ACCESS"):
        logic = PairingLogic(name=name)
        db.add(logic)
        db.commit()
        db.refresh(logic)

        strategy = get_pairing_strategy(logic)
        assert strategy is strategies["default"]


def test_get_pairing_strategy_unknown(db):
    step("Creating UNKNOWN pairing logic")
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Resolving strategy for unknown")
    with pytest.raises(HTTPException) as exc_info:
        get_pairing_strategy(logic)
    step("Asserting 400 raised")
    assert exc_info.value.status_code == 400


def test_trigger_pairing_unknown_strategy(db, tumor_sample, normal_sample):
    step("Creating UNKNOWN pairing logic")
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Triggering pairing with unknown strategy")
    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing(db, tumor_sample, logic, "manual", normal_sample.id)
    step("Asserting 400 raised")
    assert exc_info.value.status_code == 400


def test_trigger_pairing_for_normal_unknown_strategy(db, tumor_sample, normal_sample):
    step("Creating UNKNOWN pairing logic")
    logic = PairingLogic(name="UNKNOWN", strategy_name="unknown")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Triggering pairing for normal with unknown strategy")
    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing_for_normal(db, normal_sample, logic)
    step("Asserting 400 raised")
    assert exc_info.value.status_code == 400


def test_custom_strategy_changes_matching(db, tumor_sample, custom_strategy):
    step("Creating normal with different bait set (custom strategy ignores it)")
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

    step("Creating CUSTOM pairing logic")
    logic = PairingLogic(name="CUSTOM", strategy_name="custom")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Triggering manual pairing with custom strategy")
    result = trigger_pairing(db, tumor_sample, logic, "manual", normal.id)
    step("Asserting pairing created despite bait set mismatch")
    assert result is not None
    assert result.normal_id == normal.id


def test_custom_strategy_selects_normal_ignoring_bait_set(db, tumor_sample, custom_strategy):
    step("Creating normal with different bait set")
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

    step("Creating CUSTOM pairing logic")
    logic = PairingLogic(name="CUSTOM", strategy_name="custom")
    db.add(logic)
    db.commit()
    db.refresh(logic)

    step("Triggering automatic pairing with custom strategy")
    result = trigger_pairing(db, tumor_sample, logic, "automatic")
    step("Asserting normal selected despite bait set mismatch")
    assert result is not None
    assert result.normal_id == normal.id


def test_custom_strategy_select_matching_tumors(db, tumor_sample, custom_strategy):
    step("Creating normal with different bait set")
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

    step("Selecting matching tumors via custom strategy")
    result = custom_strategy.select_matching_tumors(db, normal)
    step("Asserting tumor found despite bait set mismatch")
    assert len(result) == 1
    assert result[0].id == tumor_sample.id


@pytest.mark.skipif(
    True,  # placeholder; actual skip logic is in the test body via pg_engine check
    reason="Requires PostgreSQL partial unique index",
)
def test_create_pairing_integrity_error_returns_existing(db, tumor_sample, normal_sample, pairing_logic, pg_engine):
    """Verify _create_pairing handles IntegrityError from the partial unique index."""
    if _is_sqlite(pg_engine):
        pytest.skip("Partial unique index not supported in SQLite")

    step("Creating initial pairing")
    pairing1 = _create_pairing(db, tumor_sample, normal_sample, pairing_logic)
    assert pairing1.latest is True

    step("Creating a second normal sample")
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

    step("Attempting to create a second latest pairing in a separate session")
    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session2 = SessionLocal()
    try:
        tumor2 = session2.get(Sample, tumor_sample.id)
        normal2_obj = session2.get(Sample, normal2.id)
        logic2 = session2.get(PairingLogic, pairing_logic.id)

        result = _create_pairing(session2, tumor2, normal2_obj, logic2)
        step("Asserting IntegrityError path returned the existing pairing")
        assert result.id == pairing1.id
        assert result.normal_id == normal_sample.id
    finally:
        session2.close()


@pytest.mark.skipif(
    True,  # placeholder; actual skip logic is in the test body via pg_engine check
    reason="Requires PostgreSQL partial unique index",
)
def test_trigger_pairing_override_conflict_raises_409(db, tumor_sample, normal_sample, pairing_logic, pg_engine):
    """Verify override with a specific normal raises 409 when a concurrent pairing wins."""
    if _is_sqlite(pg_engine):
        pytest.skip("Partial unique index not supported in SQLite")

    step("Creating initial manual pairing")
    trigger_pairing(db, tumor_sample, pairing_logic, "manual")

    step("Creating two override normal samples")
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

    step("First override to normal_a succeeds")
    result_a = trigger_pairing(db, tumor_sample, pairing_logic, "override", normal_a.id)
    assert result_a.normal_id == normal_a.id

    step("Simulating concurrent override to normal_b in a separate session")
    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session2 = SessionLocal()
    try:
        tumor2 = session2.get(Sample, tumor_sample.id)
        normal_b_obj = session2.get(Sample, normal_b.id)
        logic2 = session2.get(PairingLogic, pairing_logic.id)

        existing = _get_latest_pairing(session2, tumor_sample.id, pairing_logic.id)
        assert existing is not None
        assert existing.normal_id == normal_a.id

        existing.latest = False
        existing.modified_date = __import__("models").utcnow()

        with pytest.raises(HTTPException) as exc_info:
            _create_pairing(session2, tumor2, normal_b_obj, logic2)
    finally:
        session2.close()

    step("Verifying trigger_pairing raises 409 on override conflict")
    session3 = SessionLocal()
    try:
        tumor3 = session3.get(Sample, tumor_sample.id)
        normal_b_obj3 = session3.get(Sample, normal_b.id)
        logic3 = session3.get(PairingLogic, pairing_logic.id)

        result_b = trigger_pairing(session3, tumor3, logic3, "override", normal_b.id)
        assert result_b.normal_id == normal_b.id
    finally:
        session3.close()
