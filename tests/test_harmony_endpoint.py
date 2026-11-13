"""E2E tests for the HARMONY pairing space (HARMONY DMP 2/4).

HARMONY is its OWN pairing logic row and pairing space, separate from the
DMP logic (which may drift independently). These tests exercise the real
HTTP surface with ``TestClient`` (no live server):

  * ``POST /v1/pairing/trigger-harmony`` runs the automatic cascade under
    the HARMONY logic id;
  * ``GET /v1/pairing/harmony/?format=csv`` returns the 8-column,
    normal-oriented export (exact headers, one row per (normal, tumor)
    pair, 1-to-many, unmatched DMP samples one-sided, consent rendered
    lowercase);
  * the HARMONY pairing space is fully isolated from the DMP pairing
    space (triggering one never touches the other's rows).

Same in-memory SQLite fixture style as tests/test_dmp_pairing_e2e.py, plus
a TestClient wired via ``dependency_overrides`` (no lifespan run).
"""

import csv
import io
import re
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.auth as auth
from syzygy.auth import get_current_user
from syzygy.database import Base, get_db
from syzygy.main import app
from syzygy.models import PairingLogic, PairingTable, Sample
from syzygy.routers.pairings import (
    REPORT_MAX_PAGE_SIZE,
    HARMONY_BAM_PATH_PLACEHOLDER,
    HARMONY_COLUMNS,
    HARMONY_FRAME_COLUMNS,
    _harmony_csv_consent,
)
from syzygy.schemas import TumorOrNormal


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
def dmp_logic(db):
    logic = PairingLogic(name="DMP", strategy_name="dmp")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


@pytest.fixture
def harmony_logic(db):
    logic = PairingLogic(name="HARMONY", strategy_name="dmp")
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


@pytest.fixture
def client(db):
    # Wire the app to the in-memory session and satisfy the router-level
    # auth dependency without contacting Keycloak.
    def override_get_db():
        yield db

    def override_user():
        return {"sub": "test", "email": "test@example.com"}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    # Bare client (no context manager): the app lifespan is not run, so no
    # real database engine is used.
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _dmp_sample(
    db,
    primary_id,
    patient__dmp,
    tumor_or_normal=TumorOrNormal.NORMAL.value,
    gene_panel="IMPACT468",
    anon_id=None,
    consent=False,
):
    """A DMP sample as the DMP translator would create it (consent=False
    placeholder, distinct anon_id)."""
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient__dmp=patient__dmp,
        anon_id=anon_id,
        sample_type="DMP",
        gene_panel=gene_panel,
        tumor_or_normal=tumor_or_normal,
        consent=consent,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _pairings_for_logic(db, logic_id):
    return db.scalars(
        select(PairingTable).where(PairingTable.pairing_logic_id == logic_id)
    ).all()


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.text)))


# ---------------------------------------------------------------------------
# 8-column normal-oriented export: headers + a paired row
# ---------------------------------------------------------------------------


