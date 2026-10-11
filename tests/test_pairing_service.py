import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

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
from schemas import TumorOrNormal


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


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


def test_trigger_pairing_for_normal_no_pairing_logic(db, tumor_sample, normal_sample):
    trigger_pairing_for_normal(db, normal_sample, None)
    result = _get_latest_pairing(db, tumor_sample.id, pairing_logic.id)
    assert result is None
