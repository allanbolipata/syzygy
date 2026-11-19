"""Tests for the pooled_machines reference table API."""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.database import Base
from syzygy.pairing_strategies import DEFAULT_POOLED_MACHINES, seed_pooled_machines
from syzygy.routers.machines import list_machines, upsert_machine
from syzygy.schemas import PooledMachineUpsert


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_list_machines_empty(db):
    assert list_machines(db=db) == []


def test_seed_pooled_machines_idempotent(db):
    seed_pooled_machines(db)
    rows = list_machines(db=db)
    assert {r.machine_name for r in rows} == {n for n, _, _ in DEFAULT_POOLED_MACHINES}
    # The two NovaSeq X pool machines (beagle v2.3.0 "novaseq X change")
    # are registered as the novaseq family for the argos pooled tier.
    by_name = {r.machine_name: r for r in rows}
    for name in ("fauci2", "bono"):
        assert by_name[name].machine_class == "novaseq"
        assert by_name[name].machine_type == "NovaSeq X"
    seed_pooled_machines(db)
    assert len(list_machines(db=db)) == len(DEFAULT_POOLED_MACHINES)


def test_upsert_creates_then_updates(db):
    row = upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="hiseq", machine_type="HiSeq 3000"),
        db=db,
        current_user=None,
    )
    assert row.machine_name == "bono"
    assert row.machine_class == "hiseq"
    again = upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="novaseq"),
        db=db,
        current_user=None,
    )
    assert again.id == row.id
    assert again.machine_class == "novaseq"
    assert again.machine_type is None
    assert len(list_machines(db=db)) == 1


def test_upsert_normalizes_case_and_whitespace(db):
    row = upsert_machine(
        PooledMachineUpsert(machine_name="  DIANA ", machine_class="NoVaSeq"),
        db=db,
        current_user=None,
    )
    assert row.machine_name == "diana"
    assert row.machine_class == "novaseq"


def test_seed_does_not_overwrite_existing_rows(db):
    """The startup seed must not overwrite existing rows — including
    auto-registered 'unknown' rows created at ingest — so the two
    paths (seed + advisory auto-registration) cannot fight."""
    # Simulate an auto-registered row for a DEFAULT machine with a
    # class the seed would not set (ingest happened before the
    # operator set the real class).
    upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="unknown"), db=db
    )
    seed_pooled_machines(db)
    rows = list_machines(db=db)
    assert len(rows) == len(DEFAULT_POOLED_MACHINES)
    by_name = {r.machine_name: r for r in rows}
    assert by_name["bono"].machine_class == "unknown"
    # Seeding again stays idempotent.
    seed_pooled_machines(db)
    assert len(list_machines(db=db)) == len(DEFAULT_POOLED_MACHINES)
    by_name = {r.machine_name: r for r in list_machines(db=db)}
    assert by_name["bono"].machine_class == "unknown"


def test_upsert_requires_fields(db):
    with pytest.raises(HTTPException) as e:
        upsert_machine(PooledMachineUpsert(machine_name="", machine_class="novaseq"), db=db, current_user=None)
    assert e.value.status_code == 400
