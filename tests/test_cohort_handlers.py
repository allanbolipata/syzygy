"""Scenario tests for the cohort submit/update NATS handlers (T8).

Covers the locked pipeline invariants end to end (handler -> schema
validation (T3) -> classification (T5) -> CRF write (T4) -> outbox
staging (T6)):

1. fully valid cohort -> CRF written, pass report, outbox row staged in
   the same commit;
2. one tumorNotFound -> report only, NO CRF file (no partial CRFs);
3. schema-invalid payload (unknown top-level key) ->
   jsonSchemaValidated=false report, no crash;
4. top-level piName -> stripped by T3 (legacy Cohort.__init__ semantics),
   so it is NOT a schema failure (pins the actual T3 API);
5. update on an existing CRF -> header replaced, data rows preserved;
6. CRF write failure (read-only dir) -> raises (500 via the router),
   no pass report, no outbox row (the already-committed pairing row
   survives — trigger_pairing commits internally; harmless for the
   re-send).

Conventions: in-memory SQLite + StaticPool, direct handler calls (no
TestClient), monkeypatch.setattr(settings, ...) pointing crf_output_dir
at a tmp dir.
"""

import json
import os
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import nats_ingest
from database import Base, settings
from models import (
    BaitSet,
    PairingLogic,
    PairingTable,
    PendingNatsPublish,
    Sample,
)
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


# ---------------------------------------------------------------------------
# Seed + payload helpers
# ---------------------------------------------------------------------------


