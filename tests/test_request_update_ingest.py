"""Tests for cmo-request-update NATS ingestion (handle_request_update).

Pattern per the suite: in-memory SQLite, direct handler calls, no
TestClient.
"""
import copy
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import Sample
from nats_ingest import handle_request_update

REQUEST_UPDATE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "nats" / "99990_D_request_update.json"
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
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def fixture_payload():
    return json.loads(REQUEST_UPDATE_FIXTURE.read_text())


def _seed_sample(db, primary_id, extra_metadata):
    """Direct insert of one TUMOR sample under request 99990_D_2."""
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_1_1_1",
        patient_id="C-99990",
        request_id="99990_D_2",
        sample_type="Biopsy",
        gene_panel="IMPACT468",
        bait_set="IMPACT468_BAITS",
        tumor_or_normal="TUMOR",
        additional_metadata=extra_metadata,
    )
    db.add(sample)
    db.commit()
    return sample


def _all_sample_ids(db):
    return {s.primary_id for s in db.scalars(select(Sample)).all()}


def test_request_update_stamps_all_samples_later_entry_wins(db, fixture_payload):
    """The vendored fixture carries two entries for 99990_D_2 (importDate
    2023-03-08 then 2023-03-22): both samples of the request end with the
    SECOND entry's request-level values, and their own metadata keys are
    preserved."""
    a1 = _seed_sample(
        db,
        "99990_D_2_1",
        {"qcReports": [{"sample": "a1", "status": "pass"}], "importDate": "2023-01-01"},
    )
    a2 = _seed_sample(
        db,
        "99990_D_2_2",
        {"sampleMetadataList": [{"key": "k", "value": "v"}]},
    )

    result = handle_request_update(db, fixture_payload)
    assert result["status"] == "ok"
    summary = result["summary"]
    assert summary["entries"] == 2
    assert summary["requests_updated"] == 2
    assert summary["missing_requests"] == 0
    assert summary["errors"] == 0
    for entry in summary["per_entry"]:
        assert entry["igo_request_id"] == "99990_D_2"
        assert entry["outcome"] == "updated"
        assert entry["samples_updated"] == 2

    second = fixture_payload[1]
    expected_metadata = json.loads(second["requestMetadataJson"])
    for sample in (a1, a2):
        meta = sample.additional_metadata
        # The later (second) entry's blob won over the first.
        assert meta["requestMetadata"] == expected_metadata
        assert meta["requestStatus"] == second["status"]
        assert meta["requestImportDate"] == "2023-03-22"
    # Each sample's own pre-existing metadata keys survived.
    assert a1.additional_metadata["qcReports"] == [{"sample": "a1", "status": "pass"}]
    assert a1.additional_metadata["importDate"] == "2023-01-01"
    assert a2.additional_metadata["sampleMetadataList"] == [{"key": "k", "value": "v"}]


def test_mixed_batch_unknown_request_reported_and_skipped(db, caplog, fixture_payload):
    """One entry for an existing request + one for an unknown igoRequestId:
    the known request is updated on all its samples, the unknown one is
    reported in the result, no rows are created and no exception is raised."""
    a1 = _seed_sample(
        db,
        "99990_D_2_1",
        {"qcReports": [{"sample": "a1", "status": "pass"}]},
    )
    a2 = _seed_sample(db, "99990_D_2_2", {"importDate": "2023-02-02"})

    unknown_entry = {
        "igoRequestId": "99999_Z",
        "requestMetadataJson": json.dumps({"requestId": "99999_Z", "recipe": "IMPACT468"}),
        "importDate": "2023-05-01",
        "status": {"validationStatus": True, "validationReport": ""},
    }
    payload = [copy.deepcopy(fixture_payload[1]), unknown_entry]

    with caplog.at_level(logging.WARNING, logger="nats_ingest"):
        result = handle_request_update(db, payload)

    assert result["status"] == "ok"
    summary = result["summary"]
    assert summary["entries"] == 2
    assert summary["requests_updated"] == 1
    assert summary["missing_requests"] == 1
    assert summary["errors"] == 0
    known, missing = summary["per_entry"]
    assert known["igo_request_id"] == "99990_D_2"
    assert known["outcome"] == "updated"
    assert known["samples_updated"] == 2
    assert missing["igo_request_id"] == "99999_Z"
    assert missing["outcome"] == "missing"

    # The known request was updated on BOTH samples.
    expected_metadata = json.loads(payload[0]["requestMetadataJson"])
    for sample in (a1, a2):
        assert sample.additional_metadata["requestMetadata"] == expected_metadata
        assert sample.additional_metadata["requestStatus"] == payload[0]["status"]
        assert sample.additional_metadata["requestImportDate"] == "2023-03-22"

    # No rows created by the unknown entry; nothing else moved.
    assert _all_sample_ids(db) == {"99990_D_2_1", "99990_D_2_2"}
    assert any("unknown request 99999_Z" in record.message for record in caplog.records)


def test_non_array_payload_is_rejected(db, fixture_payload):
    """Request-update payloads are JSON arrays; anything else is an error
    result, not a crash."""
    result = handle_request_update(db, {"igoRequestId": "99990_D_2"})
    assert result["status"] == "error"
    assert "must be a JSON array" in result["detail"]
    assert _all_sample_ids(db) == set()
