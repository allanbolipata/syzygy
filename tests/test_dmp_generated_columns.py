"""Postgres-only tests for the DMP sargable candidate query (DMP
bulk-pairing performance, item 1).

These exercise the GENERATED ... STORED key columns
(``scripts/migrate_dmp_generated_columns.py``) and the strategy's
sargable SQL path, which only exist on a real Postgres database after
the migration has run. They SKIP the whole module when there is no
reachable Postgres (the rest of the suite falls back to in-memory
SQLite, which cannot express generated columns — there the strategy
uses the legacy Python-remap path and those columns stay NULL).

Acceptance criteria covered here (card t_8013d29e):
  (a) generated columns self-maintain — they recompute when a source
      column (patient__dmp / primary_id / additional_metadata) is
      updated, with no application-side write;
  (b) generated key values == ``remap_dmp_record`` output for a fixture
      corpus covering every key shape;
  (c) the generated (SQL) candidate row-set is identical to the legacy
      two-tier path on the same corpus (bit-identical semantics);
  (d) EXPLAIN shows the candidate query is served by the partial index
      ``ix_samples_dmp_patient_key`` (Index Scan, not Seq Scan).

``patient__dmp`` is never written anywhere in this module.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from syzygy.database import Base, settings
from syzygy.dmp_translator import remap_dmp_record
from syzygy.models import Sample
from syzygy.pairing_strategies import (
    DmpPairingStrategy,
    _has_dmp_generated_columns,
    strategies,
)
from syzygy.schemas import TumorOrNormal

TEST_DB_NAME = "syzygy_dmp_gen_test"
_EXPECTED_INDEX = "ix_samples_dmp_patient_key"


# ---------------------------------------------------------------------------
# Postgres bootstrap (skip the whole module when unavailable)
# ---------------------------------------------------------------------------


def _configured_postgres_url() -> str | None:
    """The configured Postgres URL, or None when the app is not on Postgres."""
    url = settings.database_url or ""
    return url if url.startswith("postgresql") else None


def _load_migrate():
    """Load scripts/migrate_dmp_generated_columns.py by path (no package)."""
    mig = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "migrate_dmp_generated_columns.py"
    )
    spec = importlib.util.spec_from_file_location("_dmp_gen_migrate", mig)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


def _psycopg2(url: str, dbname: str, autocommit: bool = False):
    import psycopg2

    u = make_url(url)
    conn = psycopg2.connect(
        host=u.host or "localhost",
        port=u.port or 5432,
        user=u.username,
        password=u.password,
        dbname=dbname,
        connect_timeout=5,
    )
    if autocommit:
        conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def pg_base_url():
    """A reachable Postgres URL, or skip the module."""
    url = _configured_postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not a postgresql URL (SQLite fallback)")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not importable in this environment")
    u = make_url(url)
    dbname = u.database or "postgres"
    try:
        conn = _psycopg2(url, dbname)
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres unreachable: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return url


@pytest.fixture(scope="module")
def pg_engine(pg_base_url):
    """A dedicated, freshly-migrated test database (self-contained)."""
    url = pg_base_url
    u = make_url(url)
    dbname = u.database or "postgres"
    try:
        admin = _psycopg2(url, dbname, autocommit=True)
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
            cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
        admin.close()
    except Exception as e:
        pytest.skip(f"cannot create the test database (need CREATEDB): {e}")

    # NOTE: str(URL) masks the password to "***" (SQLAlchemy renders URLs
    # with hide_password=True by default) — the fresh test DB then rejects
    # the masked password. Render unmasked explicitly.
    test_url = u.set(database=TEST_DB_NAME).render_as_string(hide_password=False)
    eng = create_engine(test_url)
    Base.metadata.create_all(bind=eng)
    try:
        _load_migrate()(test_url)
    except Exception:
        # Postgres accepted the connection and the test-database setup,
        # so a migration failure here is a real bug (invalid DDL,
        # missing privilege, ...) — not an environment gap. Fail loudly
        # instead of skipping so the host gate sees it.
        eng.dispose()
        raise
    yield eng
    # Teardown: drop the throwaway database.
    eng.dispose()
    try:
        admin = _psycopg2(url, dbname, autocommit=True)
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
        admin.close()
    except Exception:
        pass


@pytest.fixture
def pg_db(pg_engine):
    """A per-test session on the migrated test DB (cleaned after each test)."""
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


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


def _mk(
    session,
    primary_id: str,
    *,
    patient__dmp: str | None = None,
    sample_type: str = "DMP",
    meta: dict | None = None,
    ton: str = TumorOrNormal.NORMAL.value,
) -> Sample:
    """Insert one Sample (DMP by default) and return the refreshed row.

    Only the fields relevant to the DMP key are set; the rest stay NULL.
    Generated columns are never set here — the database must compute them.
    """
    s = Sample(
        primary_id=primary_id,
        cmo_sample_name=primary_id,
        tumor_or_normal=ton,
        patient__cmo=None,
        patient__dmp=patient__dmp,
        sample_type=sample_type,
        gene_panel="DMP",
        additional_metadata=meta,
        consent=False,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _exp_patient(rec) -> str | None:
    return rec["patient"] or None


def _exp_ver(rec) -> str | None:
    return rec["ver"] or None


def _exp_pool(rec) -> str | None:
    """The generated pool key: the remap pool value when it is a
    non-empty string, else NULL (NA)."""
    p = rec["pool_id"]
    return p if isinstance(p, str) and p else None


def _exp_type_norm(sample_type: str) -> str | None:
    # sample_type is NOT NULL, so this is always a (possibly empty) string.
    return None if sample_type is None else sample_type.strip().upper()


def _reference_row_set(session, tumor: Sample) -> set:
    """The legacy two-tier rule, hand-rolled straight from the remap —
    this is exactly what the legacy Python path computes, so it is the
    ground truth the generated (SQL) path must match."""
    tk = DmpPairingStrategy.pairing_keys(DmpPairingStrategy(), tumor)
    if not tk["patient"]:
        return set()
    normals = session.scalars(
        select(Sample).where(Sample.tumor_or_normal == TumorOrNormal.NORMAL.value)
    ).all()
    dmp_normals = [n for n in normals if _is_dmp(n)]

    def pool(n):
        v = remap_dmp_record(n)["pool_id"]
        return v if isinstance(v, str) and v else None

    def patient(n):
        return remap_dmp_record(n)["patient"]

    def ver(n):
        return remap_dmp_record(n)["ver"]

    tier1 = [
        n
        for n in dmp_normals
        if patient(n) == tk["patient"]
        and tk["pool_id"]
        and isinstance(tk["pool_id"], str)
        and pool(n) == tk["pool_id"]
    ]
    if tier1:
        return {n.id for n in tier1}
    if not tk["ver"]:
        return set()
    tier2 = [
        n
        for n in dmp_normals
        if patient(n) == tk["patient"] and ver(n) == tk["ver"]
    ]
    return {n.id for n in tier2}


def _is_dmp(s: Sample) -> bool:
    return str(s.sample_type or "").strip().upper() == "DMP"


def _build_corpus(session) -> dict:
    """Insert the fixture corpus; return {label: Sample}."""
    c: dict[str, Sample] = {}
    # P-9001: tier-1 (stored patient, project_name pool) + name-parse patient.
    c["t9001"] = _mk(session, "P-9001-T01-IM6", patient__dmp="P-9001",
                     meta={"project_name": "POOLA"}, ton="TUMOR")
    c["n9001a"] = _mk(session, "P-9001-N01-IM6", patient__dmp="P-9001",
                      meta={"project_name": "POOLA"})
    c["n9001b"] = _mk(session, "P-9001-N02-IM6", patient__dmp=None,
                      meta={"project_name": "POOLA"})
    c["n9001c"] = _mk(session, "P-9001-N03-IM9", patient__dmp="P-9001",
                      meta={"project_name": "POOLB"})
    # P-9002: pool_id metadata wins over project_name.
    c["t9002"] = _mk(session, "P-9002-T01-IM6", patient__dmp="P-9002",
                     meta={"pool_id": "PID2", "project_name": "PROJ2"},
                     ton="TUMOR")
    c["n9002a"] = _mk(session, "P-9002-N01-IM6", patient__dmp="P-9002",
                      meta={"pool_id": "PID2"})
    c["n9002b"] = _mk(session, "P-9002-N02-IM6", patient__dmp="P-9002",
                      meta={"project_name": "OTHER"})
    # P-9003: no pool anywhere -> tier-2 IM_ver rescue.
    c["t9003"] = _mk(session, "P-9003-T01-IM6", patient__dmp="P-9003",
                     meta=None, ton="TUMOR")
    c["n9003a"] = _mk(session, "P-9003-N01-IM6", patient__dmp="P-9003", meta=None)
    c["n9003b"] = _mk(session, "P-9003-N02-IM7", patient__dmp="P-9003", meta=None)
    # P-9004: name-parse patient on BOTH sides + pool_id pool.
    c["t9004"] = _mk(session, "P-9004-T01-IM5", patient__dmp=None,
                     meta={"pool_id": "PID4"}, ton="TUMOR")
    c["n9004a"] = _mk(session, "P-9004-N01-IM5", patient__dmp=None,
                      meta={"pool_id": "PID4"})
    # Exclusions.
    _mk(session, "LIMS-N1", sample_type="tissue")
    _mk(session, "POOL-N1", sample_type="Pooled Normal")
    _mk(session, "P-9999-N01-IM6", patient__dmp="P-9999",
        meta={"project_name": "POOLA"})
    # Unparseable DMP tumor (no P- id, NULL patient) -> no patient -> no match.
    c["tx"] = _mk(session, "X-T1", patient__dmp=None, meta=None, ton="TUMOR")
    _mk(session, "X-N1", patient__dmp=None, meta=None)
    return c


# ---------------------------------------------------------------------------
# (a) generated columns self-maintain
# ---------------------------------------------------------------------------


def test_generated_columns_self_maintain(pg_db):
    dmp = DmpPairingStrategy()
    assert _has_dmp_generated_columns(pg_db) is True, (
        "expected the generated columns to be present on this Postgres DB"
    )

    # Start from a NULL stored patient with a parseable name.
    s = _mk(pg_db, "P-7001-T01-IM6", patient__dmp=None,
            meta={"pool_id": "P1", "project_name": "PN"}, ton="TUMOR")
    assert s.dmp_patient_key == "P-7001"      # name-parse fallback
    assert s.dmp_ver_key == "IM6"
    assert s.dmp_pool_id_key == "P1"          # pool_id wins
    assert s.sample_type_norm == "DMP"

    # (1) A stored patient__dmp now wins over the name parse.
    s.patient__dmp = "P-8888"
    pg_db.commit()
    pg_db.refresh(s)
    assert s.dmp_patient_key == "P-8888"

    # (2) Clearing it back to NULL returns to the name parse.
    s.patient__dmp = None
    pg_db.commit()
    pg_db.refresh(s)
    assert s.dmp_patient_key == "P-7001"

    # (3) Changing the name re-derives patient and ver.
    s.primary_id = "P-7002-N03-IM9"
    pg_db.commit()
    pg_db.refresh(s)
    assert s.dmp_patient_key == "P-7002"
    assert s.dmp_ver_key == "IM9"

    # (4) Pool follows metadata changes (pool_id -> project_name -> NA).
    s.additional_metadata = {"project_name": "PN"}
    pg_db.commit()
    pg_db.refresh(s)
    assert s.dmp_pool_id_key == "PN"
    s.additional_metadata = {}
    pg_db.commit()
    pg_db.refresh(s)
    assert s.dmp_pool_id_key is None

    # (5) sample_type_norm tracks sample_type.
    s.sample_type = " dmp "
    pg_db.commit()
    pg_db.refresh(s)
    assert s.sample_type_norm == "DMP"
    _ = dmp  # (strategy unused here; kept for clarity)


# ---------------------------------------------------------------------------
# (b) generated key values == remap_dmp_record output
# ---------------------------------------------------------------------------


def test_generated_keys_match_remap_corpus(pg_db):
    corpus = _build_corpus(pg_db)
    all_rows = pg_db.scalars(select(Sample)).all()
    assert len(all_rows) >= len(corpus), "corpus should be the bulk of the rows"
    for s in all_rows:
        rec = remap_dmp_record(s)
        assert s.dmp_patient_key == _exp_patient(rec), (
            f"{s.primary_id}: dmp_patient_key {s.dmp_patient_key!r} "
            f"!= remap patient {rec['patient']!r}"
        )
        assert s.dmp_ver_key == _exp_ver(rec), (
            f"{s.primary_id}: dmp_ver_key {s.dmp_ver_key!r} != remap ver {rec['ver']!r}"
        )
        assert s.dmp_pool_id_key == _exp_pool(rec), (
            f"{s.primary_id}: dmp_pool_id_key {s.dmp_pool_id_key!r} "
            f"!= remap pool {rec['pool_id']!r}"
        )
        assert s.sample_type_norm == _exp_type_norm(s.sample_type), (
            f"{s.primary_id}: sample_type_norm {s.sample_type_norm!r} "
            f"!= {s.sample_type!r}"
        )


# ---------------------------------------------------------------------------
# (c) generated (SQL) row-set == legacy two-tier row-set
# ---------------------------------------------------------------------------


def test_generated_path_equivalent_to_legacy(pg_db, monkeypatch):
    corpus = _build_corpus(pg_db)
    dmp = strategies["dmp"]
    assert _has_dmp_generated_columns(pg_db) is True
    tumors = [corpus[k] for k in ("t9001", "t9002", "t9003", "t9004", "tx")]
    for tumor in tumors:
        gen = {s.id for s in dmp.select_matching_normals(pg_db, tumor)}
        # Force the legacy path on the same rows and compare.
        monkeypatch.setattr(
            "syzygy.pairing_strategies._has_dmp_generated_columns",
            lambda db: False,
        )
        legacy = {s.id for s in dmp.select_matching_normals(pg_db, tumor)}
        monkeypatch.undo()
        ref = _reference_row_set(pg_db, tumor)
        assert gen == legacy == ref, (
            f"{tumor.primary_id}: generated={sorted(str(s.primary_id) for s in dmp.select_matching_normals(pg_db, tumor))} "
            f"legacy={sorted(str(i) for i in legacy)} ref={sorted(str(i) for i in ref)}"
        )


def test_generated_path_selects_expected_normals(pg_db):
    corpus = _build_corpus(pg_db)
    dmp = strategies["dmp"]
    # P-9001: two tier-1 normals (stored + name-parse patient), the other
    # pool/IM_ver normal excluded.
    got = {s.primary_id for s in dmp.select_matching_normals(pg_db, corpus["t9001"])}
    assert got == {"P-9001-N01-IM6", "P-9001-N02-IM6"}
    # P-9002: pool_id wins -> only the PID2 normal.
    got = {s.primary_id for s in dmp.select_matching_normals(pg_db, corpus["t9002"])}
    assert got == {"P-9002-N01-IM6"}
    # P-9003: no pool -> tier-2 IM_ver rescue only.
    got = {s.primary_id for s in dmp.select_matching_normals(pg_db, corpus["t9003"])}
    assert got == {"P-9003-N01-IM6"}
    # P-9004: name-parse patient both sides + pool_id -> tier-1.
    got = {s.primary_id for s in dmp.select_matching_normals(pg_db, corpus["t9004"])}
    assert got == {"P-9004-N01-IM5"}
    # Unparseable tumor: no patient -> nothing.
    got = {s.primary_id for s in dmp.select_matching_normals(pg_db, corpus["tx"])}
    assert got == set()


# ---------------------------------------------------------------------------
# (d) EXPLAIN: the candidate query is served by the partial index
# ---------------------------------------------------------------------------


def _plan_uses_index(plan_json, index_name: str) -> bool:
    def walk(node):
        if isinstance(node, dict):
            if node.get("Index Name") == index_name:
                return True
            for v in node.values():
                if walk(v):
                    return True
        elif isinstance(node, list):
            for v in node:
                if walk(v):
                    return True
        return False

    return walk(plan_json)


def test_candidate_query_uses_partial_index(pg_db):
    # Enough DMP rows that the index is a realistic choice, across many
    # patients (aggregate counts only — no dataset content).
    for i in range(200):
        p = f"P-{8000 + i}"
        _mk(pg_db, f"{p}-N01-IM6", patient__dmp=p,
            meta={"project_name": f"POOL{i % 5}"})
    patient = "P-8000"
    pg_db.execute(text("ANALYZE samples"))
    pg_db.commit()

    with pg_db.connection() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        row = conn.execute(
            text(
                "EXPLAIN (FORMAT JSON) SELECT * FROM samples "
                "WHERE tumor_or_normal = 'NORMAL' "
                "AND sample_type_norm = 'DMP' "
                "AND dmp_patient_key = :p ORDER BY primary_id"
            ),
            {"p": patient},
        ).scalar_one()
    # EXPLAIN (FORMAT JSON) returns a one-element list (the JSON plan).
    plan = row[0] if isinstance(row, (list, tuple)) else row
    assert _plan_uses_index(plan, _EXPECTED_INDEX), (
        f"candidate query did not use {_EXPECTED_INDEX}; plan={plan!r}"
    )
