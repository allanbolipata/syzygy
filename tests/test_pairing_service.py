import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import pairing_service
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
    assert _matches(tumor_sample, normal_sample) is True


def test_matches_false_different_patient(tumor_sample, non_matching_normal):
    assert _matches(tumor_sample, non_matching_normal) is False


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
    assert _matches(tumor_sample, normal) is False


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
    assert _matches(tumor_sample, normal) is False


def test_select_latest_normal_returns_most_recent(db, tumor_sample, normal_sample, older_normal_sample):
    result = _select_latest_normal(db, tumor_sample)
    assert result is not None
    assert result.id == normal_sample.id


def test_select_latest_normal_no_match(db, tumor_sample):
    result = _select_latest_normal(db, tumor_sample)
    assert result is None


def test_select_matching_tumors_returns_matches(db, tumor_sample, normal_sample):
    result = _select_matching_tumors(db, normal_sample)
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
    result = _select_matching_tumors(db, non_matching)
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


def test_trigger_pairing_override_normal_does_not_match(db, tumor_sample, non_matching_normal, pairing_logic):
    with pytest.raises(HTTPException) as exc_info:
        trigger_pairing(db, tumor_sample, pairing_logic, "override", non_matching_normal.id)
    assert exc_info.value.status_code == 400


def test_trigger_pairing_no_pairing_logic(db, tumor_sample, normal_sample):
    result = trigger_pairing(db, tumor_sample, None, "automatic")
    assert result is None


def test_trigger_pairing_no_matching_normal(db, tumor_sample, pairing_logic):
    result = trigger_pairing(db, tumor_sample, pairing_logic, "automatic")
    assert result is None


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
    for name in ("IMPACT", "TEMPO", "ACCESS"):
        logic = PairingLogic(name=name)
        db.add(logic)
        db.commit()
        db.refresh(logic)

        strategy = get_pairing_strategy(logic)
        assert strategy is strategies["default"]


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
