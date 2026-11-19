"""TEMPO export tickets E1+E2: pairing, unpaired & tracker exports.

Spec: Projects/Syzygy/Factory/TEMPO_EXPORT_TICKETS.md. Acceptance:

E1:

- beagle-shaped headers (pairing = the extended 4-column
  ``EXTENDED_HEADER``; unpaired = the 8 beagle conflict-file fields +
  ``Possible Reason?``),
- the pairing export round-trips through
  ``scripts/import_tempo_pairs.py --dry-run`` (the real CLI, driven
  in-process over ``httpx.ASGITransport`` against the app),
- unpaired reasons are non-empty for seeded unmatched tumors,

E2:

- the tracker header is byte-equal to beagle's tracker header
  (``BEAGLE_TRACKER_HEADER`` below, captured from beagle's
  ``create_tracker_file``),
- missing metadata -> empty cells, no exceptions,
- DMP samples put ``primary_id`` in the Collaborator_ID column,

both:

- ``GET /v1/export/tempo/{pairing,unpaired,tracker}`` serve
  ``text/plain`` downloads with beagle's filenames and require auth.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.export.tempo_files import (
    PAIRING_FILENAME,
    TRACKER_FILENAME,
    TRACKER_HEADER,
    UNPAIRED_FILENAME,
    UNPAIRED_HEADER,
    build_pairing_tsv,
    build_tracker_tsv,
    build_unpaired_tsv,
)
from syzygy.main import app
from syzygy.models import AllowedUser, BaitSet, PairingLogic, PairingTable, Sample
from syzygy.pairing_import import EXTENDED_HEADER
from syzygy.schemas import TumorOrNormal

import scripts.import_tempo_pairs as itp

OPS = {"sub": "ops-1", "email": "ops@example.com", "preferred_username": "ops"}

TUMOR = TumorOrNormal.TUMOR.value
NORMAL = TumorOrNormal.NORMAL.value


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


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


@pytest.fixture
def tempo(db):
    """TEMPO logic + the bait sets its tests use, registered so the
    logic-family membership condition (listing-filter semantics) can
    see the samples."""
    logic = PairingLogic(name="TEMPO", strategy_name="tempo")
    db.add(logic)
    db.flush()
    db.add(
        BaitSet(
            bait_set="agilent",
            pairing_logic_id=logic.id,
        )
    )
    db.commit()
    db.refresh(logic)
    return logic


def _sample(
    db,
    primary_id: str,
    cmo: str,
    tumor_or_normal,
    patient__cmo,
    bait_set: str | None = "agilent",
    sample_type: str = "tissue",
    run_mode: str = "NovaSeq X 1000",
    sequencing_date=None,
    redacted: bool = False,
    additional_metadata=None,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo,
        patient__cmo=patient__cmo,
        sample_type=sample_type,
        gene_panel="IMPACT468",
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        run_mode=run_mode,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        sequencing_date=sequencing_date,
        redacted=redacted,
        additional_metadata=additional_metadata,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _pair(db, logic, tumor, normal):
    row = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        trigger_type="automatic",
        latest=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# build_pairing_tsv — beagle shape, scoping, deterministic ordering
# ---------------------------------------------------------------------------


def test_pairing_tsv_beagle_header_and_rows(db, tempo):
    # Two TEMPO pairs, created in reverse name order to pin the
    # tumor-cmo-name ordering.
    t2 = _sample(db, "P-T2", "T2-T", TUMOR, "C-2")
    n2 = _sample(db, "P-N2", "N2-N", NORMAL, "C-2")
    _pair(db, tempo, t2, n2)
    t1 = _sample(db, "P-T1", "A-T", TUMOR, "C-1")
    n1 = _sample(db, "P-N1", "A-N", NORMAL, "C-1")
    _pair(db, tempo, t1, n1)

    text = build_pairing_tsv(db, "TEMPO")
    lines = text.splitlines()
    assert lines[0] == "\t".join(EXTENDED_HEADER)
    assert text.endswith("\n")
    assert lines[1:] == [
        "A-N\tA-T\tP-N1\tP-T1",
        "N2-N\tT2-T\tP-N2\tP-T2",
    ]


def test_pairing_tsv_scopes_to_latest_rows_of_the_logic(db, tempo):
    t = _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    n = _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    retired = _pair(db, tempo, t, n)
    retired.latest = False
    db.commit()

    other = PairingLogic(name="ACCESS", strategy_name="default")
    db.add(other)
    db.commit()
    db.refresh(other)
    t_other = _sample(db, "P-T9", "T9-T", TUMOR, "C-9")
    n_other = _sample(db, "P-N9", "N9-N", NORMAL, "C-9")
    _pair(db, other, t_other, n_other)

    lines = build_pairing_tsv(db, "TEMPO").splitlines()
    # Header only: the retired row and the other-logic row are out of
    # scope.
    assert lines == ["\t".join(EXTENDED_HEADER)]

    # Re-latest the TEMPO pair -> exactly one data row.
    retired.latest = True
    db.commit()
    lines = build_pairing_tsv(db, "TEMPO").splitlines()
    assert len(lines) == 2
    assert lines[1] == "N1-N\tT1-T\tP-N1\tP-T1"


def test_pairing_tsv_unknown_logic_raises_value_error(db, tempo):
    with pytest.raises(ValueError, match="not found"):
        build_pairing_tsv(db, "NOPE")


# ---------------------------------------------------------------------------
# build_unpaired_tsv — beagle shape + non-empty diagnostics
# ---------------------------------------------------------------------------


def test_unpaired_tsv_beagle_header_and_non_empty_reasons(db, tempo):
    from datetime import date

    # A: no normals at all for the patient.
    _sample(db, "P-A-T", "A-T", TUMOR, "C-A")
    # B: same-patient normal, bait mismatch.
    _sample(
        db, "P-B-T", "B-T", TUMOR, "C-B",
        sequencing_date=date(2026, 11, 1),
    )
    _sample(db, "P-B-N", "B-N", NORMAL, "C-B", bait_set="other")
    # C: fully matching candidate but no pairing row created yet.
    _sample(db, "P-C-T", "C-T", TUMOR, "C-C")
    _sample(db, "P-C-N", "C-N", NORMAL, "C-C")
    # D: already paired -> must NOT appear in the unpaired file.
    d_t = _sample(db, "P-D-T", "D-T", TUMOR, "C-D")
    d_n = _sample(db, "P-D-N", "D-N", NORMAL, "C-D")
    _pair(db, tempo, d_t, d_n)
    # E: redacted tumor with no normals -> redaction prefix + reason.
    _sample(db, "P-E-T", "E-T", TUMOR, "C-E", redacted=True)

    lines = build_unpaired_tsv(db, "TEMPO").splitlines()
    assert lines[0] == "\t".join(UNPAIRED_HEADER)
    data = [ln.split("\t") for ln in lines[1:]]
    assert all(len(r) == len(UNPAIRED_HEADER) for r in data)
    # Ordered by cmo_sample_name; the paired tumor is absent.
    assert [r[0] for r in data] == ["A-T", "B-T", "C-T", "E-T"]
    reasons = {r[0]: r[-1] for r in data}
    # Every reason is non-empty.
    assert all(reasons.values())
    assert reasons["A-T"] == "No normals for patient"
    assert "bait_set mismatch" in reasons["B-T"]
    assert "0 matched" in reasons["B-T"]
    assert "matching candidate normal(s) (C-N)" in reasons["C-T"]
    assert reasons["E-T"].startswith("tumor redacted")
    # Row cells: patient, primary, class, run mode, sample type, bait,
    # runDate (YYYY-MM-DD, blank when unknown), reason.
    b_row = dict(zip(UNPAIRED_HEADER, [r for r in data if r[0] == "B-T"][0]))
    assert b_row["cmoPatientId"] == "C-B"
    assert b_row["primaryId"] == "P-B-T"
    assert b_row["sampleClass"] == "T"
    assert b_row["runMode"] == "NovaSeq X 1000"
    assert b_row["sampleType"] == "tissue"
    assert b_row["baitSet"] == "agilent"
    assert b_row["runDate"] == "2026-11-01"
    a_row = dict(zip(UNPAIRED_HEADER, [r for r in data if r[0] == "A-T"][0]))
    assert a_row["runDate"] == ""


def test_unpaired_tsv_unknown_logic_raises_value_error(db, tempo):
    with pytest.raises(ValueError, match="not found"):
        build_unpaired_tsv(db, "NOPE")


# ---------------------------------------------------------------------------
# Round-trip: pairing export through scripts/import_tempo_pairs.py --dry-run
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(db):
    row = AllowedUser(email=OPS["email"], role="admin", logic_groups=[])
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_pairing_export_round_trips_through_cli_dry_run(
    db, tempo, admin, monkeypatch, tmp_path
):
    t = _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    n = _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _pair(db, tempo, t, n)
    db.commit()

    path = tmp_path / "sample_pairing.txt"
    path.write_text(build_pairing_tsv(db, "TEMPO"))

    # Serve the real app in-process: the CLI's httpx.Client is swapped
    # for one whose MockTransport handler forwards every request to a
    # starlette TestClient on the live app, so the script's exact
    # request path (logic lookup, sample resolution, possible-matches)
    # runs against the real routes.
    SessionLocal = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def _override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    real_client = httpx.Client
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: OPS
    try:
        # No context manager: the lifespan (dev-DB seeding) is not run,
        # same as the repo's other TestClient fixtures.
        tc = TestClient(app)

        def _handler(request: httpx.Request) -> httpx.Response:
            resp = tc.request(
                request.method,
                str(request.url),
                headers=request.headers,
                content=request.content,
            )
            return httpx.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=resp.content,
            )

        def _proxy_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", _proxy_client)
        rc = itp.main(
            [
                str(path),
                "--base-url", "http://testserver",
                "--token", "dummy",
                "--logic", "TEMPO",
                "--dry-run",
            ]
        )
    finally:
        app.dependency_overrides.clear()

    assert rc == 0
    # Dry-run: the existing pairing stands, nothing new was created.
    assert len(db.scalars(select(PairingTable)).all()) == 1


# ---------------------------------------------------------------------------
# GET /v1/export/tempo/{pairing,unpaired}
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(db, admin):
    SessionLocal = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def _override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: OPS
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_export_endpoints_require_auth(db, tempo):
    client = TestClient(app)
    assert client.get("/v1/export/tempo/pairing").status_code == 401
    assert client.get("/v1/export/tempo/unpaired").status_code == 401
    assert client.get("/v1/export/tempo/tracker").status_code == 401


def test_export_endpoints_text_plain_beagle_filenames(authed_client, db, tempo):
    t = _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    n = _sample(db, "P-N1", "N1-N", NORMAL, "C-1")
    _pair(db, tempo, t, n)
    _sample(db, "P-T2", "T2-T", TUMOR, "C-2")  # unpaired

    resp = authed_client.get("/v1/export/tempo/pairing")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="{PAIRING_FILENAME}"'
    )
    lines = resp.text.splitlines()
    assert lines[0] == "\t".join(EXTENDED_HEADER)
    assert lines[1] == "N1-N\tT1-T\tP-N1\tP-T1"

    resp = authed_client.get("/v1/export/tempo/unpaired")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="{UNPAIRED_FILENAME}"'
    )
    lines = resp.text.splitlines()
    assert lines[0] == "\t".join(UNPAIRED_HEADER)
    assert [ln.split("\t")[0] for ln in lines[1:]] == ["T2-T"]
    assert lines[1].split("\t")[-1]  # reason non-empty


def test_export_endpoints_unknown_logic_404(authed_client, db, tempo):
    resp = authed_client.get("/v1/export/tempo/pairing?pairing_logic_name=NOPE")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
    resp = authed_client.get("/v1/export/tempo/unpaired?pairing_logic_name=NOPE")
    assert resp.status_code == 404
    resp = authed_client.get("/v1/export/tempo/tracker?pairing_logic_name=NOPE")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_openapi_lists_export_routes(db, tempo):
    paths = app.openapi()["paths"]
    assert "/v1/export/tempo/pairing" in paths
    assert "/v1/export/tempo/unpaired" in paths
    assert "/v1/export/tempo/tracker" in paths


# ---------------------------------------------------------------------------
# E2: build_tracker_tsv — beagle's sample_tracker.txt
# ---------------------------------------------------------------------------

# Byte-equal to the header line beagle's create_tracker_file writes
# (beagle runner/operator/tempo_mpgen_operator/tempo_mpgen_operator.py,
# verified 2026-11-19): the 6 PM-formatted columns followed by the 17
# raw metadata keys as column names, tab-joined, no trailing tab.
BEAGLE_TRACKER_HEADER = (
    "CMO_Sample_ID\tCollaborator_ID_(or_DMP_Sample_ID)"
    "\tHistorical_Investigator_ID_(for_CCS_use)\tSample_Class_(T/N)"
    "\tBait_set_(Agilent/_IDT/WGS)\tIGO_Request_ID_(Project_ID)"
    "\ttumorOrNormal\tspecies\tgenePanel\tsampleClass\tprimaryId"
    "\tcmoPatientId\tinvestigatorName\tinvestigatorEmail\tpiEmail"
    "\tlabHeadName\tlabHeadEmail\tpreservation\tdataAnalystName"
    "\tdataAnalystEmail\tprojectManagerName\tinvestigatorSampleId\tcmoSampleName"
)

#: The raw metadata columns (no sample-column mapping) that must resolve
#: to an empty cell when the metadata is absent.
RAW_METADATA_KEYS = (
    "species",
    "investigatorName",
    "investigatorEmail",
    "piEmail",
    "labHeadName",
    "labHeadEmail",
    "dataAnalystName",
    "dataAnalystEmail",
    "projectManagerName",
    "investigatorSampleId",
)


def _tracker_rows(db, logic_name="TEMPO"):
    lines = build_tracker_tsv(db, logic_name).splitlines()
    return lines[0], [dict(zip(TRACKER_HEADER, ln.split("\t"))) for ln in lines[1:]]


def test_tracker_tsv_header_byte_equal_to_beagle(db, tempo):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    text = build_tracker_tsv(db, "TEMPO")
    lines = text.splitlines()
    # The acceptance criterion: byte-equal to beagle's tracker header.
    assert lines[0] == BEAGLE_TRACKER_HEADER
    assert len(BEAGLE_TRACKER_HEADER.split("\t")) == 23
    assert not BEAGLE_TRACKER_HEADER.endswith("\t")
    assert text.endswith("\n")
    # The module constant is the same byte string (single source).
    assert "\t".join(TRACKER_HEADER) == BEAGLE_TRACKER_HEADER


def test_tracker_tsv_missing_metadata_empty_cells_no_raise(db, tempo):
    # additional_metadata NULL (the ingest default for non-NATS rows).
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    # Empty metadata blob.
    _sample(db, "P-T2", "T2-T", TUMOR, "C-2", additional_metadata={})
    # Empty requestMetadata blob.
    _sample(
        db, "P-T3", "T3-T", TUMOR, "C-3",
        additional_metadata={"requestMetadata": {}},
    )
    # Non-dict metadata blob (defensive: must not raise either).
    _sample(db, "P-T4", "T4-T", TUMOR, "C-4", additional_metadata="not-a-dict")

    header, rows = _tracker_rows(db)
    assert header == BEAGLE_TRACKER_HEADER
    assert len(rows) == 4
    for row in rows:
        assert len(row) == 23
        # Every raw-metadata column resolves to an empty cell.
        for key in RAW_METADATA_KEYS:
            assert row[key] == "", key
        # The sample-column cells still resolve.
        assert row["primaryId"]
        assert row["cmoPatientId"].startswith("C-")
        assert row["genePanel"] == "IMPACT468"
        assert row["preservation"] == "FFPE"
        assert row["Sample_Class_(T/N)"] == "T"
        assert row["tumorOrNormal"] == "T"
        assert row["sampleClass"] == "T"
        assert row["cmoSampleName"] == row["CMO_Sample_ID"]


def test_tracker_tsv_resolution_order_sample_column_then_metadata(db, tempo):
    _sample(
        db, "P-T1", "T1-T", TUMOR, "C-1",
        additional_metadata={
            # Sample column must win over the metadata.
            "cmoSampleName": "WRONG-NAME",
            "genePanel": "WRONG-PANEL",
            # Top-level metadata wins over requestMetadata.
            "species": "human",
            "investigatorName": "Top-level",
            "requestMetadata": {
                "investigatorName": "Nested",
                "species": "mouse",
                # Only present in requestMetadata -> must be found.
                "piEmail": "pi@example.com",
            },
        },
    )
    _, rows = _tracker_rows(db)
    row = rows[0]
    assert row["cmoSampleName"] == "T1-T"
    assert row["genePanel"] == "IMPACT468"
    assert row["species"] == "human"
    assert row["investigatorName"] == "Top-level"
    assert row["piEmail"] == "pi@example.com"
    # cmoPatientId: the sample column (patient__cmo) is present -> wins.
    assert row["cmoPatientId"] == "C-1"
    # A missing sample column falls through to the metadata: give a
    # pooled-normal-style sample (no patient__cmo) a metadata patient id.
    _sample(
        db, "P-N1", "N1-N", NORMAL, None,
        additional_metadata={"cmoPatientId": "METADATA-PATIENT"},
    )
    _, rows = _tracker_rows(db)
    n_row = [r for r in rows if r["cmoSampleName"] == "N1-N"][0]
    assert n_row["cmoPatientId"] == "METADATA-PATIENT"
    assert n_row["Sample_Class_(T/N)"] == "N"


def test_tracker_tsv_dmp_primary_id_in_collaborator_id(db):
    logic = PairingLogic(name="DMP", strategy_name="dmp")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    # DMP samples carry no bait-set key; the DMP logic's null-bait rule
    # puts them in scope.
    _sample(db, "DMP-S1", "DMP-S1", TUMOR, None, bait_set=None, sample_type="DMP")
    _sample(db, "DMP-S2", "DMP-S2", NORMAL, None, bait_set=None, sample_type="DMP")

    header, rows = _tracker_rows(db, "DMP")
    assert header == BEAGLE_TRACKER_HEADER
    assert len(rows) == 2
    for row in rows:
        # The acceptance criterion: primary_id in the Collaborator_ID
        # column for DMP samples.
        assert row["Collaborator_ID_(or_DMP_Sample_ID)"] == row["primaryId"]
        assert row["primaryId"] in ("DMP-S1", "DMP-S2")
        # DMP rows carry no bait set; the metadata slot stays empty.
        assert row["Bait_set_(Agilent/_IDT/WGS)"] == ""


def test_tracker_tsv_non_dmp_collaborator_from_metadata(db, tempo):
    _sample(
        db, "P-T1", "T1-T", TUMOR, "C-1",
        additional_metadata={"investigatorSampleId": "INV-77"},
    )
    _, rows = _tracker_rows(db)
    row = rows[0]
    # Non-DMP: beagle fills the Collaborator slot from the
    # investigatorSampleId metadata; the spec pins the Historical
    # Investigator ID to the same key.
    assert row["Collaborator_ID_(or_DMP_Sample_ID)"] == "INV-77"
    assert row["Historical_Investigator_ID_(for_CCS_use)"] == "INV-77"
    # And the metadata investigatorSampleId column carries it too.
    assert row["investigatorSampleId"] == "INV-77"
    # No metadata at all -> both slots empty, no raise.
    _sample(db, "P-T2", "T2-T", TUMOR, "C-2")
    _, rows = _tracker_rows(db)
    t2 = [r for r in rows if r["cmoSampleName"] == "T2-T"][0]
    assert t2["Collaborator_ID_(or_DMP_Sample_ID)"] == ""
    assert t2["Historical_Investigator_ID_(for_CCS_use)"] == ""


def test_tracker_tsv_scopes_to_logic_family_both_classes(db, tempo):
    t = _sample(db, "P-T1", "A-T", TUMOR, "C-1")
    n = _sample(db, "P-N1", "A-N", NORMAL, "C-1")
    _pair(db, tempo, t, n)
    # Unpaired normal in the family: beagle's tracker covers whole
    # patients, so it must appear.
    _sample(db, "P-N2", "B-N", NORMAL, "C-2")
    # Unregistered bait set: out of every logic's scope.
    _sample(db, "P-X1", "X-T", TUMOR, "C-X", bait_set="other")

    _, rows = _tracker_rows(db)
    assert [r["cmoSampleName"] for r in rows] == ["A-N", "A-T", "B-N"]
    a_n = rows[0]
    assert a_n["Sample_Class_(T/N)"] == "N"
    assert a_n["tumorOrNormal"] == "N"
    assert a_n["sampleClass"] == "N"
    assert a_n["cmoPatientId"] == "C-1"


def test_tracker_tsv_unknown_logic_raises_value_error(db, tempo):
    with pytest.raises(ValueError, match="not found"):
        build_tracker_tsv(db, "NOPE")


def test_tracker_endpoint_beagle_filename_and_header(authed_client, db, tempo):
    _sample(db, "P-T1", "T1-T", TUMOR, "C-1")
    _sample(db, "P-N1", "T1-N", NORMAL, "C-1")

    resp = authed_client.get("/v1/export/tempo/tracker")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="{TRACKER_FILENAME}"'
    )
    lines = resp.text.splitlines()
    assert lines[0] == BEAGLE_TRACKER_HEADER
    # One row per in-scope sample (the two above), header + 2 rows.
    assert len(lines) == 3
    assert [ln.split("\t")[0] for ln in lines[1:]] == ["T1-N", "T1-T"]
