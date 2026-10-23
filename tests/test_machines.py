"""Tests for the pooled_machines reference table API."""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from pairing_strategies import DEFAULT_POOLED_MACHINES, seed_pooled_machines
from routers.machines import list_machines, upsert_machine
from schemas import PooledMachineUpsert


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
    seed_pooled_machines(db)
    assert len(list_machines(db=db)) == len(DEFAULT_POOLED_MACHINES)


def test_upsert_creates_then_updates(db):
    row = upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="hiseq", machine_type="HiSeq 3000"),
        db=db,
    )
    assert row.machine_name == "bono"
    assert row.machine_class == "hiseq"
    again = upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="novaseq"), db=db
    )
    assert again.id == row.id
    assert again.machine_class == "novaseq"
    assert again.machine_type is None
    assert len(list_machines(db=db)) == 1


def test_upsert_normalizes_case_and_whitespace(db):
    row = upsert_machine(
        PooledMachineUpsert(machine_name="  DIANA ", machine_class="NoVaSeq"), db=db
    )
    assert row.machine_name == "diana"
    assert row.machine_class == "novaseq"


def test_upsert_requires_fields(db):
    with pytest.raises(HTTPException) as e:
        upsert_machine(PooledMachineUpsert(machine_name="", machine_class="novaseq"), db=db)
    assert e.value.status_code == 400
