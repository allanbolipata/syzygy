"""DMP perf 4/4: perf guard (index-scan assertion) + end-to-end
equivalence harness.

PG-only tests; the whole module SKIPS without a reachable Postgres (the
rest of the suite falls back to in-memory SQLite, which cannot express
``GENERATED ALWAYS AS ... STORED`` columns or ``regexp_match`` — card 1's
generated-column path only exists on a real Postgres with
``scripts/migrate_dmp_generated_columns.py`` applied). Host-side
execution (local pg@14 before merge) is tracked in the reviewer contract
t_2a79a050 deferral list.

1. ``test_perf_guard_candidate_query_uses_index_scan`` — the perf guard
   (brief item 4.1). Seeds a table-scale DMP corpus where a large share
   of DMP rows has ``patient__dmp IS NULL`` (mirroring the measured
   staging baseline shape in aggregate: 162,466 of 287,600 staging DMP
   rows were NULL-patient; pre-fix the per-tumor candidate query was a
   Parallel Seq Scan — 167 ms, 75,047 rows returned per tumor, because
   the IS-NULL arm fetched table-scale rows). On the strategy's real
   per-tumor candidate query it then asserts:
     (a) the plan is served by the partial index
         ``ix_samples_dmp_patient_key``;
     (b) the plan has no Seq Scan on ``samples``;
     (c) the rows the query actually returns are bounded by a multiple
         of the patient's own DMP rows (row-count bound — never table
         scale).
2. ``test_perf_guard_would_fail_on_legacy_query_shape`` — negative
   control (guard sensitivity). EXPLAIN-ANALYZEs the pre-fix candidate
   shape (stored-column filter + IS-NULL arm) on the same seed and
   proves it violates the guard's assertions: no partial-index usage and
   a table-scale row count (the fixture-scale analogue of staging's
   75,047 returned rows). Proves the guard has teeth without
   regressing production code.
3. ``test_e2e_bulk_pass_equivalence_generated_vs_legacy`` — the
   end-to-end equivalence harness (brief item 4.2). Card 1's full
   fixture corpus plus two unpaired tumors, run through the complete DMP
   bulk-pass path (cards 2-3: background job + batched commits) twice —
   once forced down the pre-fix legacy candidate query, once on card 1's
   generated-column path. The paired/unpaired sets, the job counters,
   and the hand-rolled two-tier reference rule must all agree.

Data hygiene: synthetic fixtures only, aggregate counts only in code and
docstrings. ``patient__dmp`` is set here only as synthetic fixture
values.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import syzygy.routers.pairings as pairings
from syzygy.database import Base, settings  # noqa: F401 (settings: parity with card 1)
from syzygy.models import PairingLogic, PairingTable, Sample
from syzygy.pairing_strategies import (
    DmpPairingStrategy,
    _has_dmp_generated_columns,
    seed_pooled_machines,
    strategies,
)
from syzygy.routers.pairings import (
    pairing_job_status_endpoint,
    trigger_dmp_pairing_endpoint,
)
from syzygy.schemas import TumorOrNormal

# Card 1's test module: the shared PG bootstrap helpers, the fixture
# corpus ("full fixture corpus", brief item 4.2), and the hand-rolled
# two-tier reference rule (ground truth for the pre-change semantics).
from test_dmp_generated_columns import (
    _build_corpus,
    _configured_postgres_url,
    _is_dmp,
    _load_migrate,
    _mk,
    _psycopg2,
    _reference_row_set,
)

TEST_DB_NAME = "syzygy_dmp_guard_e2e_test"
_EXPECTED_INDEX = "ix_samples_dmp_patient_key"
_PROBE_PATIENT = "P-8000"
#: Background DMP normals — table scale for the guard seed (aggregate).
_BG_PATIENTS = 2000


# ---------------------------------------------------------------------------
# Postgres bootstrap (skip the whole module when unavailable) — mirrors
# tests/test_dmp_generated_columns.py, with its own throwaway database.
# ---------------------------------------------------------------------------


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
    conn = None
    try:
        conn = _psycopg2(url, dbname)
    except Exception as e:
        pytest.skip(f"Postgres unreachable: {e}")
    finally:
        if conn is not None:
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

    test_url = str(u.set(database=TEST_DB_NAME))
    eng = create_engine(test_url)
    Base.metadata.create_all(bind=eng)
    try:
        _load_migrate()(test_url)
    except Exception as e:
        eng.dispose()
        pytest.skip(f"generated-column migration failed (needs PG13+): {e}")
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


@pytest.fixture()
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


@pytest.fixture(scope="module")
def captured_sql(pg_engine):
    """Every statement executed on the module engine, from any thread."""
    entries: list[tuple[str, object]] = []
    lock = threading.Lock()

    def _on_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        with lock:
            entries.append((statement, parameters))

    sa_event.listens_for(pg_engine, "before_cursor_execute", _on_cursor_execute)
    try:
        yield entries
    finally:
        sa_event.remove(_on_cursor_execute, pg_engine, "before_cursor_execute")


# ---------------------------------------------------------------------------
# EXPLAIN (ANALYZE) plan helpers
# ---------------------------------------------------------------------------


def _plan_nodes(plan):
    nodes: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            nodes.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(plan)
    return nodes


def _plan_index_nodes(plan):
    """The plan nodes served by the DMP partial index."""
    return [n for n in _plan_nodes(plan) if n.get("Index Name") == _EXPECTED_INDEX]


def _plan_seq_scans_on(plan, relation: str):
    return [
        n for n in _plan_nodes(plan)
        if n.get("Node Type") == "Seq Scan" and n.get("Relation Name") == relation
    ]


def _plan_max_actual_rows(plan) -> float:
    """The maximum actual row count over all plan nodes — what the query
    physically produced/received at its busiest node (the rows the client
    gets when that node is the top one, as for a bare SELECT)."""
    rows = [
        n["Actual Rows"] for n in _plan_nodes(plan)
        if isinstance(n.get("Actual Rows"), (int, float))
    ]
    return max(rows) if rows else 0.0


def _explain_analyze_json(engine, statement: str, params: dict) -> dict:
    """EXPLAIN (ANALYZE, FORMAT JSON) of ``statement`` (psycopg2 pyformat
    placeholders) via a raw cursor on the engine's database. The analyzed
    plan reports actual rows per node — what the query really returns,
    not a planner estimate."""
    u = make_url(str(engine.url))
    import psycopg2

    conn = psycopg2.connect(
        host=u.host or "localhost",
        port=u.port or 5432,
        user=u.username,
        password=u.password,
        dbname=u.database,
        connect_timeout=5,
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)
            row = cur.fetchone()
        finally:
            cur.close()
    finally:
        conn.close()
    if row is None:
        raise AssertionError("EXPLAIN returned no plan rows")
    value = row[0]
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        value = value[0] if value else {}
    return value


# ---------------------------------------------------------------------------
# 1. Perf guard: the per-tumor candidate query stays an index scan
# ---------------------------------------------------------------------------


def _seed_guard_corpus(pg_db) -> int:
    """Probe patient + table-scale background DMP normals.

    Mirrors the measured staging baseline shape in aggregate: a DMP table
    where a large share of rows has ``patient__dmp IS NULL`` — exactly
    what the pre-fix candidate query's IS-NULL arm fetched (162,466 of
    287,600 staging DMP rows were NULL-patient). Returns the probe
    patient's own DMP row count.
    """
    objs = [
        Sample(
            primary_id=f"P-{10000 + i}-N01-IM6",
            cmo_sample_name=f"P-{10000 + i}-N01-IM6",
            tumor_or_normal=TumorOrNormal.NORMAL.value,
            patient__dmp=(f"P-{10000 + i}" if i % 2 == 0 else None),
            sample_type="DMP",
            gene_panel="DMP",
            additional_metadata={"project_name": f"POOL{i % 7}"},
            consent=False,
        )
        for i in range(_BG_PATIENTS)
    ]
    pg_db.add_all(objs)
    # The probe patient's own DMP rows: one tumor + two normals (one
    # stored patient, one name-parsed), same pool.
    _mk(pg_db, f"{_PROBE_PATIENT}-T01-IM6", patient__dmp=_PROBE_PATIENT,
        meta={"pool_id": "POOLP"}, ton="TUMOR")
    _mk(pg_db, f"{_PROBE_PATIENT}-N01-IM6", patient__dmp=_PROBE_PATIENT,
        meta={"pool_id": "POOLP"})
    _mk(pg_db, f"{_PROBE_PATIENT}-N02-IM6", patient__dmp=None,
        meta={"pool_id": "POOLP"})
    pg_db.execute(text("ANALYZE samples"))
    pg_db.commit()
    own_rows = pg_db.execute(
        text("SELECT count(*) FROM samples WHERE dmp_patient_key = :p"),
        {"p": _PROBE_PATIENT},
    ).scalar_one()
    return int(own_rows)


def test_perf_guard_candidate_query_uses_index_scan(pg_db, captured_sql):
    """The per-tumor candidate query must stay an index scan returning only
    the patient's own DMP rows (card 1's generated key + partial index).

    Guards against regression to the pre-fix shape measured on staging
    (2026-11-13): Parallel Seq Scan, 167 ms, 75,047 rows returned per
    tumor — the IS-NULL arm fetched table-scale rows. All three
    assertions must hold:
      (a) the plan is served by ``ix_samples_dmp_patient_key``;
      (b) no Seq Scan on ``samples`` in the plan;
      (c) the actual row count is bounded by a multiple of the patient's
          own DMP rows (row-count bound), never table scale.
    """
    own_rows = _seed_guard_corpus(pg_db)
    assert _has_dmp_generated_columns(pg_db) is True, (
        "expected the generated columns on this Postgres DB"
    )
    row_bound = 2 * own_rows + 2

    # The real per-tumor candidate query, captured on the engine.
    captured_sql.clear()
    rows = strategies["dmp"]._same_patient_dmp(
        pg_db, TumorOrNormal.NORMAL.value, _PROBE_PATIENT
    )
    # Correctness: only the patient's own DMP normals, both key shapes.
    assert {s.primary_id for s in rows} == {
        f"{_PROBE_PATIENT}-N01-IM6",
        f"{_PROBE_PATIENT}-N02-IM6",
    }
    hits = [
        (stmt, params)
        for stmt, params in captured_sql
        if "FROM samples" in stmt and "dmp_patient_key = %(" in stmt
    ]
    assert hits, "no generated-path candidate query was captured on the engine"
    stmt, params = hits[-1]

    plan = _explain_analyze_json(pg_db.get_bind(), stmt, params)

    # (a) served by the partial index.
    assert _plan_index_nodes(plan), (
        f"candidate query is not served by {_EXPECTED_INDEX}: {plan!r}"
    )
    # (b) no table scan.
    assert not _plan_seq_scans_on(plan, "samples"), (
        f"Seq Scan on samples in the candidate query plan (pre-fix "
        f"regression shape): {plan!r}"
    )
    # (c) row-count bound proportional to the patient's own DMP rows.
    max_rows = _plan_max_actual_rows(plan)
    assert max_rows <= row_bound, (
        f"candidate query returned {max_rows} rows (bound {row_bound}; the "
        f"patient has {own_rows} own DMP rows) — table-scale fetch, the "
        f"pre-fix regression shape. plan={plan!r}"
    )


# The pre-fix candidate shape (stored-column filter + IS-NULL arm) — what
# the legacy Python-remap path fetched per tumor before card 1.
_LEGACY_CANDIDATE_SQL = (
    "SELECT * FROM samples "
    "WHERE tumor_or_normal = 'NORMAL' "
    "AND trim(upper(sample_type)) = 'DMP' "
    "AND (patient__dmp = %(p)s OR patient__dmp IS NULL) "
    "ORDER BY primary_id"
)


def test_perf_guard_would_fail_on_legacy_query_shape(pg_db, captured_sql):
    """Negative control (guard sensitivity): the pre-fix candidate shape
    must violate the perf guard's assertions on the same seed — it is not
    served by the partial index and its row count is table-scale (the
    fixture-scale analogue of staging's 75,047 returned rows). Proves the
    guard catches the regression without regressing production code."""
    own_rows = _seed_guard_corpus(pg_db)
    row_bound = 2 * own_rows + 2

    # The IS-NULL arm fetches every NULL-patient DMP row in the table.
    legacy_rows = pg_db.execute(
        text(
            "SELECT count(*) FROM samples "
            "WHERE tumor_or_normal = 'NORMAL' "
            "AND trim(upper(sample_type)) = 'DMP' "
            "AND (patient__dmp = :p OR patient__dmp IS NULL)"
        ),
        {"p": _PROBE_PATIENT},
    ).scalar_one()
    assert legacy_rows > row_bound, (
        f"seed is no longer table-scale ({legacy_rows} rows vs bound "
        f"{row_bound}); the row-count guard would be vacuous"
    )

    plan = _explain_analyze_json(
        pg_db.get_bind(), _LEGACY_CANDIDATE_SQL, {"p": _PROBE_PATIENT}
    )
    # The guard's assertions would fail on this plan:
    assert not _plan_index_nodes(plan), (
        f"negative control: legacy shape unexpectedly used {_EXPECTED_INDEX}: "
        f"{plan!r}"
    )
    max_rows = _plan_max_actual_rows(plan)
    assert max_rows > row_bound, (
        f"negative control: legacy shape returned only {max_rows} rows "
        f"(bound {row_bound}) — the row-count guard would not catch the "
        f"regression. plan={plan!r}"
    )


# ---------------------------------------------------------------------------
# 2. End-to-end equivalence harness (cards 1-3 together)
# ---------------------------------------------------------------------------

# Expected results over the full fixture corpus: card 1's corpus (P-9001..
# P-9004, the non-DMP/exclusion rows, the unparseable X-T1) plus two
# unpaired tumors added below — P-9005 (pool, no DMP normal) and P-9006
# (no pool, normal on a different IM_ver) — so the harness pins the
# unpaired set, not just the paired set.
EXPECTED_PAIRS = {
    ("P-9001-T01-IM6", "P-9001-N01-IM6"),
    ("P-9001-T01-IM6", "P-9001-N02-IM6"),
    ("P-9002-T01-IM6", "P-9002-N01-IM6"),
    ("P-9003-T01-IM6", "P-9003-N01-IM6"),
    ("P-9004-T01-IM5", "P-9004-N01-IM5"),
}
EXPECTED_UNPAIRED_TUMORS = {"P-9005-T01-IM6", "P-9006-T01-IM6"}
EXPECTED_MISSING_PATIENT = {"X-T1"}
EXPECTED_SCANNED = 7
EXPECTED_COUNTERS = {
    "scanned": 7,
    "total": 7,
    "already_paired": 0,
    "missing_patient__dmp": 1,
    "paired": 4,
    "unpaired": 2,
    "needs_follow_up": False,
}


def _seed_e2e_corpus(pg_db) -> dict:
    """Card 1's full fixture corpus + the two unpaired tumors."""
    corpus = _build_corpus(pg_db)
    corpus["t9005"] = _mk(pg_db, "P-9005-T01-IM6", patient__dmp="P-9005",
                          meta={"project_name": "POOLE"}, ton="TUMOR")
    corpus["t9006"] = _mk(pg_db, "P-9006-T01-IM6", patient__dmp="P-9006",
                          ton="TUMOR")
    corpus["n9006"] = _mk(pg_db, "P-9006-N01-IM7", patient__dmp="P-9006")
    return corpus


def _dmp_tumors(pg_db) -> list:
    rows = pg_db.scalars(
        select(Sample).where(Sample.tumor_or_normal == TumorOrNormal.TUMOR.value)
    ).all()
    return [t for t in rows if _is_dmp(t)]


def _wait_job_terminal(job_id: str, deadline: float = 30.0) -> dict:
    started = time.monotonic()
    while True:
        status = pairing_job_status_endpoint(job_id)
        if status["status"] in ("completed", "failed"):
            return status
        if time.monotonic() - started > deadline:
            raise AssertionError(
                f"pairing job {job_id} did not finish in {deadline}s: {status}"
            )
        time.sleep(0.01)


def _wait_no_active_passes(deadline: float = 5.0) -> None:
    """The worker releases its pass claim in a finally that runs after the
    job record reaches a terminal state — poll briefly for the release."""
    started = time.monotonic()
    while pairings._active_pairing_passes:
        if time.monotonic() - started > deadline:
            raise AssertionError(
                f"pass claim not released within {deadline}s: "
                f"{pairings._active_pairing_passes}"
            )
        time.sleep(0.01)


def _run_dmp_pass(pg_db, logic, captured_sql, deadline: float = 30.0):
    """One full DMP bulk pass over the background-job path (trigger
    endpoint -> worker thread -> bulk pass -> batched commits). Returns
    (job_id, final job status, latest pair set, unpaired DMP tumor pids)."""
    pg_db.rollback()
    with pg_db.get_bind().begin() as conn:
        conn.execute(PairingTable.__table__.delete())
    pg_db.expire_all()
    captured_sql.clear()

    job = trigger_dmp_pairing_endpoint(db=pg_db)
    assert job["status"] == "pending", job
    status = _wait_job_terminal(job["job_id"], deadline)
    assert status["status"] == "completed", status

    rows = pg_db.scalars(
        select(PairingTable).where(
            PairingTable.pairing_logic_id == logic.id,
            PairingTable.latest == True,  # noqa: E712
        )
    ).all()
    assert all(r.multi_pair for r in rows), "DMP pairings must be multi_pair rows"
    pair_set = {(r.tumor.primary_id, r.normal.primary_id) for r in rows}
    paired_tumor_ids = {r.tumor_id for r in rows}
    unpaired = {
        t.primary_id for t in _dmp_tumors(pg_db)
        if t.id not in paired_tumor_ids
    }
    return job["job_id"], status, pair_set, unpaired


def test_e2e_bulk_pass_equivalence_generated_vs_legacy(
    pg_db, monkeypatch, captured_sql
):
    """End-to-end equivalence: one DMP bulk pass over the full fixture
    corpus, run twice through the complete cards 2-3 plumbing (background
    job + batched commits) — once forced down the pre-fix legacy
    candidate query, once on card 1's generated-column path. The
    paired/unpaired sets and the job counters must be identical in both
    runs and match the pre-change two-tier reference rule."""
    _seed_e2e_corpus(pg_db)

    logic = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    pg_db.add(logic)
    pg_db.commit()
    seed_pooled_machines(pg_db)

    # Ground truth: the pre-change two-tier rule, hand-rolled, over the
    # full corpus — independent of the app's pass machinery.
    ref_pairs = set()
    for tumor in _dmp_tumors(pg_db):
        for normal_id in _reference_row_set(pg_db, tumor):
            ref_pairs.add(
                (tumor.primary_id, pg_db.get(Sample, normal_id).primary_id)
            )
    assert ref_pairs == EXPECTED_PAIRS, (
        f"reference rule disagrees with the pinned expectations: {ref_pairs}"
    )
    assert len(_dmp_tumors(pg_db)) == EXPECTED_SCANNED

    # --- run 1: pre-fix legacy candidate query (forced) -----------------
    monkeypatch.setattr(
        "syzygy.pairing_strategies._has_dmp_generated_columns",
        lambda db: False,
    )
    monkeypatch.setattr(
        pairings, "SessionLocal",
        sessionmaker(bind=pg_db.get_bind(), expire_on_commit=False),
    )
    job1, status1, pairs1, unpaired1 = _run_dmp_pass(pg_db, logic, captured_sql)
    assert any("patient__dmp IS NULL" in stmt for stmt, _ in captured_sql), (
        "run 1 never executed the legacy candidate query"
    )
    monkeypatch.undo()

    # --- run 2: card 1's generated-column path (unforced) ---------------
    assert _has_dmp_generated_columns(pg_db) is True
    monkeypatch.setattr(
        pairings, "SessionLocal",
        sessionmaker(bind=pg_db.get_bind(), expire_on_commit=False),
    )
    job2, status2, pairs2, unpaired2 = _run_dmp_pass(pg_db, logic, captured_sql)
    assert any("dmp_patient_key = %(" in stmt for stmt, _ in captured_sql), (
        "run 2 never executed the generated-column candidate query"
    )

    # --- equivalence: identical results pre/post change ------------------
    assert pairs1 == pairs2, f"pair sets diverge pre/post change: {pairs1 ^ pairs2}"
    assert pairs1 == EXPECTED_PAIRS, f"unexpected pair set: {pairs1}"
    assert unpaired1 == unpaired2, (
        f"unpaired sets diverge pre/post change: {unpaired1 ^ unpaired2}"
    )
    assert unpaired1 == EXPECTED_UNPAIRED_TUMORS | EXPECTED_MISSING_PATIENT

    for status in (status1, status2):
        for key, expected in EXPECTED_COUNTERS.items():
            assert status[key] == expected, (
                f"{key}: {status[key]!r} != {expected!r} (status={status})"
            )

    # Both runs left no dangling claims; both job records are registered.
    _wait_no_active_passes()
    assert not pairings._active_pairing_passes
    for job_id in (job1, job2):
        assert job_id in pairings._pairing_jobs