def _add_sample(
    db,
    primary_id,
    cmo,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    patient__cmo="C-10001",
    gene_panel="PANEL-A",
    bait_set="BAIT-1",
    sample_type="tissue",
    **kw,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo,
        patient__cmo=patient__cmo,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        **kw,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _tumor(db, primary_id="s-10001_A", cmo="C-10001-X001-d01", **kw):
    """A tumor whose DB primary_id carries the legacy ``s-`` prefix, so
    the tests exercise id-space normalization (request sends
    ``10001_A``)."""
    return _add_sample(
        db, primary_id, cmo, tumor_or_normal=TumorOrNormal.TUMOR.value, **kw
    )


def _setup_logic(db, bait="BAIT-1", name="TEMPO", active=True):
    """Active pairing logic + BaitSet row mapping ``bait`` to it."""
    logic = PairingLogic(name=name, active=active)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    db.add(BaitSet(bait_set=bait, pairing_logic_id=logic.id))
    db.commit()
    return logic


def _entry(**kw):
    entry = {"primaryId": "10001_A", "cmoId": "C-10001-X001-d01"}
    entry.update(kw)
    return entry


def _payload(cohort_id="COH-1", samples=None, **kw):
    payload = {
        "cohortId": cohort_id,
        "endUsers": ["analyst@example.org"],
        "pmUsers": ["pm@example.org"],
        "projectTitle": "Handler Test Cohort",
        "samples": samples if samples is not None else [_entry()],
    }
    payload.update(kw)
    return payload


def _outbox_rows(db):
    return db.scalars(select(PendingNatsPublish)).all()


def _assert_outbox(db, cohort_id, expected_report):
    rows = _outbox_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.cohort_id == cohort_id
    assert row.subject == settings.nats_cohort_result_subject
    assert row.published_at is None
    assert json.loads(row.report_json) == expected_report


# ---------------------------------------------------------------------------
# 1. fully valid cohort
# ---------------------------------------------------------------------------


def test_submit_fully_valid_writes_crf_stages_report_and_outbox(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    result = nats_ingest.cohort_submit_handler(db, _payload())

    assert result["status"] == "ok"
    report = result["report"]
    assert report["cohortId"] == "COH-1"
    assert report["jsonSchemaValidated"] is True
    assert report["passesCheck"] is True
    assert report.get("samples", []) == []

    # CRF written into the tmp dir with the resolved pair as its only row.
    crf = tmp_path / "crf" / "COH-1.cohort.txt"
    assert crf.is_file()
    lines = crf.read_text().splitlines()
    assert lines[0] == "#endUsers:analyst@example.org"
    assert lines[1] == "#pmUsers:pm@example.org"
    assert lines[2] == "#projectTitle:Handler Test Cohort"
    assert lines[3] == "#type:investigator"
    assert lines[4] == "#TUMOR_ID\tNORMAL_ID"
    assert lines[5] == "C-10001-X001-d01\tC-10001-N001-d01"
    assert len(lines) == 6

    # The pass report was staged for publishing (same commit as the pair).
    _assert_outbox(db, "COH-1", report)
    pairing = db.scalars(select(PairingTable)).first()
    assert pairing is not None and pairing.latest is True


# ---------------------------------------------------------------------------
# 2. one tumorNotFound -> report only, no CRF
# ---------------------------------------------------------------------------


def test_submit_tumor_not_found_is_report_only_no_crf(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    # No Sample row for the tumor: it lands in tumorNotFound.

    result = nats_ingest.cohort_submit_handler(
        db,
        _payload(samples=[_entry(primaryId="99999_A", cmoId="C-99999-X001-d01")]),
    )

    assert result["status"] == "ok"
    report = result["report"]
    assert report["cohortId"] == "COH-1"
    assert report["jsonSchemaValidated"] is True
    assert report["passesCheck"] is False
    assert report["samples"] == [
        {"primaryId": "99999_A", "cmoId": "C-99999-X001-d01", "tumorNotFound": True}
    ]

    # No CRF at all — not even the directory.
    assert not (tmp_path / "crf").exists()
    assert _outbox_rows(db) and json.loads(_outbox_rows(db)[0].report_json) == report


# ---------------------------------------------------------------------------
# 3. schema-invalid payload -> jsonSchemaValidated=false report, no crash
# ---------------------------------------------------------------------------


def test_submit_schema_invalid_top_level_key_is_schema_failure_report(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    payload = _payload()
    payload["bogusKey"] = "x"  # rejected: top-level additionalProperties:false

    result = nats_ingest.cohort_submit_handler(db, payload)

    assert result["status"] == "ok"
    report = result["report"]
    assert report == {
        "cohortId": "COH-1",
        "jsonSchemaValidated": False,
        "passesCheck": False,
    }
    assert not (tmp_path / "crf").exists()
    _assert_outbox(db, "COH-1", report)


def test_submit_pi_name_stripped_by_schema_layer_not_schema_failure(db, tmp_path, monkeypatch):
    """T3 strips top-level piName before request-schema validation (legacy
    Cohort.__init__ semantics — see tests/test_cohort_schema.py), so a
    piName payload must NOT produce a jsonSchemaValidated=false report; it
    proceeds to classification. Pins the actual T3 API against a regression
    that would reject piName at the handler level."""
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    payload = _payload()
    payload["piName"] = "Dr. Investigator"

    result = nats_ingest.cohort_submit_handler(db, payload)

    assert result["status"] == "ok"
    report = result["report"]
    assert report["jsonSchemaValidated"] is True
    # Classified (no tumor seeded), not schema-rejected.
    assert report["passesCheck"] is False
    assert report["samples"] == [
        {"primaryId": "10001_A", "cmoId": "C-10001-X001-d01", "tumorNotFound": True}
    ]
    assert not (tmp_path / "crf").exists()


# ---------------------------------------------------------------------------
# 4. update on an existing CRF -> header replaced, data rows preserved
# ---------------------------------------------------------------------------


def test_update_existing_crf_replaces_header_preserves_rows(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    # First a submit writes the full CRF...
    submit = nats_ingest.cohort_submit_handler(db, _payload())
    assert submit["report"]["passesCheck"] is True

    # ...then an update with a changed header.
    update_payload = _payload()
    update_payload["endUsers"] = ["newanalyst@example.org"]
    result = nats_ingest.cohort_update_handler(db, update_payload)

    assert result["status"] == "ok"
    assert result["report"]["passesCheck"] is True
    assert result["report"]["jsonSchemaValidated"] is True

    lines = (tmp_path / "crf" / "COH-1.cohort.txt").read_text().splitlines()
    assert lines[0] == "#endUsers:newanalyst@example.org"  # header replaced
    assert lines[1] == "#pmUsers:pm@example.org"
    data_rows = [line for line in lines if not line.startswith("#")]
    assert data_rows == ["C-10001-X001-d01\tC-10001-N001-d01"]  # preserved verbatim

    # Both reports staged (submit + update), newest is the update's.
    rows = _outbox_rows(db)
    assert len(rows) == 2
    assert json.loads(rows[-1].report_json) == result["report"]


def test_update_missing_crf_falls_back_to_full_write(db, tmp_path, monkeypatch):
    """An update for a cohort with no existing CRF writes the full CRF
    (idempotent overwrite — the manual re-send recovery path)."""
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    result = nats_ingest.cohort_update_handler(db, _payload())

    assert result["status"] == "ok"
    assert result["report"]["passesCheck"] is True
    lines = (tmp_path / "crf" / "COH-1.cohort.txt").read_text().splitlines()
    data_rows = [line for line in lines if not line.startswith("#")]
    assert data_rows == ["C-10001-X001-d01\tC-10001-N001-d01"]


# ---------------------------------------------------------------------------
# 5. CRF write failure -> raises (500 via router), no pass report
# ---------------------------------------------------------------------------


def test_submit_crf_write_failure_raises_no_pass_report_no_outbox(db, tmp_path, monkeypatch):
    ro_dir = tmp_path / "crf"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o555)  # read-only: mkstemp inside it must fail
    monkeypatch.setattr(settings, "crf_output_dir", str(ro_dir))
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    try:
        with pytest.raises(OSError):
            nats_ingest.cohort_submit_handler(db, _payload())
    finally:
        os.chmod(ro_dir, 0o755)  # allow tmp cleanup

    # No CRF, no pass report, no outbox row. The pairing row survives:
    # pairing_service.trigger_pairing commits internally, so it was already
    # durable when the write failed — harmless, the re-send's classification
    # treats that current match as winning.
    assert not (ro_dir / "COH-1.cohort.txt").exists()
    assert _outbox_rows(db) == []
    assert db.scalars(select(PairingTable)).first() is not None


# ---------------------------------------------------------------------------
# dispatch wiring + non-object payloads
# ---------------------------------------------------------------------------


def test_dispatch_routes_cohort_subjects(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crf_output_dir", str(tmp_path / "crf"))
    _setup_logic(db)
    _tumor(db)
    _add_sample(db, "NORMAL-A", "C-10001-N001-d01")

    for subject, handler in (
        (settings.nats_cohort_submit_subject, nats_ingest.cohort_submit_handler),
        (settings.nats_cohort_update_subject, nats_ingest.cohort_update_handler),
    ):
        result = nats_ingest.dispatch(db, subject, _payload())
        assert result["status"] == "ok"
        assert result["report"]["cohortId"] == "COH-1"
        assert result["report"]["passesCheck"] is True


def test_cohort_handlers_reject_non_object_payloads(db):
    bad_payload: Any = ["not", "an", "object"]
    for handler in (nats_ingest.cohort_submit_handler, nats_ingest.cohort_update_handler):
        result = handler(db, bad_payload)
        assert result["status"] == "error"
        assert "JSON object" in result["detail"]
        assert "report" not in result
