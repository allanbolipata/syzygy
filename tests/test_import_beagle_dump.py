"""Unit tests for runId aggregation in scripts/import_beagle_dump.py.

Beagle filemetadata rows carry a ``runId`` per file (e.g.
``"zzmachine_A_L001_R1"``). The importer must aggregate the distinct
values per sample (across all of its file rows, including anonymous rows
that attach via igoRequestId) and persist them verbatim under
``additional_metadata["run_ids"]`` — normalization (lowercase, prefix
split) happens at pairing time, not at write time.

Also covers the DMP ACELA envelope (DA3): DMP filemetadata rows may be
``{path, metadata, pathChecks}`` envelopes; the importer flattens each
record through ``flatten_acela_record`` (the single flatten point) so DMP
rows store ``bam_path``/``bam_exists``/``path_checks`` in
``additional_metadata`` — byte-equal, on shared keys, to what the NATS
dmp-sample path stores for the same record.

All values are synthetic; no real runId or sample identifiers.
"""

import copy
import json
import os
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from import_beagle_dump import (
    build_samples,
    merge_dmp_samples,
    merge_samples,
    to_sample_kwargs,
    upsert_samples,
)

from syzygy.database import Base
from syzygy.models import Sample
from syzygy.nats_ingest import handle_dmp_sample


def test_merge_samples_aggregates_run_ids_deduped():
    """runId values from a sample's file rows are aggregated into a
    distinct, order-preserving list; samples without a runId get no
    run_ids key."""
    rows = [
        ("LIMS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L001_R1"}),
        ("LIMS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L001_R1"}),  # duplicate
        ("ACCESS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L002_R2"}),
        ("LIMS", {"primaryId": "P-2", "tumorOrNormal": "Normal", "runId": "zztank_B_L001_R1"}),
        ("LIMS", {"primaryId": "P-3", "tumorOrNormal": "Tumor"}),
    ]
    samples = merge_samples(rows)
    assert samples["P-1"]["kwargs"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L002_R2",
    ]
    assert samples["P-2"]["kwargs"]["run_ids"] == ["zztank_B_L001_R1"]
    assert "run_ids" not in samples["P-3"]["kwargs"]


def test_merge_samples_anonymous_run_ids_follow_target():
    """An anonymous row (no primaryId) carrying a runId attaches to the
    sample whose igoRequestId matches, and its runId accumulates into
    that sample's aggregated list."""
    rows = [
        (
            "LIMS",
            {
                "primaryId": "P-1",
                "tumorOrNormal": "Tumor",
                "igoRequestId": "IGO-1",
                "runId": "zzmachine_A_L001_R1",
            },
        ),
        (
            "ACCESS",
            {
                "igoRequestId": "IGO-1",
                "tumorOrNormal": "Tumor",
                "runId": "zzmachine_A_L003_R3",
            },
        ),
    ]
    samples = merge_samples(rows)
    assert "P-1" in samples
    assert samples["P-1"]["kwargs"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L003_R3",
    ]


