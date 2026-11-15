"""Tests for the bulk-pairing pass guard and the DB pool / threadpool caps.

A bulk pairing pass holds one pooled DB connection for its whole multi-minute
loop. Before the guard, a re-click (or a second tab) started a SECOND full pass
and a second checkout, until the app raised ``QueuePool limit of size 5 overflow
10 reached ... timeout 30.00`` mid-request. ``syzygy/database.py`` now sizes the pool
explicitly, ``syzygy/main.py`` caps the sync-endpoint threadpool to fit inside it, and
the trigger endpoints claim one pass per logic (409 otherwise). These tests pin
all three so a later "optimization" cannot quietly undo any of them.
"""

import time
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.database as database
import syzygy.pairing_service as pairing_service
import syzygy.routers.pairings as pairings
from syzygy.models import Base, PairingLogic, Sample
from syzygy.pairing_strategies import seed_pooled_machines
from syzygy.routers.pairings import (
    trigger_all_pairings_endpoint,
    trigger_dmp_pairing_endpoint,
    trigger_harmony_pairing_endpoint,
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = Session(engine)
    with session.begin():
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
    yield session
    session.close()
    # A test that leaves a pass claimed would poison the next one.
    pairings._active_pairing_passes.clear()


def _logic(db, name):
    logic = PairingLogic(name=name, strategy_name="dmp", active=True)
    db.add(logic)
    db.commit()
    return logic


def _dmp_tumor_and_normal(db, pid="P-4201"):
    """A pairable DMP tumor + same-patient normal (the DMP strategy pairs
    these via the same-patient / IM_ver tier)."""
    tumor = Sample(
        primary_id=f"{pid}-T01-IM6",
        cmo_sample_name=f"{pid} tumor name",
        patient__dmp=pid,
        anon_id=f"anon-{pid}-t",
        sample_type="DMP",
        gene_panel="IMPACT468",
        tumor_or_normal="TUMOR",
        consent=False,
    )
    normal = Sample(
        primary_id=f"{pid}-N01-IM6",
        cmo_sample_name=f"{pid} normal name",
        patient__dmp=pid,
        anon_id=f"anon-{pid}-n",
        sample_type="DMP",
        gene_panel="IMPACT468",
        tumor_or_normal="NORMAL",
        consent=False,
    )
    db.add_all([tumor, normal])
    db.commit()
    db.refresh(tumor)
    db.refresh(normal)
    return tumor, normal


def _bulk_result(logic_name, **overrides):
    """A complete bulk-pass result dict (the worker's contract)."""
    result = {
        "pairing_logic": logic_name,
        "scanned": 1,
        "total": 1,
        "already_paired": 0,
        "missing_patient__dmp": 0,
        "paired": 1,
        "unpaired": 0,
        "new_pairings": [],
        "needs_follow_up": False,
    }
    result.update(overrides)
    return result


def _test_session_factory(db):
    """A ``SessionLocal`` replacement bound to the test engine — the job
    worker must never reach for the app's real engine."""
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def _wait_job_terminal(job_id, deadline=5.0):
    """Poll the status endpoint until the job reaches a terminal state."""
    end = time.monotonic() + deadline
    status = None
    while True:
        status = pairings.pairing_job_status_endpoint(job_id)
        if status["status"] in ("completed", "failed"):
            return status
        if time.monotonic() >= end:
            raise AssertionError(
                f"job {job_id} not terminal within {deadline}s: {status}"
            )
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Pool sizing: the threadpool must never outnumber the pool
# ---------------------------------------------------------------------------


def test_threadpool_tokens_fit_inside_the_pool():
    """Every sync-endpoint thread can hold a connection for its whole request,
    so in-flight DB work has to fit inside pool_size + max_overflow."""
    assert database.THREADPOOL_TOKENS <= database.POOL_SIZE + database.POOL_MAX_OVERFLOW


def test_app_engine_uses_the_explicit_pool_settings():
    """The QueuePool defaults (5 + 10, 30 s) are what failed; the engine must
    keep carrying the explicit sizing."""
    if database.engine.dialect.name != "postgresql":
        pytest.skip("pool sizing only concerns the Postgres app engine")
    pool = database.engine.pool
    assert pool.size() == database.POOL_SIZE
    assert pool.timeout() == database.POOL_TIMEOUT_SECONDS
    assert pool._max_overflow == database.POOL_MAX_OVERFLOW


# ---------------------------------------------------------------------------
# One bulk pairing pass per logic
# ---------------------------------------------------------------------------


def test_pass_claims_and_releases_its_logic(db):
    logic = _logic(db, "HARMONY")
    with pairings._pairing_pass(db, logic, "HARMONY"):
        assert logic.id in pairings._active_pairing_passes
    assert logic.id not in pairings._active_pairing_passes


def test_claim_is_released_even_when_the_pass_raises(db):
    """A pass that blows up must not leave its logic permanently claimed —
    every later trigger would answer 409 with nothing running."""
    logic = _logic(db, "HARMONY")
    with pytest.raises(RuntimeError):
        with pairings._pairing_pass(db, logic, "HARMONY"):
            raise RuntimeError("pairing pass blew up mid-run")
    assert logic.id not in pairings._active_pairing_passes


def test_second_harmony_trigger_is_rejected_without_a_second_pass(db, monkeypatch):
    """409 from the endpoint, and the pairing function is never entered again."""
    logic = _logic(db, "HARMONY")
    started = []
    monkeypatch.setattr(
        pairings, "trigger_harmony_pairing", lambda db: started.append(1) or {}
    )
    pairings._active_pairing_passes.add(logic.id)  # a pass already in flight
    with pytest.raises(HTTPException) as exc:
        trigger_harmony_pairing_endpoint(db=db)
    assert exc.value.status_code == 409
    assert "already in progress" in exc.value.detail
    assert started == []
    # The running pass keeps its claim; the rejected caller did not take one.
    assert logic.id in pairings._active_pairing_passes


def test_first_harmony_trigger_returns_job_id_and_worker_releases_the_claim(
    db, monkeypatch
):
    """The endpoint returns a job id immediately; the pass runs on a worker
    thread and releases its claim when the pass is done."""
    logic = _logic(db, "HARMONY")
    monkeypatch.setattr(
        pairings,
        "trigger_harmony_pairing",
        lambda db, progress=None: _bulk_result("HARMONY"),
    )
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    result = trigger_harmony_pairing_endpoint(db=db)
    assert result["pairing_logic"] == "HARMONY"
    assert result["status"] == "pending"
    assert result["job_id"]

    status = _wait_job_terminal(result["job_id"])
    assert status["status"] == "completed"
    assert status["paired"] == 1
    assert status["scanned"] == 1
    assert status["total"] == 1
    assert status["started_at"] is not None
    assert status["finished_at"] is not None
    # The claim is released once the worker's pass is done.
    assert logic.id not in pairings._active_pairing_passes


def test_dmp_and_harmony_passes_do_not_block_each_other(db, monkeypatch):
    """Separate pairing spaces get separate claims: a DMP job in flight must
    not 409 a HARMONY trigger (and vice versa)."""
    _logic(db, "DMP")
    _logic(db, "HARMONY")
    monkeypatch.setattr(
        pairings, "trigger_dmp_pairing", lambda db, progress=None: _bulk_result("DMP")
    )
    monkeypatch.setattr(
        pairings,
        "trigger_harmony_pairing",
        lambda db, progress=None: _bulk_result("HARMONY", paired=2),
    )
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    dmp_job = trigger_dmp_pairing_endpoint(db=db)
    harmony_job = trigger_harmony_pairing_endpoint(db=db)
    assert dmp_job["pairing_logic"] == "DMP"
    assert harmony_job["pairing_logic"] == "HARMONY"

    dmp_status = _wait_job_terminal(dmp_job["job_id"])
    harmony_status = _wait_job_terminal(harmony_job["job_id"])
    assert dmp_status["status"] == "completed" and dmp_status["paired"] == 1
    assert harmony_status["status"] == "completed" and harmony_status["paired"] == 2
    assert pairings._active_pairing_passes == set()


def test_bulk_backfill_blocks_a_second_bulk_backfill(db, monkeypatch):
    """Unscoped trigger-all claims a shared key: running the same whole-sample
    backfill twice is the worst version of this failure."""
    started = []
    monkeypatch.setattr(
        pairings,
        "trigger_pairing_for_unpaired_tumors",
        lambda db, logic_name=None: started.append(1) or {},
    )
    pairings._active_pairing_passes.add(None)
    with pytest.raises(HTTPException) as exc:
        trigger_all_pairings_endpoint(db=db)
    assert exc.value.status_code == 409
    assert started == []


def test_scoped_backfill_shares_the_claim_of_that_logics_trigger(db, monkeypatch):
    """``trigger-all?pairing_logic_name=HARMONY`` pairs the same space as
    ``trigger-harmony``, so one claim has to cover both."""
    logic = _logic(db, "HARMONY")
    monkeypatch.setattr(
        pairings,
        "trigger_harmony_pairing",
        lambda db, progress=None: _bulk_result("HARMONY", paired=3),
    )
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))
    job = trigger_harmony_pairing_endpoint(db=db)
    assert _wait_job_terminal(job["job_id"])["paired"] == 3
    pairings._active_pairing_passes.add(logic.id)  # a pass already in flight
    with pytest.raises(HTTPException) as exc:
        trigger_all_pairings_endpoint(pairing_logic_name="HARMONY", db=db)
    assert exc.value.status_code == 409


