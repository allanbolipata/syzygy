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


def test_harmony_json_default_returns_row_list(client, db, dmp_logic, harmony_logic):
    _dmp_sample(db, "P-0001234-T01-IM6", "P-0001234",
                tumor_or_normal=TumorOrNormal.TUMOR.value, anon_id="anon-t1")
    _dmp_sample(db, "P-0001234-N01-IM6", "P-0001234", anon_id="anon-n1")
    client.post("/v1/pairing/trigger-harmony")

    response = client.get("/v1/pairing/harmony/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
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
