"""Tests for cmo-sample-update NATS ingestion (handle_sample_update).

Pattern per the suite: in-memory SQLite, direct handler calls, no
TestClient.
"""
import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import PairingLogic, PairingTable, Sample
from nats_ingest import handle_new_request, handle_sample_update

NEW_REQUEST_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "nats" / "99908_C_new_request.json"
)
UPDATE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "nats" / "99908_C_1_update_sample.json"
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
def new_request_payload():
    return json.loads(NEW_REQUEST_FIXTURE.read_text())


@pytest.fixture
def update_payload():
    return json.loads(UPDATE_FIXTURE.read_text())


def _sample_by_primary_id(db, primary_id):
    return db.scalar(select(Sample).where(Sample.primary_id == primary_id))


def _pairing_rows(db):
    return list(db.scalars(select(PairingTable)).all())


def _register_single_sample(db, request_id: str, patient_id: str) -> None:
    """Register one TUMOR sample under a request via the ingest path."""
    entry = {
        "primaryId": f"{request_id}_1",
        "cmoSampleName": f"{patient_id}-X001-d01",
        "cmoPatientId": patient_id,
        "sampleType": "Biopsy",
        "tumorOrNormal": "Tumor",
        "genePanel": "IMPACT505",
        "baitSet": "IMPACT505_BAITS",
        "libraries": [{"runs": [{"runMode": "NovaSeq S2", "runDate": "2023-04-10"}]}],
    }
    result = handle_new_request(db, {"igoRequestId": request_id, "samples": [entry]})
    assert result["status"] == "ok"
    assert result["summary"]["samples_created"] == 1


def test_update_refreshes_sample_metadata(db, new_request_payload, update_payload):
    """Happy path: the vendored update flips 99908_C_1 Normal -> Tumor and
    refreshes its sample-level metadata in place."""
    handle_new_request(db, new_request_payload)
    result = handle_sample_update(db, update_payload)
    assert result["status"] == "ok"
    summary = result["summary"]
    assert summary["sample_id"] == "99908_C_1"
    assert summary["request_id"] == "99908_C"
    assert "tumor_or_normal" in summary["fields_changed"]
    # The flip is pairing-relevant, so the re-pairing path ran — but no
    # matching normal exists in this DB, so nothing was paired.
    assert summary["pairing"]["triggered"] is True
    assert summary["pairing"]["pairing_created"] is False

    sample = _sample_by_primary_id(db, "99908_C_1")
    assert sample.tumor_or_normal == "TUMOR"
    assert sample.request_id == "99908_C"
    assert sample.patient_id == "C-PPPPP6"
    assert sample.gene_panel == "IMPACT505"
    assert sample.bait_set == "IMPACT505_BAITS"
    assert sample.cmo_sample_name == update_payload["latestSampleMetadata"]["cmoSampleName"]

    latest = update_payload["latestSampleMetadata"]
    meta = sample.additional_metadata
    # Request-level blob from the new-request ingest is preserved.
    assert meta["requestMetadata"] == json.loads(new_request_payload["requestJson"])
    assert meta["requestStatus"] == new_request_payload["status"]
    # Latest-sample-metadata keys merged (same-named keys replaced).
    assert meta["importDate"] == latest["importDate"]
    assert meta["status"] == latest["status"]
    assert meta["qcReports"] == latest["qcReports"]
    assert meta["additionalProperties"] == latest["additionalProperties"]
    assert meta["tissueLocation"] == latest["tissueLocation"]
    # Full SMILE metadata history preserved verbatim.
    assert meta["sampleMetadataList"] == update_payload["sampleMetadataList"]
    assert len(meta["sampleMetadataList"]) == 2

    # Nothing else moved: no pairings, still five samples, others untouched.
    assert _pairing_rows(db) == []
    assert len(list(db.scalars(select(Sample)).all())) == 5
    other = _sample_by_primary_id(db, "99908_C_17")
    assert other.tumor_or_normal == "NORMAL"
    assert other.patient_id == "C-PPPP16"


def test_update_with_unknown_request_is_skipped(db, caplog, update_payload):
    """Unknown sample AND unknown request: nothing is created or changed,
    a WARN with the agreed message is logged."""
    _register_single_sample(db, "99999_A", "C-XXXXX1")

    payload = copy.deepcopy(update_payload)
    payload["latestSampleMetadata"]["primaryId"] = "99999_Z_1"
    payload["latestSampleMetadata"]["igoRequestId"] = "99999_Z"
    payload["latestSampleMetadata"]["additionalProperties"]["igoRequestId"] = "99999_Z"

    with caplog.at_level(logging.WARNING, logger="nats_ingest"):
        result = handle_sample_update(db, payload)

    assert result["status"] == "skipped"
    assert "unknown request 99999_Z for sample 99999_Z_1" in result["detail"]
    assert [s.primary_id for s in db.scalars(select(Sample)).all()] == ["99999_A_1"]
    assert _pairing_rows(db) == []
    assert any(
        "unknown request 99999_Z for sample 99999_Z_1" in record.message
        for record in caplog.records
    )