def test_second_dmp_trigger_while_job_running_409s(db, monkeypatch):
    """The overlap guard survives the async refactor: while the first pass is
    running in the background, a second same-logic trigger still 409s — and
    the first job finishes normally afterwards."""
    _logic(db, "DMP")
    seed_pooled_machines(db)
    _dmp_tumor_and_normal(db)

    entered = threading.Event()
    release = threading.Event()
    real_trigger = pairing_service.trigger_pairing

    def gated(db_session, tumor, logic_row, trigger_type, *args, **kwargs):
        if not entered.is_set():
            entered.set()
        release.wait(timeout=10)
        return real_trigger(db_session, tumor, logic_row, trigger_type, *args, **kwargs)

    monkeypatch.setattr(pairing_service, "trigger_pairing", gated)
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    job = trigger_dmp_pairing_endpoint(db=db)
    assert job["status"] == "pending"
    assert entered.wait(timeout=5), "worker never entered the pass"

    try:
        # The pass is running: a second DMP trigger must 409.
        with pytest.raises(HTTPException) as exc:
            trigger_dmp_pairing_endpoint(db=db)
        assert exc.value.status_code == 409
        assert "already in progress" in exc.value.detail
    finally:
        release.set()

    status = _wait_job_terminal(job["job_id"])
    assert status["status"] == "completed"
    assert status["paired"] == 1
    assert status["scanned"] == 1
    assert status["total"] == 1
    assert status["needs_follow_up"] is False
    assert not pairings._active_pairing_passes


