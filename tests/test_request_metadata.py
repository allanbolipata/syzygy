"""Tests for request-level metadata helpers (Option A: metadata on samples)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import Sample
from request_metadata import request_exists, request_samples, update_request_metadata


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


def _make_sample(primary_id, request_id, **extra):
    return Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_1_1_1",
        patient_id="P-1",
        request_id=request_id,
        sample_type="Biopsy",
        gene_panel="IMPACT468",
        bait_set="IMPACT468_BAITS",
        tumor_or_normal="TUMOR",
        **extra,
    )


@pytest.fixture
def seeded(db):
    """Two samples under request A (with pre-existing metadata), one under B."""
    meta_a = {
        "qcReports": [{"sample": "a1", "status": "pass"}],
        "importDate": "2026-01-01T00:00:00Z",
        "sampleMetadataList": [{"key": "k", "value": "v"}],
    }
    a1 = _make_sample("A1", "REQ-A", additional_metadata=meta_a)
    a2 = _make_sample("A2", "REQ-A", additional_metadata={"importDate": "2026-01-02T00:00:00Z"})
    b1 = _make_sample("B1", "REQ-B", additional_metadata={"importDate": "2026-01-03T00:00:00Z"})
    db.add_all([a1, a2, b1])
    db.commit()
    return {"a1": a1, "a2": a2, "b1": b1}


def test_request_samples_and_existence(db, seeded):
    assert {s.primary_id for s in request_samples(db, "REQ-A")} == {"A1", "A2"}
    assert {s.primary_id for s in request_samples(db, "REQ-B")} == {"B1"}
    assert request_exists(db, "REQ-A") is True
    assert request_exists(db, "REQ-B") is True
    assert request_exists(db, "REQ-MISSING") is False
    assert request_samples(db, "REQ-MISSING") == []


def test_update_all_three_keys(db, seeded):
    meta = {"labHead": "Dr. X", "isCmoRequest": True}
    status = {"validationStatus": "valid", "validationReport": {"errors": []}}
    import_date = "2026-02-03T00:00:00Z"

    before_b = dict(seeded["b1"].additional_metadata or {})
    updated = update_request_metadata(
        db, "REQ-A", request_metadata=meta, status=status, import_date=import_date
    )
    assert updated == 2

    db.expire_all()
    for key in ("qcReports", "importDate", "sampleMetadataList"):
        assert seeded["a1"].additional_metadata[key] == {
            "qcReports": [{"sample": "a1", "status": "pass"}],
            "importDate": "2026-01-01T00:00:00Z",
            "sampleMetadataList": [{"key": "k", "value": "v"}],
        }[key]
    for s in (seeded["a1"], seeded["a2"]):
        assert s.additional_metadata["requestMetadata"] == meta
        assert s.additional_metadata["requestStatus"] == status
        assert s.additional_metadata["requestImportDate"] == import_date

    # B sample untouched: same metadata content, no request-level keys.
    assert seeded["b1"].additional_metadata == before_b
    assert "requestMetadata" not in seeded["b1"].additional_metadata
    assert "requestStatus" not in seeded["b1"].additional_metadata
    assert "requestImportDate" not in seeded["b1"].additional_metadata


def test_partial_update_status_only(db, seeded):
    status = {"validationStatus": "invalid", "validationReport": {"errors": ["boom"]}}
    updated = update_request_metadata(db, "REQ-A", status=status)
    assert updated == 2
    db.expire_all()
    for s in (seeded["a1"], seeded["a2"]):
        assert s.additional_metadata["requestStatus"] == status
        assert "requestMetadata" not in s.additional_metadata
        assert "requestImportDate" not in s.additional_metadata
        # Pre-existing keys survive.
        assert s.additional_metadata["importDate"]


def test_unknown_request_returns_zero(db, seeded):
    updated = update_request_metadata(db, "REQ-MISSING", status={"validationStatus": "valid"})
    assert updated == 0
    # Nothing changed anywhere.
    assert seeded["a1"].additional_metadata == {
        "qcReports": [{"sample": "a1", "status": "pass"}],
        "importDate": "2026-01-01T00:00:00Z",
        "sampleMetadataList": [{"key": "k", "value": "v"}],
    }


def test_update_replaces_prior_request_blob(db, seeded):
    first = {"labHead": "Dr. Old"}
    second = {"labHead": "Dr. New"}
    update_request_metadata(db, "REQ-A", request_metadata=first)
    assert seeded["a1"].additional_metadata["requestMetadata"] == first
    update_request_metadata(db, "REQ-A", request_metadata=second)
    assert seeded["a1"].additional_metadata["requestMetadata"] == second
    assert "labHead" not in seeded["a1"].additional_metadata  # no merge of old blob