def test_update_for_unregistered_sample_under_known_request_is_skipped(db, caplog):
    """The request is known (another sample carries it) but the sample is
    not registered: no creation, WARN with the agreed message."""
    _register_single_sample(db, "99999_A", "C-XXXXX1")

    payload = {
        "smileSampleId": "00000000-0000-0000-0000-000000000000",
        "primarySampleAlias": "MOCKREQUEST1_Z_9",
        "sampleClass": "Tumor",
        "sampleCategory": "Patient Derived",
        "datasource": "smile",
        "sampleAliases": [
            {"value": "99999_A_99", "namespace": "igoId"},
            {"value": "00000000-0000-0000-0000-000000000000", "namespace": "investigatorId"},
        ],
        "patient": {
            "smilePatientId": "11111111-1111-1111-1111-111111111111",
            "patientAliases": [
                {"value": "C-XXXXX1", "namespace": "cmoId"},
                {"value": "11111111-1111-1111-1111-111111111111", "namespace": "smileId"},
            ],
            "cmoPatientId": {"value": "C-XXXXX1", "namespace": "cmoId"},
        },
        "latestSampleMetadata": {
            "primaryId": "99999_A_99",
            "cmoPatientId": "C-XXXXX1",
            "investigatorSampleId": "00000000-0000-0000-0000-000000000000",
            "cmoSampleName": "C-XXXXX1-X009-d09",
            "sampleName": "99999_A_99",
            "igoRequestId": "99999_A",
            "importDate": "2023-04-26",
            "species": "Human",
            "sex": "Female",
            "tumorOrNormal": "Tumor",
            "sampleType": "Biopsy",
            "preservation": "FFPE",
            "sampleClass": "Tumor",
            "sampleOrigin": "FFPE",
            "tissueLocation": "Head/Neck",
            "genePanel": "IMPACT505",
            "baitSet": "IMPACT505_BAITS",
            "igoComplete": True,
            "qcReports": [],
            "libraries": [{"runs": [{"runMode": "NovaSeq S2", "runDate": "2023-04-26"}]}],
            "cmoSampleIdFields": [],
            "additionalProperties": {"igoRequestId": "99999_A"},
            "status": {"status": "Completed"},
        },
        "sampleMetadataList": [],
    }

    with caplog.at_level(logging.WARNING, logger="nats_ingest"):
        result = handle_sample_update(db, payload)

    assert result["status"] == "skipped"
    assert (
        "sample 99999_A_99 not registered under existing request 99999_A" in result["detail"]
    )
    assert [s.primary_id for s in db.scalars(select(Sample)).all()] == ["99999_A_1"]
    assert _pairing_rows(db) == []
    assert any(
        "not registered under existing request 99999_A" in record.message
        for record in caplog.records
    )


def test_flip_to_tumor_triggers_pairing(db, new_request_payload, update_payload):
    """A pairing-relevant field change (Normal -> Tumor) re-runs the
    ingestion pairing path and auto-pairs the tumor with the seeded
    same-patient normal."""
    handle_new_request(db, new_request_payload)

    # The counterpart normal for 99908_C_1: same patient, gene panel,
    # bait set and run mode; preservation/origin combination that the
    # argos gate resolves as "frozen".
    normal = Sample(
        primary_id="99908_B_1",
        cmo_sample_name="99908_B_1_1_1_1",
        patient_id="C-PPPPP6",
        request_id="99908_B",
        sample_type="other",
        gene_panel="IMPACT505",
        bait_set="IMPACT505_BAITS",
        tumor_or_normal="NORMAL",
        preservation="Frozen",
        sample_origin="Fresh or Frozen",
        run_mode="NovaSeq S2",
        sequencing_date=datetime(2023, 4, 1, tzinfo=timezone.utc),
    )
    db.add(normal)
    db.commit()

    # The vendored fixture's latestSampleMetadata carries
    # preservation "Frozen" + sampleOrigin "Tissue"; the argos preservation
    # gate (resolve_preservation) marks that combination as unresolvable,
    # which would block every pair. Same payload shape with a resolvable
    # origin so the flip exercises the pairing path end to end.
    payload = copy.deepcopy(update_payload)
    payload["latestSampleMetadata"]["sampleOrigin"] = "Fresh or Frozen"

    result = handle_sample_update(db, payload)
    assert result["status"] == "ok"
    pairing_summary = result["summary"]["pairing"]
    assert pairing_summary["triggered"] is True
    assert pairing_summary["trigger_type"] == "automatic"
    assert pairing_summary["pairing_created"] is True
    assert pairing_summary["normal_id"] == str(normal.id)

    rows = _pairing_rows(db)
    assert len(rows) == 1
    pairing = rows[0]
    assert pairing.trigger_type == "automatic"
    assert pairing.latest is True
    tumor = _sample_by_primary_id(db, "99908_C_1")
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == normal.id
