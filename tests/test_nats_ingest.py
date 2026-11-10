"""NATS ingest foundation tests.

Covers the Settings defaults, subject dispatch, and the internal ingest
endpoint's auth/422 behavior (router functions called directly, per the
suite's pattern). The last test exercises a real round trip against a
live dev NATS server and is skipped when none answers at
127.0.0.1:4222 with the MDB_STREAM stream present.
"""
import json
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
from database import Base, Settings
from models import Sample
from nats_ingest import dispatch
from routers.nats_ingest import NatsIngestIn, ingest_nats
from sqlalchemy import select

NEW_REQUEST = "MDB_STREAM.server.cpt-gateway.cmo-new-request"
REQUEST_UPDATE = "MDB_STREAM.server.cpt-gateway.cmo-request-update"
SAMPLE_UPDATE = "MDB_STREAM.server.cpt-gateway.cmo-sample-update"
# DMP key data arrives in the syzygy DB record format (ticket 2/8) — the
# exact fields dmp_translator.to_dmp_sample_kwargs maps.
DMP_SAMPLE = "MDB_STREAM.server.dmp-gateway.dmp-sample"
SUBJECTS = {
    "new-request": NEW_REQUEST,
    "request-update": REQUEST_UPDATE,
    "sample-update": SAMPLE_UPDATE,
    "dmp-sample": DMP_SAMPLE,
}
LIVE_NATS_URL = "nats://127.0.0.1:4222"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nats"


@pytest.fixture(scope="module")
def pg_engine():
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


