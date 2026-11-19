"""Tests for scripts/register_bait_sets.py (ticket P4).

Covers the backfill contract: dry-run default (no rows written), --apply
inserts only CLASSIFIABLE unregistered names, unclassifiable names are
reported NOT inserted (fail-closed preserved), case-insensitive
already-registered skipping (find_bait_set semantics), missing
pairing-logic rows skipped, and idempotency (a second --apply inserts
nothing). The dev-box acceptance (IMPACT468/IMPACT505_BAITS visible to
ARGOS-open users after --apply) is exercised against the dev database
per the ticket's Accept list, not here.
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import register_bait_sets as rbs
from syzygy import database as syzygy_database
from syzygy.bait_sets import logic_membership_condition
from syzygy.database import Base
from syzygy.models import BaitSet, PairingLogic, Sample

ALL_LOGICS = (
    ("ARGOS", "argos"),
    ("TEMPO", "tempo"),
    ("ACCESS", "default"),
    ("DMP", "dmp"),
    ("HARMONY", "dmp"),
)


def _sample(db, bait_set, n=1, sample_type="LIMS", panel="panel"):
    for i in range(n):
        db.add(
            Sample(
                primary_id=f"{sample_type}-{bait_set}-{i}",
                cmo_sample_name=f"{bait_set}-{i}",
                sample_type=sample_type,
                gene_panel=panel,
                tumor_or_normal="TUMOR",
                bait_set=bait_set,
            )
        )
    db.commit()


def _seed(session):
    for name, strat in ALL_LOGICS:
        session.add(PairingLogic(name=name, strategy_name=strat))
    # One pre-registered name (case-insensitive skip must cover it); the
    # fixture pins it to ARGOS, so its sample is ARGOS-visible.
    argos = session.scalar(select(PairingLogic).where(PairingLogic.name == "ARGOS"))
    session.add(
        BaitSet(bait_set="IDT_Exome_v2_BAITS", pairing_logic_id=argos.id)
    )
    # Unregistered values in samples:
    _sample(session, "IMPACT468", n=2)               # classifiable -> ARGOS
    _sample(session, "IMPACT505_BAITS", n=3)         # classifiable -> ARGOS
    _sample(session, "MSK-ACCESS-v3-probes", n=1)    # classifiable -> ACCESS
    _sample(session, "mystery_panel", n=1)           # UNCLASSIFIABLE
    _sample(session, "IDT_Exome_v2_BAITS", n=1)      # already registered


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)
    yield session
    session.close()


def _rows(db):
    return {r.bait_set: r.pairing_logic for r in db.scalars(select(BaitSet)).all()}


# --------------------------------------------------------------------------
# dry-run default
# --------------------------------------------------------------------------


def test_dry_run_inserts_nothing(db, capsys):
    rc = rbs.run(db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would insert 'IMPACT468' -> ARGOS" in out
    assert "would insert 'MSK-ACCESS-v3-probes' -> ACCESS" in out
    rows = _rows(db)
    assert "IMPACT468" not in rows
    assert "IMPACT505_BAITS" not in rows
    assert "MSK-ACCESS-v3-probes" not in rows
    # pre-registered row untouched, no duplicates
    assert rows["IDT_Exome_v2_BAITS"] == "ARGOS"
    assert "mystery_panel" in out and "NOT inserted" in out


# --------------------------------------------------------------------------
# --apply: classifiable only
# --------------------------------------------------------------------------


def test_apply_registers_classifiable_only(db, capsys):
    rc = rbs.run(db, apply=True)
    assert rc == 0
    rows = _rows(db)
    assert rows["IMPACT468"] == "ARGOS"
    assert rows["IMPACT505_BAITS"] == "ARGOS"
    assert rows["MSK-ACCESS-v3-probes"] == "ACCESS"
    # fail-closed preserved: unclassifiable name NOT inserted
    assert "mystery_panel" not in rows
    out = capsys.readouterr().out
    assert "inserted 3 bait_sets row(s)" in out
    assert "UNCLASSIFIABLE 'mystery_panel'" in out
    assert "NOT inserted" in out


def test_apply_makes_classifiable_samples_visible(db):
    """After --apply the newly registered names pass the Layer B logic
    filter; the unclassifiable one still belongs to no logic."""
    rbs.run(db, apply=True)
    argos = db.scalar(select(PairingLogic).where(PairingLogic.name == "ARGOS"))
    visible = db.scalar(
        select(func.count())
        .select_from(Sample)
        .where(logic_membership_condition(argos))
    )
    assert visible == 6  # IMPACT468 (2) + IMPACT505_BAITS (3) + the
                         # fixture's pre-registered IDT_Exome_v2_BAITS (1, ARGOS)
    access = db.scalar(select(PairingLogic).where(PairingLogic.name == "ACCESS"))
    assert (
        db.scalar(
            select(func.count()).select_from(Sample).where(
                logic_membership_condition(access)
            )
        )
        == 1  # MSK-ACCESS-v3-probes only
    )
    # mystery_panel belongs to NO logic (fail-closed preserved)
    for logic in db.scalars(select(PairingLogic)).all():
        assert (
            db.scalar(
                select(func.count())
                .select_from(Sample)
                .where(
                    logic_membership_condition(logic),
                    Sample.bait_set == "mystery_panel",
                )
            )
            == 0
        )


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_apply_is_idempotent(db, capsys):
    rbs.run(db, apply=True)
    rows_after_first = _rows(db)
    capsys.readouterr()  # discard first-run output
    rbs.run(db, apply=True)
    rows_after_second = _rows(db)
    assert rows_after_first == rows_after_second
    out = capsys.readouterr().out
    assert "0 to register" in out
    assert "inserted 0 bait_sets row(s)" in out
    # the unclassifiable name is still reported on the second run
    assert "UNCLASSIFIABLE 'mystery_panel'" in out


def test_case_insensitive_registered_skip(db, capsys):
    """A sample value that differs only in case from a registered row is
    'registered' (find_bait_set matches case-insensitively) and must not
    be re-inserted under the other case."""
    rbs.run(db, apply=True)
    # rename the inserted row to a different case
    db.execute(
        text("update bait_sets set bait_set = 'impact468' where bait_set = 'IMPACT468'")
    )
    db.commit()
    # a sample with the canonical case must now be skipped as registered
    _sample(db, "IMPACT468", n=1)
    rc = rbs.run(db, apply=True)
    assert rc == 0
    rows = _rows(db)
    assert "IMPACT468" not in rows  # not re-inserted under the other case
    assert "impact468" in rows


# --------------------------------------------------------------------------
# classified but no pairing-logic row
# --------------------------------------------------------------------------


def test_classified_missing_logic_row_skipped(db, capsys):
    argos = db.scalar(select(PairingLogic).where(PairingLogic.name == "ARGOS"))
    db.delete(argos)
    db.commit()
    rc = rbs.run(db, apply=True)
    assert rc == 0
    rows = _rows(db)
    assert "IMPACT468" not in rows  # classifiable but no ARGOS row
    assert "MSK-ACCESS-v3-probes" in rows  # ACCESS row still exists
    out = capsys.readouterr().out
    assert "SKIPPED 'IMPACT468'" in out and "NOT inserted" in out


# --------------------------------------------------------------------------
# main(): argparse wiring (default dry-run, --apply)
# --------------------------------------------------------------------------


@pytest.fixture
def wired_db(tmp_path, monkeypatch):
    """Point syzygy.database at the same DB as a fresh seeded session so
    main() reads/writes the fixture data. File-backed sqlite on purpose:
    main() disposes the engine, and disposing an in-memory sqlite engine
    wipes the whole database out from under the assertions."""
    dbfile = tmp_path / "wired.db"
    engine = create_engine(f"sqlite:///{dbfile}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)
    monkeypatch.setattr(syzygy_database, "Base", Base)
    monkeypatch.setattr(syzygy_database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(syzygy_database, "engine", engine)
    yield session
    session.close()
    engine.dispose()


def test_main_dry_run_default(wired_db, capsys):
    rc = rbs.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert _rows(wired_db).get("IMPACT468") is None


def test_main_apply_flag(wired_db, capsys):
    rc = rbs.main(["--apply"])
    assert rc == 0
    rows = _rows(wired_db)
    assert rows.get("IMPACT468") == "ARGOS"
    assert rows.get("mystery_panel") is None
