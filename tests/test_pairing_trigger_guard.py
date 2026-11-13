"""Tests for the bulk-pairing pass guard and the DB pool / threadpool caps.

A bulk pairing pass holds one pooled DB connection for its whole multi-minute
loop. Before the guard, a re-click (or a second tab) started a SECOND full pass
and a second checkout, until the app raised ``QueuePool limit of size 5 overflow
10 reached ... timeout 30.00`` mid-request. ``syzygy/database.py`` now sizes the pool
explicitly, ``syzygy/main.py`` caps the sync-endpoint threadpool to fit inside it, and
the trigger endpoints claim one pass per logic (409 otherwise). These tests pin
all three so a later "optimization" cannot quietly undo any of them.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import syzygy.database as database
import syzygy.routers.pairings as pairings
from syzygy.models import Base, PairingLogic
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


def test_first_harmony_trigger_runs_and_then_releases(db, monkeypatch):
    logic = _logic(db, "HARMONY")
    monkeypatch.setattr(
        pairings, "trigger_harmony_pairing", lambda db: {"pairing_logic": "HARMONY"}
    )
    result = trigger_harmony_pairing_endpoint(db=db)
    assert result == {"pairing_logic": "HARMONY"}
    assert logic.id not in pairings._active_pairing_passes


def test_dmp_and_harmony_passes_do_not_block_each_other(db, monkeypatch):
    """Separate pairing spaces get separate claims."""
    _logic(db, "DMP")
    _logic(db, "HARMONY")
    monkeypatch.setattr(pairings, "trigger_dmp_pairing", lambda db: {"paired": 1})
    monkeypatch.setattr(pairings, "trigger_harmony_pairing", lambda db: {"paired": 2})
    assert trigger_dmp_pairing_endpoint(db=db)["paired"] == 1
    assert trigger_harmony_pairing_endpoint(db=db)["paired"] == 2
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
    monkeypatch.setattr(pairings, "trigger_harmony_pairing", lambda db: {"paired": 3})
    assert trigger_harmony_pairing_endpoint(db=db)["paired"] == 3
    pairings._active_pairing_passes.add(logic.id)  # a pass already in flight
    with pytest.raises(HTTPException) as exc:
        trigger_all_pairings_endpoint(pairing_logic_name="HARMONY", db=db)
    assert exc.value.status_code == 409
