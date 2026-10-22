"""Tests for the bait_sets table and classification rules."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bait_sets import (
    SEED_BAIT_SETS,
    classify_bait_set,
    resolve_bait_set_logic,
    seed_bait_sets,
)
from database import Base
from models import BaitSet, PairingLogic


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for name, strat in (("ARGOS", "argos"), ("TEMPO", "tempo"), ("ACCESS", "default")):
        session.add(PairingLogic(name=name, strategy_name=strat))
    session.commit()
    yield session
    session.close()


def test_classify_rules():
    assert classify_bait_set("IDT_Exome_v2_BAITS") == "TEMPO"
    assert classify_bait_set("CMO-CH") == "ACCESS"
    assert classify_bait_set("MSK-ACCESS-v3-probes") == "ACCESS"
    assert classify_bait_set("MSK-IMPACT468_baits") == "ARGOS"
    assert classify_bait_set("HEMEPACT_v4_BAITS") == "ARGOS"
    assert classify_bait_set("something_else") is None
    assert classify_bait_set("") is None


def test_seed_idempotent(db):
    seed_bait_sets(db)
    seed_bait_sets(db)
    rows = db.scalars(select(BaitSet)).all()
    assert len(rows) == len(SEED_BAIT_SETS) == 11
    by = {r.bait_set: r.pairing_logic for r in rows}
    assert by["IDT_Exome_v1_FP_BAITS"] == "TEMPO"
    assert by["MSK-ACCESS-v1_0-probesAllwFP"] == "ACCESS"


def test_resolve_registers_rule_matches(db):
    seed_bait_sets(db)
    assert resolve_bait_set_logic(db, "MSK-IMPACT505").name == "ARGOS"
    assert db.scalar(select(BaitSet).where(BaitSet.bait_set == "MSK-IMPACT505")) is not None
    assert resolve_bait_set_logic(db, "unknown_bait") is None
    assert resolve_bait_set_logic(db, None) is None