def test_to_sample_kwargs_stores_run_ids_in_additional_metadata():
    """The aggregated run_ids land in additional_metadata["run_ids"],
    verbatim (no normalization at write time) and deduped; run_ids must
    NOT leak as a top-level Sample kwarg."""
    merged = {
        "primaryId": "P-1",
        "tumorOrNormal": "Tumor",
        "cmoSampleName": "P1 name",
        "genePanel": "HC_IMPACT",
        "baitSet": "IMPACT505_BAITS",
        "run_ids": ["zzmachine_A_L001_R1", "zzmachine_A_L002_R2", "zzmachine_A_L001_R1"],
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert kwargs["additional_metadata"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L002_R2",
    ]
    assert "run_ids" not in kwargs


def test_to_sample_kwargs_run_ids_verbatim_no_normalization():
    """Raw runId casing is preserved (lowercasing happens at pairing
    time, not at write time)."""
    merged = {
        "primaryId": "P-2",
        "tumorOrNormal": "Normal",
        "cmoSampleName": "P2 name",
        "run_ids": ["ZzTank_S1_L005_R1"],
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert kwargs["additional_metadata"]["run_ids"] == ["ZzTank_S1_L005_R1"]


def test_to_sample_kwargs_without_run_ids_omits_key():
    """A sample record without runIds gets no run_ids key at all (not
    an empty list) — legacy rows keep their additional_metadata shape."""
    merged = {
        "primaryId": "P-3",
        "tumorOrNormal": "Tumor",
        "cmoSampleName": "P3 name",
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert "run_ids" not in (kwargs.get("additional_metadata") or {})


# ---------------------------------------------------------------------------
# DMP ACELA envelope (DA3): filemetadata rows may be ACELA envelopes
# {path, metadata, pathChecks}; the importer passes each record through
# flatten_acela_record (the single flatten point) so DMP rows store
# bam_path/bam_exists/path_checks in additional_metadata — the same
# vocabulary the NATS dmp-sample handler stores. All values synthetic.
# ---------------------------------------------------------------------------


def _dmp_envelope(**overrides):
    """A synthetic ACELA DMP envelope row (file_system_filemetadata shape)."""
    record = {
        "path": "/dmp/mirror/impact468/I/N/isfake01.bam",
        "metadata": {
            "sample": "P-00ISFAK01-T01-IM6",
            "type": "T",
            "assay": "IM6",
            "cmo_assay": "IMPACT468",
            "anon_id": "isfak01t01im6",
            "patient": {"cmo": "ISFAK01", "dmp": "P-00ISFAK01"},
            "coverage": 600,
            "project_name": "ISFAKE0000000101",
            "part_c_consent": 1,
            "external_id": "EXT-ISFAK01-T01",
        },
        "pathChecks": {
            "mirror": {"status": "OK", "size_bytes": 12345, "checked_at": "2026-11-20T00:00:00"},
            "dms": {"status": "OK", "size_bytes": 12345, "checked_at": "2026-11-20T00:00:00"},
        },
    }
    record.update(overrides)
    return record


def test_merge_dmp_samples_flattens_envelope_rows():
    """Envelope rows are unwrapped through flatten_acela_record before
    the merge: keyed by metadata.sample, carrying bam_path/path_checks/
    bam_exists; first-non-null-wins across a sample's files; legacy flat
    rows pass through unchanged."""
    rows = [
        ("DMP BAMs", _dmp_envelope()),
        # Second file for the same sample, later pathCheck reports MISSING:
        # first-non-null-wins keeps the first row's bam values.
        (
            "DMP BAMs",
            _dmp_envelope(
                path="/dmp/mirror/impact468/I/N/isfake01b.bam",
                pathChecks={
                    "mirror": {"status": "MISSING", "size_bytes": 0, "checked_at": "t2"},
                },
            ),
        ),
        # A legacy flat row (no envelope) still merges under its sample id.
        (
            "DMP BAMs",
            {
                "sample": "P-00ISFAK02-T01-IM6",
                "type": "T",
                "assay": "IM6",
                "cmo_assay": "IMPACT468",
                "coverage": 300,
            },
        ),
    ]
    samples = merge_dmp_samples(rows)
    assert set(samples) == {"P-00ISFAK01-T01-IM6", "P-00ISFAK02-T01-IM6"}
    k1 = samples["P-00ISFAK01-T01-IM6"]["kwargs"]
    # Envelope unwrapped: no nested 'metadata' key; flat vocabulary + bam keys.
    assert "metadata" not in k1
    assert k1["sample"] == "P-00ISFAK01-T01-IM6"
    assert k1["bam_path"] == "/dmp/mirror/impact468/I/N/isfake01.bam"  # first row wins
    assert k1["bam_exists"] == 1
    assert k1["path_checks"]["dms"]["status"] == "OK"
    assert k1["coverage"] == 600
    k2 = samples["P-00ISFAK02-T01-IM6"]["kwargs"]
    # Legacy flat row: untouched, no bam keys added.
    assert "bam_path" not in k2
    assert "bam_exists" not in k2
    assert "path_checks" not in k2
    assert k2["coverage"] == 300


def test_build_samples_dmp_envelope_row_carries_bam_keys():
    """The importer pipeline (merge -> build) stores bam_path/bam_exists/
    path_checks in additional_metadata for an envelope row; the envelope
    keys themselves do not leak as Sample kwargs; consent stays the
    placeholder False (part_c_consent is stored verbatim, never wired)."""
    rows = [("DMP BAMs", _dmp_envelope())]
    samples = merge_dmp_samples(rows)
    dmp_list, skipped, _ = build_samples(samples, is_dmp=True)
    assert skipped == []
    kw = dmp_list[0]
    assert kw["primary_id"] == "P-00ISFAK01-T01-IM6"
    assert kw["file_group"] == "DMP BAMs"
    assert kw["consent"] is False
    meta = kw["additional_metadata"]
    assert meta["bam_path"] == "/dmp/mirror/impact468/I/N/isfake01.bam"
    assert meta["bam_exists"] == 1
    assert meta["path_checks"]["mirror"] == {
        "status": "OK",
        "size_bytes": 12345,
        "checked_at": "2026-11-20T00:00:00",
    }
    # part_c_consent passes through verbatim like any other key.
    assert meta["part_c_consent"] == 1
    assert meta["coverage"] == 600
    # Envelope keys are not column-mapped and must not leak as kwargs.
    assert "path" not in kw
    assert "pathChecks" not in kw
    assert "metadata" not in kw


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


def test_dump_stored_metadata_byte_equal_to_nats_path(db):
    """For the same record, the importer-stored additional_metadata is
    byte-equal to what the NATS dmp-sample path stores, key for key on
    the shared keys — one vocabulary (DA3 accept)."""
    envelope = _dmp_envelope()
    # Importer path: the same pure pipeline main() runs.
    samples = merge_dmp_samples([("DMP BAMs", copy.deepcopy(envelope))])
    dmp_list, skipped, _ = build_samples(samples, is_dmp=True)
    assert skipped == []
    dump_meta = dmp_list[0]["additional_metadata"] or {}
    # NATS path: the same record on the dmp-sample subject.
    assert handle_dmp_sample(db, copy.deepcopy(envelope))["status"] == "ok"
    row = db.scalar(select(Sample).where(Sample.primary_id == "P-00ISFAK01-T01-IM6"))
    assert row is not None
    nats_meta = row.additional_metadata or {}
    shared = set(dump_meta) & set(nats_meta)
    assert {"bam_path", "path_checks", "bam_exists", "part_c_consent", "coverage"} <= shared
    for key in shared:
        assert json.dumps(dump_meta[key], sort_keys=True) == json.dumps(
            nats_meta[key], sort_keys=True
        ), key


def test_dump_dmp_rerun_idempotent(db):
    """Re-running the DMP import over the same envelope rows upserts in
    place: one row, stable bam metadata, no duplicates."""
    rows = [("DMP BAMs", _dmp_envelope())]

    def build():
        out, skipped, _ = build_samples(merge_dmp_samples(rows), is_dmp=True)
        assert skipped == []
        return out

    created, updated = upsert_samples(db, build())
    assert (created, updated) == (1, 0)
    # Re-run: same rows, same values — updated in place, no new row.
    created2, updated2 = upsert_samples(db, build())
    assert (created2, updated2) == (0, 1)
    assert db.scalar(select(func.count(Sample.id))) == 1
    row = db.scalar(select(Sample).where(Sample.primary_id == "P-00ISFAK01-T01-IM6"))
    assert row is not None
    assert row.file_group == "DMP BAMs"
    meta = row.additional_metadata
    assert meta["bam_exists"] == 1
    assert meta["bam_path"] == "/dmp/mirror/impact468/I/N/isfake01.bam"
    assert meta["path_checks"]["mirror"]["status"] == "OK"