def test_status_reports_progress_while_running_and_final_state(db, monkeypatch):
    """The status endpoint reports progress (scanned/total, paired, unpaired)
    while a job runs and the final state when it is done."""
    _logic(db, "DMP")
    seed_pooled_machines(db)
    for i in range(3):
        _dmp_tumor_and_normal(db, pid=f"P-{i:04d}")

    entered = threading.Event()
    release = threading.Event()
    real_trigger = pairing_service.trigger_pairing
    calls = []

    def gated(db_session, tumor, logic_row, trigger_type, *args, **kwargs):
        calls.append(tumor.id)
        if len(calls) == 1 and not entered.is_set():
            entered.set()
            release.wait(timeout=10)
        return real_trigger(db_session, tumor, logic_row, trigger_type, *args, **kwargs)

    monkeypatch.setattr(pairing_service, "trigger_pairing", gated)
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    job = trigger_dmp_pairing_endpoint(db=db)
    assert job["status"] == "pending"
    assert entered.wait(timeout=5), "worker never reached the first tumor"
    try:
        # Mid-pass: the job is running, the main list is known (total), and
        # no tumor has settled yet (scanned still 0 — the gate blocks the
        # first trigger_pairing call, so the first settle has not happened).
        running = pairings.pairing_job_status_endpoint(job["job_id"])
        assert running["status"] == "running"
        assert running["started_at"] is not None
        assert running["finished_at"] is None
        assert running["total"] == 3
        assert 0 <= running["scanned"] < 3
        assert running["paired"] == 0
    finally:
        release.set()

    final = _wait_job_terminal(job["job_id"])
    assert final["status"] == "completed"
    assert final["scanned"] == 3
    assert final["total"] == 3
    assert final["paired"] == 3
    assert final["unpaired"] == 0
    assert final["finished_at"] is not None
    assert final["needs_follow_up"] is False