def test_settings_defaults(monkeypatch):
    for var in ("NATS_INGEST_TOKEN", "NATS_NEW_REQUEST_SUBJECT",
                "NATS_REQUEST_UPDATE_SUBJECT", "NATS_SAMPLE_UPDATE_SUBJECT",
                "NATS_DMP_SAMPLE_SUBJECT"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.nats_ingest_token == ""
    assert s.nats_new_request_subject == NEW_REQUEST
    assert s.nats_request_update_subject == REQUEST_UPDATE
    assert s.nats_sample_update_subject == SAMPLE_UPDATE
    assert s.nats_dmp_sample_subject == DMP_SAMPLE


def test_dispatch_routes_each_production_subject(db, caplog):
    caplog.set_level(logging.INFO)
    # request-update carries a JSON array (beagle ground truth); the other
    # two events carry JSON objects. All three handlers are implemented.
    caplog.clear()
    # An entry without a igoRequestId is a reported missing request, not a
    # crash: dispatch still returns "ok" with the per-entry report.
    result = dispatch(db, REQUEST_UPDATE, [{"id": "x"}])
    assert result["status"] == "ok"
    assert result["summary"]["entries"] == 1
    assert result["summary"]["missing_requests"] == 1
    # A new-request object without samples is a no-op, not an error.
    result = dispatch(db, NEW_REQUEST, {"id": "x"})
    assert result["status"] == "ok"
    # An update without latestSampleMetadata is a reported error.
    result = dispatch(db, SAMPLE_UPDATE, {"id": "x"})
    assert result["status"] == "error"
    assert "latestSampleMetadata" in result["detail"]


def test_dispatch_unknown_subject_is_error(db):
    result = dispatch(db, "MDB_STREAM.server.cpt-gateway.unknown", {})
    assert result["status"] == "error"
    assert "unknown subject" in result["detail"]


# ---------------------------------------------------------------------------
# DMP ingest (ticket 2/8): the payload arrives in the syzygy DB record
# format and is stored as-is (dmp_translator); no key-file-shaped parsing.
# ---------------------------------------------------------------------------

def _dmp_record(**overrides):
    """A DMP record in the exact DB-format (beagle 'DMP BAMs' filemetadata)."""
    record = {
        "sample": "P-7700123-T01-IM6",
        "type": "T",
        "assay": "IM6",
        "cmo_assay": "IMPACT468",
        "anon_id": "ab3f9k2m",
        "patient": {"cmo": "DMPAT12", "dmp": "P-7700123"},
        "coverage": 687,
        "project_name": "poolA",
        "sample_type": "0",
        "external_id": "EXT-7700123-T01",
        "tumor_type": "lung",
        "updated": "2024-04-30T12:00:00",
        "version": 1,
    }
    record.update(overrides)
    return record


def _get_dmp_row(db):
    return db.scalar(select(Sample).where(Sample.primary_id == "P-7700123-T01-IM6"))


def test_dmp_dispatch_routes_db_format_record(db):
    # A record object on the dmp subject routes to the DMP handler (an
    # un-routed subject would report 'unknown subject').
    result = dispatch(db, DMP_SAMPLE, _dmp_record())
    assert result["status"] == "ok"
    assert result["summary"]["action"] == "created"


def test_dmp_full_record_created(db):
    dispatch(db, DMP_SAMPLE, _dmp_record())
    row = _get_dmp_row(db)
    assert row is not None
    assert row.sample_type == "DMP"
    assert row.tumor_or_normal == "TUMOR"
    assert row.patient__dmp == "P-7700123"
    assert row.patient__cmo == "C-DMPAT12"
    assert row.anon_id == "ab3f9k2m"
    assert row.gene_panel == "IMPACT468"
    assert row.bait_set == "IMPACT468"
    assert row.cmo_sample_name == "EXT-7700123-T01"
    # Same group as the beagle dump import assigns, so rows ingested via
    # NATS and via the dump are indistinguishable in that column.
    assert row.file_group == "DMP BAMs"
    # The rest of the record is stored as-is in additional_metadata.
    assert row.additional_metadata["coverage"] == 687
    assert row.additional_metadata["project_name"] == "poolA"
    assert row.additional_metadata["tumor_type"] == "lung"


def test_dmp_partial_record_no_clobber(db):
    dispatch(db, DMP_SAMPLE, _dmp_record())
    # A partial re-send carrying only some fields must update those and
    # leave everything else as-is.
    result = dispatch(db, DMP_SAMPLE, {"sample": "P-7700123-T01-IM6", "coverage": 100})
    assert result["status"] == "ok"
    assert result["summary"]["action"] == "updated"
    row = _get_dmp_row(db)
    assert row.additional_metadata["coverage"] == 100        # updated
    assert row.gene_panel == "IMPACT468"                     # not reset
    assert row.cmo_sample_name == "EXT-7700123-T01"          # not reset to primary_id
    assert row.patient__dmp == "P-7700123"
    assert row.anon_id == "ab3f9k2m"
    assert row.tumor_or_normal == "TUMOR"
    # Metadata keys the partial record does not carry are preserved.
    assert row.additional_metadata["project_name"] == "poolA"
    assert row.additional_metadata["tumor_type"] == "lung"


def test_dmp_partial_explicit_fields_apply_even_without_others(db):
    # Regression: the per-column source check must not skip columns whose
    # source key IS present just because unrelated keys are absent.
    dispatch(db, DMP_SAMPLE, _dmp_record())
    result = dispatch(db, DMP_SAMPLE, {
        "sample": "P-7700123-T01-IM6",
        "patient": {"dmp": "P-7700999"},
        "anon_id": "zz99xx88",
    })
    assert result["status"] == "ok"
    row = _get_dmp_row(db)
    assert row.patient__dmp == "P-7700999"
    assert row.anon_id == "zz99xx88"
    # The absent patient.cmo does not clobber the stored cmo id...
    assert row.patient__cmo == "C-DMPAT12"
    # ...and unrelated columns are untouched.
    assert row.gene_panel == "IMPACT468"
    assert row.cmo_sample_name == "EXT-7700123-T01"


def test_dmp_partial_without_type_backfills_stored(db):
    dispatch(db, DMP_SAMPLE, _dmp_record(type="T"))
    result = dispatch(db, DMP_SAMPLE, {"sample": "P-7700123-T01-IM6", "coverage": 55})
    assert result["status"] == "ok"
    row = _get_dmp_row(db)
    assert row.tumor_or_normal == "TUMOR"
    assert row.additional_metadata["coverage"] == 55


def test_dmp_new_record_without_type_rejected(db):
    rec = _dmp_record()
    del rec["type"]
    result = dispatch(db, DMP_SAMPLE, rec)
    assert result["status"] == "error"
    assert "type" in result["detail"]
    assert _get_dmp_row(db) is None


def test_dmp_invalid_type_rejected(db):
    result = dispatch(db, DMP_SAMPLE, _dmp_record(type="X"))
    assert result["status"] == "error"
    assert _get_dmp_row(db) is None


def test_dmp_missing_sample_id_rejected(db):
    rec = _dmp_record()
    del rec["sample"]
    result = dispatch(db, DMP_SAMPLE, rec)
    assert result["status"] == "error"
    assert "sample" in result["detail"]


def test_dmp_non_object_payload_rejected(db):
    result = dispatch(db, DMP_SAMPLE, [_dmp_record()])
    assert result["status"] == "error"
    assert "JSON object" in result["detail"]


def test_router_dispatch_returns_200_shape(monkeypatch, db, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    body = NatsIngestIn(subject=REQUEST_UPDATE, payload=[{"id": "x"}])
    result = ingest_nats(body, db, x_nats_token="secret")
    assert set(result) == {"subject", "status", "detail"}
    assert result["subject"] == REQUEST_UPDATE
    assert result["status"] == "ok"
    # The entry names no known request: reported in the detail line.
    assert "1 missing" in result["detail"]


def test_router_accepts_list_payload(monkeypatch, db, caplog):
    """request-update bodies are JSON arrays; they must dispatch, not 422."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    body = NatsIngestIn(subject=REQUEST_UPDATE, payload=[{"igoRequestId": "x"}])
    result = ingest_nats(body, db, x_nats_token="secret")
    assert result["status"] == "ok"
    # The named request has no sample rows: reported as missing, nothing
    # created.
    assert "1 missing" in result["detail"]


def test_router_unknown_subject_is_422(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    body = NatsIngestIn(subject="MDB_STREAM.server.cpt-gateway.bogus", payload={})
    with pytest.raises(HTTPException) as exc:
        ingest_nats(body, db, x_nats_token="secret")
    assert exc.value.status_code == 422


def test_router_rejects_everything_when_token_unconfigured(monkeypatch, db):
    """Fail-closed: no token configured => 403 even with a presented header."""
    monkeypatch.setattr(database.settings, "nats_ingest_token", "")
    body = NatsIngestIn(subject=NEW_REQUEST, payload={})
    with pytest.raises(HTTPException) as exc:
        ingest_nats(body, db, x_nats_token="anything")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc_missing:
        ingest_nats(body, db, x_nats_token=None)
    assert exc_missing.value.status_code == 403


def test_router_token_rejects_wrong_or_missing(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    body = NatsIngestIn(subject=NEW_REQUEST, payload={})
    with pytest.raises(HTTPException) as wrong:
        ingest_nats(body, db, x_nats_token="nope")
    assert wrong.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        ingest_nats(body, db, x_nats_token=None)
    assert missing.value.status_code == 403
    # The right token passes auth and dispatches normally.
    result = ingest_nats(body, db, x_nats_token="secret")
    assert result["status"] == "ok"


def _live_nats_available() -> bool:
    """True if a NATS server answers at 127.0.0.1:4222 with MDB_STREAM."""
    try:
        import asyncio

        import nats

        async def probe() -> bool:
            try:
                nc = await nats.connect(servers=[LIVE_NATS_URL])
            except Exception:
                return False
            try:
                await nc.jetstream().stream_info("MDB_STREAM")
                return True
            except Exception:
                return False
            finally:
                await nc.close()

        return asyncio.run(probe())
    except Exception:
        return False


@pytest.mark.skipif(
    not _live_nats_available(),
    reason="no live NATS server with MDB_STREAM at 127.0.0.1:4222",
)
def test_live_nats_round_trip():
    """Publish a vendored fixture to the live dev stream and pull it back."""
    import asyncio

    import nats

    fixture_path = FIXTURE_DIR / "99908_C_new_request.json"
    original = fixture_path.read_bytes()

    async def run() -> bytes:
        nc = await nats.connect(servers=[LIVE_NATS_URL])
        sub = None
        try:
            js = nc.jetstream()
            ack = await js.publish(NEW_REQUEST, original)
            assert ack.stream == "MDB_STREAM"
            sub = await js.pull_subscribe(NEW_REQUEST, stream="MDB_STREAM")
            # Ephemeral consumer, deliver_policy all: our message must be in
            # the fetched batch (compare bytes rather than assuming order).
            msgs = await sub.fetch(batch=64, timeout=10.0)
            for msg in msgs:
                if msg.data == original:
                    return msg.data
            await sub.fetch(batch=64, timeout=5.0)  # drain, then fail below
            raise AssertionError("published message not found in stream")
        finally:
            if sub is not None:
                await sub.unsubscribe()
            await nc.close()

    data = asyncio.run(run())
    assert json.loads(data) == json.loads(original)
