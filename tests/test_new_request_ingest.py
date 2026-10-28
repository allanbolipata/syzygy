"""Tests for cmo-new-request NATS ingestion (handle_new_request).

Pattern per the suite: in-memory SQLite, direct handler calls, no
TestClient.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import BaitSet, PairingLogic, PairingTable, Sample
from nats_ingest import handle_new_request

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "nats" / "99908_C_new_request.json"
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    # The pairing-logic rows the bait-set classification rules resolve to.
    for name, strategy in (("ARGOS", "argos"), ("TEMPO", "tempo"), ("ACCESS", "default")):
        session.add(PairingLogic(name=name, strategy_name=strategy))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def fixture_payload():
    return json.loads(FIXTURE_PATH.read_text())


def _sample_by_primary_id(db, primary_id):
    return db.scalar(select(Sample).where(Sample.primary_id == primary_id))


def _pairing_rows(db):
    return list(db.scalars(select(PairingTable)).all())


def test_vendored_fixture_registers_all_samples(db, fixture_payload):
    result = handle_new_request(db, fixture_payload)
    assert result["status"] == "ok"
    summary = result["summary"]
    assert summary["request_id"] == "99908_C"
    assert summary["samples_created"] == 5
    assert summary["samples_updated"] == 0
    assert summary["skipped"] == []

    samples = list(db.scalars(select(Sample)).all())
    assert {s.primary_id for s in samples} == {
        "99908_C_1", "99908_C_2", "99908_C_4", "99908_C_9", "99908_C_17",
    }

    sample = _sample_by_primary_id(db, "99908_C_17")
    assert sample.patient_id == "C-PPPP16"
    assert sample.request_id == "99908_C"
    assert sample.cmo_sample_name == "C-PPPP16-X001-d01"
    assert sample.gene_panel == "IMPACT505"
    assert sample.bait_set == "IMPACT505_BAITS"
    assert sample.tumor_or_normal == "NORMAL"
    assert sample.sample_type == "other"
    assert sample.run_mode == "NovaSeq S2"
    assert sample.file_group == "SMILE"

    entry = next(s for s in fixture_payload["samples"] if s["primaryId"] == "99908_C_17")
    meta = sample.additional_metadata
    assert meta["qcReports"] == entry["qcReports"]
    assert meta["importDate"] == entry["importDate"]
    # Request-level blob comes from the requestJson string, plus status.
    assert meta["requestMetadata"] == json.loads(fixture_payload["requestJson"])
    assert meta["requestMetadata"]["requestId"] == "99908_C"
    assert meta["requestStatus"] == fixture_payload["status"]


def test_all_normals_without_matching_tumors_create_no_pairing(db, fixture_payload):
    """All five fixture samples are NORMAL: the back-fill runs but finds
    no matching tumors, so nothing is paired and nothing errors."""
    result = handle_new_request(db, fixture_payload)
    assert result["status"] == "ok"
    assert _pairing_rows(db) == []
    # IMPACT505_BAITS is covered by ARGOS (rule-matched, auto-registered
    # on first sight) — no sample is left uncovered.
    assert result["summary"]["uncovered"] == []
    assert (
        db.scalar(select(BaitSet).where(BaitSet.bait_set == "IMPACT505_BAITS"))
        is not None
    )
    for sample in db.scalars(select(Sample)).all():
        assert sample.errors in (None, [])


def _seed_normal(db, primary_id="99908_B_1", request_id="99908_B") -> Sample:
    normal = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_1_1_1",
        patient_id="C-PPPP16",
        request_id=request_id,
        sample_type="other",
        gene_panel="IMPACT505",
        bait_set="IMPACT505_BAITS",
        tumor_or_normal="NORMAL",
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        run_mode="NovaSeq S2",
        sequencing_date=datetime(2023, 4, 1, tzinfo=timezone.utc),
    )
    db.add(normal)
    db.commit()
    return normal


def test_tumor_ingest_pairs_with_seeded_normal(db):
    """A new-request tumor whose patient has a NORMAL with the same
    patient_id + gene_panel + bait_set and matching run_mode/preservation
    is auto-paired on ingest."""
    normal = _seed_normal(db)
    tumor_entry = {
        "primaryId": "99908_C_1",
        "cmoSampleName": "C-PPPP16-X001-d01",
        "cmoPatientId": "C-PPPP16",
        "sampleType": "Biopsy",
        "tumorOrNormal": "Tumor",
        "preservation": ["FFPE"],
        "sampleOrigin": ["FFPE"],
        "genePanel": "IMPACT505",
        "baitSet": "IMPACT505_BAITS",
        "libraries": [{"runs": [{"runMode": "NovaSeq S2", "runDate": "2023-04-10"}]}],
    }
    payload = {
        "igoRequestId": "99908_C",
        "status": {"validationStatus": True},
        "samples": [tumor_entry],
    }

    result = handle_new_request(db, payload)
    assert result["status"] == "ok"
    assert result["summary"]["samples_created"] == 1
    assert result["summary"]["pairings_created"] == 1

    rows = _pairing_rows(db)
    assert len(rows) == 1
    pairing = rows[0]
    assert pairing.trigger_type == "automatic"
    assert pairing.latest is True
    tumor = _sample_by_primary_id(db, "99908_C_1")
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id


def test_ingesting_the_same_request_twice_is_idempotent(db, fixture_payload):
    first = handle_new_request(db, fixture_payload)
    assert first["summary"]["samples_created"] == 5
    rows_after_first = len(list(db.scalars(select(Sample)).all()))
    sample = _sample_by_primary_id(db, "99908_C_17")
    blob_before = sample.additional_metadata["requestMetadata"]
    status_before = sample.additional_metadata["requestStatus"]

    second = handle_new_request(db, fixture_payload)
    assert second["status"] == "ok"
    assert second["summary"]["samples_created"] == 0
    assert second["summary"]["samples_updated"] == 5

    assert len(list(db.scalars(select(Sample)).all())) == rows_after_first == 5
    sample = _sample_by_primary_id(db, "99908_C_17")
    assert sample.additional_metadata["requestMetadata"] == blob_before
    assert sample.additional_metadata["requestStatus"] == status_before


def test_bait_set_without_logic_is_registered_but_unpaired(db):
    entry = {
        "primaryId": "99999_A_1",
        "cmoSampleName": "C-XXXXX1-X001-d01",
        "cmoPatientId": "C-XXXXX1",
        "sampleType": "Biopsy",
        "tumorOrNormal": "Tumor",
        "genePanel": "MYSTERY_PANEL",
        "baitSet": "MYSTERY_PANEL_BAITS",
        "libraries": [{"runs": [{"runMode": "NovaSeq S2", "runDate": "2023-04-10"}]}],
    }
    payload = {"igoRequestId": "99999_A", "samples": [entry]}
    result = handle_new_request(db, payload)
    assert result["status"] == "ok"

    sample = _sample_by_primary_id(db, "99999_A_1")
    assert sample is not None
    assert sample.file_group == "SMILE"
    assert _pairing_rows(db) == []
    assert result["summary"]["uncovered"] == [
        {
            "primary_id": "99999_A_1",
            "bait_set": "MYSTERY_PANEL_BAITS",
            "reason": "no pairing logic for bait set 'MYSTERY_PANEL_BAITS'",
        }
    ]


def test_missing_required_fields_skips_the_row(db):
    entry = {
        "primaryId": "99999_B_1",
        "cmoSampleName": "C-XXXXX2-X001-d01",
        "cmoPatientId": "C-XXXXX2",
        "sampleType": "Biopsy",
        "tumorOrNormal": "Tumor",
        "baitSet": "IMPACT505_BAITS",
        # genePanel missing
    }
    payload = {"igoRequestId": "99999_B", "samples": [entry]}
    result = handle_new_request(db, payload)
    assert result["status"] == "ok"
    assert _sample_by_primary_id(db, "99999_B_1") is None
    assert result["summary"]["skipped"] == [
        {"primary_id": "99999_B_1", "errors": ["genePanel missing"]}
    ]