def test_end_of_pass_recheck_picks_up_mid_pass_ingest(db, monkeypatch):
    """The frozen-snapshot fix, mid-pass half: a DMP tumor ingested WHILE
    the pass is running (created on/after pass start, after the main list
    is already materialized) is not in the main list — only the end-of-pass
    re-check can see it. It must be swept by the re-check and paired, not
    silently missed with an under-reported ``scanned`` (the staging bug)."""
    _logic(db, "DMP")
    seed_pooled_machines(db)
    _dmp_tumor_and_normal(db, pid="P-5001")  # pre-pass tumor: in the main list

    first_done = threading.Event()
    release = threading.Event()
    real_trigger = pairing_service.trigger_pairing
    calls = []

    def gated(db_session, tumor, logic_row, trigger_type, *args, **kwargs):
        calls.append(tumor.id)
        if len(calls) == 1 and not first_done.is_set():
            first_done.set()
            release.wait(timeout=10)
        return real_trigger(db_session, tumor, logic_row, trigger_type, *args, **kwargs)

    monkeypatch.setattr(pairing_service, "trigger_pairing", gated)
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    job = trigger_dmp_pairing_endpoint(db=db)
    assert job["status"] == "pending"
    # Block the worker on the first tumor's pairing: the main list is
    # already materialized and the pass is mid-flight when the ingest lands.
    assert first_done.wait(timeout=5), "worker never reached the first tumor"
    try:
        _dmp_tumor_and_normal(db, pid="P-5002")  # mid-pass ingest
    finally:
        release.set()

    status = _wait_job_terminal(job["job_id"])
    assert status["status"] == "completed", status
    # Both tumors scanned and paired: the main list plus the re-check sweep.
    assert status["scanned"] == 2, status
    assert status["total"] == 2
    assert status["paired"] == 2
    assert status["unpaired"] == 0
    assert status["needs_follow_up"] is False
    assert not pairings._active_pairing_passes


def test_job_rescans_the_tumor_list_at_start_not_at_request_time(db, monkeypatch):
    """The frozen-snapshot fix: the tumor list is (re-)scanned when the
    worker starts the pass, not at HTTP-request time. A tumor ingested after
    the trigger request — while the job is still pending — must be picked up."""
    _logic(db, "DMP")
    seed_pooled_machines(db)

    opened = threading.Event()
    release = threading.Event()
    real_session_factory = _test_session_factory(db)

    class GatedSessionLocal:
        """Blocks the worker before it opens its session, i.e. between the
        trigger request and the pass's tumor-list materialization."""

        def __call__(self):
            if not opened.is_set():
                opened.set()
            release.wait(timeout=10)
            return real_session_factory()

    monkeypatch.setattr(pairings, "SessionLocal", GatedSessionLocal())

    job = trigger_dmp_pairing_endpoint(db=db)
    assert job["status"] == "pending"
    assert opened.wait(timeout=5), "worker never reached its session"
    try:
        # In-flight import: ingested after the trigger request, before the
        # pass's main list is materialized.
        _dmp_tumor_and_normal(db)
    finally:
        release.set()

    status = _wait_job_terminal(job["job_id"])
    # The mid-import tumor was scanned and paired: the list was re-scanned at
    # job start, so the pre-request (empty) state did not freeze the pass.
    assert status["status"] == "completed"
    assert status["scanned"] == 1
    assert status["total"] == 1
    assert status["paired"] == 1


def test_job_marks_failed_and_releases_claim_when_the_pass_raises(db, monkeypatch):
    """A pass that blows up in the worker marks the job failed and releases
    its claim, so a later trigger can start a fresh pass."""
    logic = _logic(db, "HARMONY")

    def boom(db, progress=None):
        raise RuntimeError("pass exploded in the worker")

    monkeypatch.setattr(pairings, "trigger_harmony_pairing", boom)
    monkeypatch.setattr(pairings, "SessionLocal", _test_session_factory(db))

    job = trigger_harmony_pairing_endpoint(db=db)
    status = _wait_job_terminal(job["job_id"])
    assert status["status"] == "failed"
    assert "pass exploded in the worker" in (status.get("error") or "")
    assert status["finished_at"] is not None
    assert logic.id not in pairings._active_pairing_passes
