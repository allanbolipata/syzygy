"""TEMPO export ticket E1: pairing + unpaired export module & endpoints.

Spec: Projects/Syzygy/Factory/TEMPO_EXPORT_TICKETS.md. Acceptance:

- beagle-shaped headers (pairing = the extended 4-column
  ``EXTENDED_HEADER``; unpaired = the 8 beagle conflict-file fields +
  ``Possible Reason?``),
- the pairing export round-trips through
  ``scripts/import_tempo_pairs.py --dry-run`` (the real CLI, driven
  in-process over ``httpx.ASGITransport`` against the app),
- unpaired reasons are non-empty for seeded unmatched tumors,
- ``GET /v1/export/tempo/{pairing,unpaired}`` serve ``text/plain``
  downloads with beagle's filenames and require auth.
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
    UNPAIRED_FILENAME,
    UNPAIRED_HEADER,
    build_pairing_tsv,
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
    primary_id,
    cmo,
    tumor_or_normal,
    patient__cmo,
    bait_set="agilent",
    run_mode="NovaSeq X 1000",
    sequencing_date=None,
    redacted=False,
):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo,
        patient__cmo=patient__cmo,
        sample_type="tissue",
        gene_panel="IMPACT468",
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal,
        run_mode=run_mode,
        preservation=["FFPE"],
        sample_origin=["FFPE"],
        sequencing_date=sequencing_date,
        redacted=redacted,
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


def test_openapi_lists_both_export_routes(db, tempo):
    paths = app.openapi()["paths"]
    assert "/v1/export/tempo/pairing" in paths
    assert "/v1/export/tempo/unpaired" in paths