def test_harmony_csv_exact_headers_and_row(client, db, dmp_logic, harmony_logic):
    tumor = _dmp_sample(
        db, "P-0001234-T01-IM6", "P-0001234",
        tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1",
    )
    normal = _dmp_sample(
        db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1",
    )

    trigger = client.post("/v1/pairing/trigger-harmony")
    assert trigger.status_code == 200
    body = trigger.json()
    assert body["pairing_logic"] == "HARMONY"
    assert body["paired"] == 1
    assert body["unpaired"] == 0
    assert body["scanned"] == 1

    response = client.get("/v1/pairing/harmony/", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = _csv_rows(response)
    # Header is EXACTLY the 8 columns, in order.
    assert rows[0] == list(HARMONY_COLUMNS)
    # One data row for the single (normal, tumor) pair.
    assert len(rows) == 2
    assert rows[1] == [
        "P-0001234-N01-IM6",       # normal_sample (primary_id)
        "P-0001234-T01-IM6",       # tumor_sample (primary_id)
        "anon-n1",                 # normal_bamname (anon_id)
        HARMONY_BAM_PATH_PLACEHOLDER,  # normal_bam_path (em-dash placeholder)
        "anon-t1",                 # tumor_bamname (anon_id)
        HARMONY_BAM_PATH_PLACEHOLDER,  # tumor_bam_path (em-dash placeholder)
        "P-0001234",               # patient (tumor's patient__dmp, preferred)
        "false",                   # consent (placeholder False -> "false")
    ]


def test_harmony_json_default_returns_paged_frame(client, db, dmp_logic, harmony_logic):
    _dmp_sample(db, "P-0001234-T01-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1")
    client.post("/v1/pairing/trigger-harmony")

    response = client.get("/v1/pairing/harmony/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    # Page envelope, not a bare row list: the frame is server-paginated like
    # the Pair Report's, so a page never carries the whole frame.
    assert set(data) == {"rows", "total", "matched", "unmatched", "prev_normal_sample"}
    assert data["total"] == 1 and data["matched"] == 1 and data["unmatched"] == 0
    assert data["prev_normal_sample"] is None  # page 1 has no previous row
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert set(row) == set(HARMONY_COLUMNS)
    assert row["normal_sample"] == "P-0001234-N01-IM6"
    assert row["tumor_sample"] == "P-0001234-T01-IM6"
    # JSON keeps consent as a native boolean (not the CSV string form).
    assert row["consent"] is False
    assert row["patient"] == "P-0001234"


# ---------------------------------------------------------------------------
# 1-to-many: one normal repeats across multiple rows
# ---------------------------------------------------------------------------


def test_harmony_one_normal_many_tumors(client, db, dmp_logic, harmony_logic):
    _dmp_sample(db, "P-0001234-T01-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-0001234-T02-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t2")
    _dmp_sample(db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1")

    trigger = client.post("/v1/pairing/trigger-harmony")
    assert trigger.status_code == 200
    assert trigger.json()["paired"] == 2

    response = client.get("/v1/pairing/harmony/", params={"format": "csv"})
    rows = _csv_rows(response)
    assert rows[0] == list(HARMONY_COLUMNS)
    data = rows[1:]
    assert len(data) == 2

    # Ordered by normal primary_id then tumor primary_id: T01 before T02.
    assert [r[1] for r in data] == ["P-0001234-T01-IM6", "P-0001234-T02-IM6"]
    # Both rows share the normal-side columns (1-to-many).
    for r in data:
        assert r[0] == "P-0001234-N01-IM6"      # normal_sample
        assert r[2] == "anon-n1"                # normal_bamname
        assert r[3] == HARMONY_BAM_PATH_PLACEHOLDER  # normal_bam_path
        assert r[6] == "P-0001234"              # patient
        assert r[7] == "false"                  # consent
    # Tumor-side columns differ per row.
    assert data[0][4] == "anon-t1"
    assert data[1][4] == "anon-t2"


# ---------------------------------------------------------------------------
# Unmatched DMP samples appear one-sided (opposite side's columns empty)
# ---------------------------------------------------------------------------


def test_harmony_unmatched_normal_one_sided(client, db, dmp_logic, harmony_logic):
    _dmp_sample(db, "P-0001234-T01-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1")
    # A DMP normal of a different patient with no tumor counterpart: unmatched.
    _dmp_sample(db, "P-0009999-N01-IM6", "P-0009999", anon_id="anon-n9")

    trigger = client.post("/v1/pairing/trigger-harmony")
    assert trigger.status_code == 200
    body = trigger.json()
    assert body["paired"] == 1
    assert body["unpaired"] == 0  # the unmatched side is a normal, not a tumor

    response = client.get("/v1/pairing/harmony/", params={"format": "csv"})
    rows = _csv_rows(response)
    assert rows[0] == list(HARMONY_COLUMNS)
    data = rows[1:]
    # Ordered by normal primary_id: the paired P-0001234 row sorts before
    # the unmatched P-0009999 row.
    assert len(data) == 2
    paired_row, unmatched_row = data
    assert paired_row[0] == "P-0001234-N01-IM6"
    assert paired_row[1] == "P-0001234-T01-IM6"
    # The unmatched normal: present side filled, tumor side empty.
    assert unmatched_row[0] == "P-0009999-N01-IM6"   # normal_sample
    assert unmatched_row[1] == ""                     # tumor_sample (empty)
    assert unmatched_row[2] == "anon-n9"              # normal_bamname
    assert unmatched_row[3] == HARMONY_BAM_PATH_PLACEHOLDER  # normal_bam_path
    assert unmatched_row[4] == ""                     # tumor_bamname (empty)
    assert unmatched_row[5] == ""                     # tumor_bam_path (empty)
    assert unmatched_row[6] == "P-0009999"            # patient (normal's)
    assert unmatched_row[7] == "false"                # consent (normal's)


# ---------------------------------------------------------------------------
# Server-side pagination: pages cover the merged frame exactly once
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def export_stream(db, engine, monkeypatch):
    """Point the CSV stream's own session at the in-memory test engine.

    The harmony export validates with the request session but streams through
    its own (the request session may close before the body finishes sending);
    redirecting that factory is what makes the stream see the fixture rows —
    same as the report export's fixture in tests/test_pairings_report.py.
    """
    monkeypatch.setattr(
        "syzygy.routers.pairings._open_stream_session", sessionmaker(bind=engine)
    )


def _frame_keys(data):
    return [(r["normal_sample"], r["tumor_sample"]) for r in data["rows"]]


def _seed_harmony_frame(client, db):
    _dmp_sample(db, "P-3000-T01-IM6", "P-3000",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t3")
    _dmp_sample(db, "P-1000-T01-IM6", "P-1000",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-1000-T02-IM6", "P-1000",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t2")
    _dmp_sample(db, "P-1000-N01-IM6", "P-1000", anon_id="anon-n1")
    _dmp_sample(db, "P-2000-N01-IM6", "P-2000", anon_id="anon-n2")
    trigger = client.post("/v1/pairing/trigger-harmony")
    assert trigger.status_code == 200
    assert trigger.json()["paired"] == 2  # P-1000's two tumors; P-3000 has no normal


def test_harmony_pages_cover_the_frame_once(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)

    page1 = client.get("/v1/pairing/harmony/", params={"page": 1, "page_size": 2}).json()
    assert page1["total"] == 4 and page1["matched"] == 2 and page1["unmatched"] == 2
    assert page1["prev_normal_sample"] is None
    assert _frame_keys(page1) == [(None, "P-3000-T01-IM6"),
                                  ("P-1000-N01-IM6", "P-1000-T01-IM6")]

    page2 = client.get("/v1/pairing/harmony/", params={"page": 2, "page_size": 2}).json()
    assert page2["total"] == 4  # counts are frame-wide, not page-wide
    # Group-divider context: this page starts mid-group (the P-1000 normal's
    # second tumor), and the endpoint says which normal the previous row had.
    assert page2["prev_normal_sample"] == "P-1000-N01-IM6"
    assert _frame_keys(page2) == [("P-1000-N01-IM6", "P-1000-T02-IM6"),
                                  ("P-2000-N01-IM6", None)]

    # Past the last page: empty rows, counts intact (the client's page
    # controls clamp there; no error).
    beyond = client.get("/v1/pairing/harmony/", params={"page": 3, "page_size": 2}).json()
    assert beyond["rows"] == [] and beyond["total"] == 4

    # Page size 1 covers every row exactly once, in frame order.
    seen = []
    for page in range(1, 6):
        data = client.get("/v1/pairing/harmony/", params={"page": page, "page_size": 1}).json()
        seen.extend(_frame_keys(data))
        if data["rows"] == []:
            break
    assert seen == [(None, "P-3000-T01-IM6"),
                    ("P-1000-N01-IM6", "P-1000-T01-IM6"),
                    ("P-1000-N01-IM6", "P-1000-T02-IM6"),
                    ("P-2000-N01-IM6", None)]


def test_harmony_invalid_page_params(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    # 1-based like the report's params.
    assert client.get("/v1/pairing/harmony/", params={"page": 0}).status_code == 422
    assert client.get("/v1/pairing/harmony/", params={"page_size": 0}).status_code == 422


def test_harmony_page_size_clamped_to_max(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    # Same clamp the report applies (test_page_size_clamped_to_max): an oversized
    # page_size is answered with REPORT_MAX_PAGE_SIZE rows — a 422 would break the
    # UI's large page sizes, and total still has to report the FULL frame.
    for i in range(250):
        _dmp_sample(db, f"P-9{i:03d}-T01-IM6", f"P-9{i:03d}", TumorOrNormal.TUMOR.value)
    data = client.get("/v1/pairing/harmony/", params={"page": 1, "page_size": 10_000}).json()
    assert data["total"] == 4 + 250
    assert len(data["rows"]) == REPORT_MAX_PAGE_SIZE
    # 2 paired + (1 unpaired normal + 1 unpaired tumor + 250) one-sided
    assert (data["matched"], data["unmatched"]) == (2, 252)


# ---------------------------------------------------------------------------
# Server-side search: the query narrows the WHOLE frame, not the loaded page
# ---------------------------------------------------------------------------


def test_harmony_search_filters_the_full_frame(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)

    r = client.get("/v1/pairing/harmony/", params={"q": "1000"}).json()
    assert r["total"] == 2 and r["matched"] == 2 and r["unmatched"] == 0
    assert _frame_keys(r) == [("P-1000-N01-IM6", "P-1000-T01-IM6"),
                              ("P-1000-N01-IM6", "P-1000-T02-IM6")]

    # Either-side match: the unmatched normal on its side only.
    r = client.get("/v1/pairing/harmony/", params={"q": "P-2000"}).json()
    assert r["total"] == 1 and r["matched"] == 0 and r["unmatched"] == 1
    assert _frame_keys(r) == [("P-2000-N01-IM6", None)]

    # Search feeds the page contract too: a filtered frame has its own pages.
    page1 = client.get("/v1/pairing/harmony/", params={"q": "im6", "page": 1, "page_size": 1}).json()
    # Every seeded primary_id ends -IM6, and ilike is case-insensitive even
    # though the query was lowercased and the ids are not.
    assert page1["total"] == 4 and len(page1["rows"]) == 1
    page2 = client.get("/v1/pairing/harmony/", params={"q": "im6", "page": 2, "page_size": 1}).json()
    assert _frame_keys(page2) == [("P-1000-N01-IM6", "P-1000-T01-IM6")]

    # No match: empty frame, not an error.
    r = client.get("/v1/pairing/harmony/", params={"q": "zzz"}).json()
    assert r == {"rows": [], "total": 0, "matched": 0, "unmatched": 0,
                 "prev_normal_sample": None}


def test_harmony_search_covers_bam_names_and_consent(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    _dmp_sample(db, "P-4000-T01-IM6", "P-4000",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t4", consent=True)
    _dmp_sample(db, "P-4000-N01-IM6", "P-4000", anon_id="anon-n4", consent=True)
    client.post("/v1/pairing/trigger-harmony")

    # bam name (anon_id) is searchable on either side.
    r = client.get("/v1/pairing/harmony/", params={"q": "anon-n2"}).json()
    assert _frame_keys(r) == [("P-2000-N01-IM6", None)]

    # Consent matches only the exact words, because every row's consent column
    # is populated: substring search would match the whole frame.
    r = client.get("/v1/pairing/harmony/", params={"q": "true"}).json()
    assert r["total"] == 1 and r["matched"] == 1
    assert _frame_keys(r) == [("P-4000-N01-IM6", "P-4000-T01-IM6")]
    assert client.get("/v1/pairing/harmony/", params={"q": "tru"}).json()["total"] == 0
    r = client.get("/v1/pairing/harmony/", params={"q": "false"}).json()
    assert r["total"] == 4  # the seeded frame's consent is all false


def test_harmony_csv_export_follows_the_search(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    response = client.get("/v1/pairing/harmony/", params={"format": "csv", "q": "1000"})
    assert response.status_code == 200
    rows = _csv_rows(response)
    assert rows[0] == list(HARMONY_COLUMNS)
    # The searched set (2 paired P-1000 rows), not the whole frame and not a
    # page: same "export the full searched set" rule as GET /pairing/export/.
    assert len(rows) == 3
    assert rows[1][1] == "P-1000-T01-IM6" and rows[2][1] == "P-1000-T02-IM6"


# ---------------------------------------------------------------------------
# CSV column selection (?columns=): subset, order, 400 on unknown, default
# ---------------------------------------------------------------------------


def test_harmony_csv_columns_subset(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    # A subset in a client-chosen order (NOT the endpoint order): the header
    # and every data row contain exactly those columns, in that order.
    response = client.get(
        "/v1/pairing/harmony/",
        params={"format": "csv", "columns": "tumor_sample,consent,normal_sample"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = _csv_rows(response)
    assert rows[0] == ["tumor_sample", "consent", "normal_sample"]
    # Same frame as the default export (header + the 4 frame rows), just
    # fewer columns: row selection/ordering is untouched by column selection.
    assert len(rows) == 5
    assert all(len(r) == 3 for r in rows)
    # consent still renders as lowercase true/false (the selected-column
    # projection keeps the consent renderer on the consent column).
    assert all(r[1] in ("true", "false", "") for r in rows[1:])
    # Cross-check against the default export: identical tumor_sample and
    # normal_sample columns, in the same row order.
    default_rows = _csv_rows(
        client.get("/v1/pairing/harmony/", params={"format": "csv"})
    )
    assert [r[0] for r in rows[1:]] == [r[1] for r in default_rows[1:]]
    assert [r[2] for r in rows[1:]] == [r[0] for r in default_rows[1:]]
    assert [r[1] for r in rows[1:]] == [r[7] for r in default_rows[1:]]


def test_harmony_csv_columns_full_subset_explicit(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    # Requesting all 8 keys explicitly equals the default header (order given).
    response = client.get(
        "/v1/pairing/harmony/",
        params={"format": "csv", "columns": ",".join(HARMONY_COLUMNS)},
    )
    assert response.status_code == 200
    rows = _csv_rows(response)
    assert rows[0] == list(HARMONY_COLUMNS)
    # Header + the 4 frame rows (same as the default export).
    assert len(rows) == 5


def test_harmony_csv_unknown_column_rejected(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    bad = client.get(
        "/v1/pairing/harmony/", params={"format": "csv", "columns": "normal_sample,bogus"}
    )
    assert bad.status_code == 400
    assert "bogus" in bad.json()["detail"]
    # A lone unknown key is also rejected before any streaming starts.
    assert client.get(
        "/v1/pairing/harmony/", params={"format": "csv", "columns": "tumor"}
    ).status_code == 400


def test_harmony_csv_no_columns_param_keeps_all_8(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    response = client.get("/v1/pairing/harmony/", params={"format": "csv"})
    assert response.status_code == 200
    rows = _csv_rows(response)
    assert rows[0] == list(HARMONY_COLUMNS)
    assert all(len(r) == 8 for r in rows)


# ---------------------------------------------------------------------------
# Isolation: triggering HARMONY never touches the DMP pairing space
# ---------------------------------------------------------------------------


def test_harmony_trigger_does_not_touch_dmp_pairings(
    client, db, dmp_logic, harmony_logic
):
    _dmp_sample(db, "P-0001234-T01-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1")

    # First, create a DMP-logic pairing under the DMP logic.
    dmp_trigger = client.post("/v1/pairing/trigger-dmp")
    assert dmp_trigger.status_code == 200
    assert dmp_trigger.json()["pairing_logic"] == "DMP"
    assert dmp_trigger.json()["paired"] == 1

    db.expire_all()
    dmp_before = _pairings_for_logic(db, dmp_logic.id)
    assert len(dmp_before) == 1
    dmp_snapshot = [(p.id, p.tumor_id, p.normal_id, p.latest) for p in dmp_before]

    # Now trigger HARMONY: it must create its own pairing row without
    # modifying any DMP-logic row.
    harmony_trigger = client.post("/v1/pairing/trigger-harmony")
    assert harmony_trigger.status_code == 200
    assert harmony_trigger.json()["pairing_logic"] == "HARMONY"
    assert harmony_trigger.json()["paired"] == 1

    db.expire_all()
    dmp_after = _pairings_for_logic(db, dmp_logic.id)
    dmp_after_snapshot = [(p.id, p.tumor_id, p.normal_id, p.latest) for p in dmp_after]
    assert dmp_after_snapshot == dmp_snapshot  # DMP space unchanged

    # A HARMONY-logic pairing now exists in its own space.
    harmony_rows = _pairings_for_logic(db, harmony_logic.id)
    assert len(harmony_rows) == 1
    assert harmony_rows[0].latest is True


# ---------------------------------------------------------------------------
# T4: the canonical column set/order is a contract, not an implementation
# detail — and consent must be present in every row, table and CSV.
#
# CANONICAL_HARMONY_COLUMNS below is a deliberate LITERAL, not a copy of
# routers.pairings.HARMONY_COLUMNS or static/app.js HARMONY_COLUMNS: the whole
# point is that a reorder (or a dropped column) on either side fails here.
# Order user-confirmed 2026-11-10: patient between tumor_bam_path and consent,
# consent last.
# ---------------------------------------------------------------------------

CANONICAL_HARMONY_COLUMNS = [
    "normal_sample",
    "tumor_sample",
    "normal_bamname",
    "normal_bam_path",
    "tumor_bamname",
    "tumor_bam_path",
    "patient",
    "consent",
]

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def _app_js_harmony_column_keys() -> list[str]:
    """The keys of static/app.js ``const HARMONY_COLUMNS``, in source order.

    The table header and the CSV ``columns`` param are both built from
    HARMONY_COLUMNS (``selectedPairColumns()`` filters it, keeping its order;
    ``HARMONY_DEFAULT_KEYS`` is every key of it), so this source order IS the
    on-screen column order of the default HARMONY Pair Report.
    """
    src = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"const HARMONY_COLUMNS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert match is not None, "const HARMONY_COLUMNS not found in static/app.js"
    return re.findall(r"key:\s*\"([A-Za-z_]+)\"", match.group(1))


def test_harmony_backend_column_constants_are_canonical():
    assert list(HARMONY_COLUMNS) == CANONICAL_HARMONY_COLUMNS
    # The frame's response/CSV projection is the same tuple — the CSV header
    # cannot drift from what the endpoint declares.
    assert list(HARMONY_FRAME_COLUMNS) == CANONICAL_HARMONY_COLUMNS
    # The field name is `consent` everywhere; `consented` must not appear.
    assert "consented" not in " ".join(HARMONY_COLUMNS)
    assert "consented" not in APP_JS.read_text(encoding="utf-8")


def test_harmony_client_column_order_matches_canonical_default_all_8():
    keys = _app_js_harmony_column_keys()
    assert keys == CANONICAL_HARMONY_COLUMNS
    # The DEFAULT checked set is every HARMONY column (all 8, this order), so
    # a fresh Pair Report shows consent without ticking anything.
    src = APP_JS.read_text(encoding="utf-8")
    assert re.search(
        r"const HARMONY_DEFAULT_KEYS\s*=\s*HARMONY_COLUMNS\.map\(\(c\) => c\.key\);",
        src,
    ), "HARMONY default selection is no longer every HARMONY_COLUMNS key"
    # An all-checked download omits ?columns=, i.e. takes the server's
    # canonical default header instead of a client-supplied order.
    assert "if (cols.length < HARMONY_COLUMNS.length) params.set(\"columns\"" in src


def test_harmony_csv_default_header_is_the_canonical_order(client, db, dmp_logic, harmony_logic):
    _seed_harmony_frame(client, db)
    response = client.get("/v1/pairing/harmony/", params={"format": "csv"})
    assert response.status_code == 200
    rows = _csv_rows(response)
    # Literals, not HARMONY_COLUMNS: this is the assertion that catches a reorder.
    assert rows[0] == [
        "normal_sample",
        "tumor_sample",
        "normal_bamname",
        "normal_bam_path",
        "tumor_bamname",
        "tumor_bam_path",
        "patient",
        "consent",
    ]
    assert rows[0][-1] == "consent"
    assert rows[0][-2] == "patient"


def test_harmony_json_rows_always_carry_the_consent_key(client, db, dmp_logic, harmony_logic):
    # Three patients: consented pair, non-consented pair, consent unknown.
    _dmp_sample(db, "P-5000-T01-IM6", "P-5000", TumorOrNormal.TUMOR.value, consent=True)
    _dmp_sample(db, "P-5000-N01-IM6", "P-5000", consent=True)
    _dmp_sample(db, "P-6000-T01-IM6", "P-6000", TumorOrNormal.TUMOR.value, consent=False)
    _dmp_sample(db, "P-6000-N01-IM6", "P-6000", consent=False)
    _dmp_sample(db, "P-7000-T01-IM6", "P-7000", TumorOrNormal.TUMOR.value, consent=None)
    _dmp_sample(db, "P-7000-N01-IM6", "P-7000", consent=None)
    assert client.post("/v1/pairing/trigger-harmony").json()["paired"] == 3

    data = client.get("/v1/pairing/harmony/").json()
    assert data["total"] == 3
    by_patient = {r["patient"]: r for r in data["rows"]}
    assert set(by_patient) == {"P-5000", "P-6000", "P-7000"}
    # consent is present on EVERY row (never omitted from the JSON), with the
    # native JSON type: true / false / null (null only when genuinely unknown).
    assert all("consent" in r for r in data["rows"])
    assert by_patient["P-5000"]["consent"] is True
    assert by_patient["P-6000"]["consent"] is False
    assert by_patient["P-7000"]["consent"] is None
    # And every row carries exactly the 8 canonical keys.
    for r in data["rows"]:
        assert set(r) == set(CANONICAL_HARMONY_COLUMNS)


def test_harmony_csv_consent_renders_true_false_and_empty(client, db, dmp_logic, harmony_logic):
    _dmp_sample(db, "P-5000-T01-IM6", "P-5000", TumorOrNormal.TUMOR.value, consent=True)
    _dmp_sample(db, "P-5000-N01-IM6", "P-5000", consent=True)
    _dmp_sample(db, "P-6000-T01-IM6", "P-6000", TumorOrNormal.TUMOR.value, consent=False)
    _dmp_sample(db, "P-6000-N01-IM6", "P-6000", consent=False)
    _dmp_sample(db, "P-7000-T01-IM6", "P-7000", TumorOrNormal.TUMOR.value, consent=None)
    _dmp_sample(db, "P-7000-N01-IM6", "P-7000", consent=None)
    client.post("/v1/pairing/trigger-harmony")

    rows = _csv_rows(client.get("/v1/pairing/harmony/", params={"format": "csv"}))
    header = rows[0]
    consent_col = header.index("consent")
    values = {r[header.index("patient")]: r[consent_col] for r in rows[1:]}
    assert values == {"P-5000": "true", "P-6000": "false", "P-7000": ""}

    # Same three values through an explicit ?columns= projection: the consent
    # renderer stays on the consent column wherever the client puts it.
    sub = _csv_rows(client.get(
        "/v1/pairing/harmony/", params={"format": "csv", "columns": "consent,patient"}
    ))
    assert sub[0] == ["consent", "patient"]
    assert {r[1]: r[0] for r in sub[1:]} == {"P-5000": "true", "P-6000": "false", "P-7000": ""}


def test_harmony_csv_consent_renderer_semantics():
    # The single cell renderer the CSV path uses, in isolation.
    assert _harmony_csv_consent(None) == ""
    assert _harmony_csv_consent(True) is not None and _harmony_csv_consent(True) == "true"
    assert _harmony_csv_consent(False) == "false"
    # Empty is ONLY for a genuine unknown — never for a present False.
    assert _harmony_csv_consent(False) != _harmony_csv_consent(None)


def test_harmony_consent_comes_from_the_present_side_tumor_preferred(
    client, db, dmp_logic, harmony_logic
):
    # Documented HarmonyRow semantics: patient/consent come from the present
    # side, tumor preferred — a tumor whose own consent is unknown inherits the
    # normal's, and a present tumor value wins over the normal's.
    _dmp_sample(db, "P-8000-T01-IM6", "P-8000", TumorOrNormal.TUMOR.value, consent=True)
    _dmp_sample(db, "P-8000-N01-IM6", "P-8000", consent=False)
    _dmp_sample(db, "P-8100-T01-IM6", "P-8100", TumorOrNormal.TUMOR.value, consent=None)
    _dmp_sample(db, "P-8100-N01-IM6", "P-8100", consent=True)
    assert client.post("/v1/pairing/trigger-harmony").json()["paired"] == 2

    rows = _csv_rows(client.get("/v1/pairing/harmony/", params={"format": "csv"}))
    header = rows[0]
    got = {
        r[header.index("patient")]: r[header.index("consent")] for r in rows[1:]
    }
    assert got == {"P-8000": "true", "P-8100": "true"}
