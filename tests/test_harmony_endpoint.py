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
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
from auth import get_current_user
from database import Base, get_db
from main import app
from models import PairingLogic, PairingTable, Sample
from routers.pairings import (
    REPORT_MAX_PAGE_SIZE,
    HARMONY_BAM_PATH_PLACEHOLDER,
    HARMONY_COLUMNS,
)
from schemas import TumorOrNormal


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
        "routers.pairings._open_stream_session", sessionmaker(bind=engine)
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
    # Same frame as the default export (5 rows), just fewer columns:
    # row selection/ordering is untouched by column selection.
    assert len(rows) == 6
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
    assert len(rows) == 6


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
