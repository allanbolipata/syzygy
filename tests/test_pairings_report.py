"""Tests for the server-side Pair Report endpoint (GET /pairing/report/).

The endpoint returns one page of the *merged* report list (paired rows,
then — for DMP logics — the unmatched DMP samples rendered as
one-sided rows) plus the full-list ``total`` computed in SQL. The
frontend used to build this merge client-side (app.js
``appendUnmatchedDmpRows``): the unmatched DMP sample fills its natural
side (TUMOR -> tumor, NORMAL -> normal) and the opposite side is null.
These tests pin the total, page boundaries, the DMP vs non-DMP split,
and stable ordering.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from models import Base, PairingLogic, PairingTable, Sample
from routers.pairings import REPORT_MAX_PAGE_SIZE, pairing_report
from schemas import TumorOrNormal

BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _sample(db, primary_id, sample_type, tumor, created):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"CMO_{primary_id}",
        gene_panel="AC1",
        sample_type=sample_type,
        tumor_or_normal=tumor,
        created_date=created,
    )
    db.add(sample)
    db.flush()
    return sample


def _logic(db, name, strategy_name):
    logic = PairingLogic(name=name, strategy_name=strategy_name, active=True)
    db.add(logic)
    db.flush()
    return logic


def _pairing(db, logic, tumor, normal, created):
    p = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        latest=True,
        trigger_type="automatic",
        created_date=created,
    )
    db.add(p)
    db.flush()
    return p


def _side_ids(row):
    """(tumor_id, normal_id) of a report row, for order/disjoint checks."""
    return (
        row.tumor_id if row.tumor is not None else None,
        row.normal_id if row.normal is not None else None,
    )


# --- (a) total is correct for a mixed DMP fixture ---------------------------

def test_total_is_paired_plus_unmatched_dmp(db):
    logic = _logic(db, "DMP", "dmp")
    # Two latest pairings (both sides present) -> 2 paired rows.
    for i in range(2):
        tumor = _sample(db, f"T{i}", "DMP", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"N{i}", "DMP", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    # Three DMP samples with no latest pairing -> 3 unmatched rows.
    for i in range(3):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=i))

    result = pairing_report("DMP", 1, 50, db)
    # total is the full merged count (2 paired + 3 unmatched), SQL-derived.
    assert result.total == 5
    # Page 1 (size 50) carries every row.
    assert len(result.rows) == 5
    # Paired rows carry both sides; unmatched rows carry exactly one.
    both = sum(1 for r in result.rows if r.tumor is not None and r.normal is not None)
    one = sum(1 for r in result.rows if (r.tumor is not None) ^ (r.normal is not None))
    assert both == 2 and one == 3


def test_total_excludes_superseded_pairings(db):
    logic = _logic(db, "DMP", "dmp")
    tumor = _sample(db, "T0", "DMP", TumorOrNormal.TUMOR.value, BASE_DATE)
    normal_a = _sample(db, "N0", "DMP", TumorOrNormal.NORMAL.value, BASE_DATE)
    normal_b = _sample(db, "N1", "DMP", TumorOrNormal.NORMAL.value, BASE_DATE)
    # A superseded pairing must not count toward the total.
    stale = _pairing(db, logic, tumor, normal_a, BASE_DATE)
    stale.latest = False
    _pairing(db, logic, tumor, normal_b, BASE_DATE + timedelta(minutes=1))
    db.flush()

    result = pairing_report("DMP", 1, 50, db)
    # 1 latest paired row + normal_a (no longer referenced) unmatched = 2.
    assert result.total == 2


# --- (b) page boundaries with a small page_size -----------------------------

def test_page_boundaries_dmp(db):
    logic = _logic(db, "DMP", "dmp")
    # 3 paired rows, newest first: P2 (newest) -> P0 (oldest).
    paired = []
    for i in range(3):
        tumor = _sample(db, f"PT{i}", "DMP", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"PN{i}", "DMP", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        paired.append(_pairing(db, logic, tumor, normal,
                               BASE_DATE + timedelta(minutes=i)))
    # 4 unmatched DMP samples, newest first: U3 (newest) -> U0 (oldest).
    for i in range(4):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=i))

    assert pairing_report("DMP", 1, 30, db).total == 7

    page1 = pairing_report("DMP", 1, 3, db)
    page2 = pairing_report("DMP", 2, 3, db)
    page3 = pairing_report("DMP", 3, 3, db)

    # Page 1 = all 3 paired rows (both sides present).
    assert [r for r in page1.rows if r.tumor and r.normal] and len(page1.rows) == 3
    assert all(r.tumor is not None and r.normal is not None for r in page1.rows)
    # Page 2 = the first 3 unmatched rows (one side null each).
    assert len(page2.rows) == 3
    assert all((r.tumor is not None) ^ (r.normal is not None) for r in page2.rows)
    # Page 3 = the single remaining unmatched row.
    assert len(page3.rows) == 1
    assert (page3.rows[0].tumor is not None) ^ (page3.rows[0].normal is not None)

    # Pages are disjoint and together cover the whole list, in order.
    # Key on the sample IDs on each side (not the pairing FKs — unmatched
    # rows carry no pairing identity, so (tumor_id, normal_id) is
    # (None, None) for every one of them).
    seen = [
        (r.tumor.id if r.tumor else None, r.normal.id if r.normal else None)
        for page in (page1, page2, page3)
        for r in page.rows
    ]
    assert len(seen) == 7 and len(set(seen)) == 7
    # Merged order is paired-first, each section by created_date desc: the
    # newest paired row leads page 1, the newest unmatched row leads page 2.
    assert page1.rows[0].tumor_id == _pairing_by_tumor(db, "PT2").tumor_id
    # The fixture's unmatched samples are NORMAL, so the row's sample sits
    # on the normal side (the pairing FK normal_id stays null by design).
    assert page2.rows[0].normal is not None and page2.rows[0].tumor is None


def _pairing_by_tumor(db, tumor_primary):
    t = db.query(Sample).filter_by(primary_id=tumor_primary).one()
    return db.query(PairingTable).filter_by(tumor_id=t.id).one()


def test_page_straddles_paired_into_unmatched(db):
    logic = _logic(db, "DMP", "dmp")
    for i in range(3):
        tumor = _sample(db, f"PT{i}", "DMP", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"PN{i}", "DMP", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    for i in range(2):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=i))

    # page_size 4 > 3 paired rows: page 1 must hold 3 paired + 1 unmatched.
    page1 = pairing_report("DMP", 1, 4, db)
    assert len(page1.rows) == 4
    both = [r for r in page1.rows if r.tumor is not None and r.normal is not None]
    one = [r for r in page1.rows if (r.tumor is not None) ^ (r.normal is not None)]
    assert len(both) == 3 and len(one) == 1
    # The paired rows come first, the unmatched row last.
    assert page1.rows[-1] in one


# --- (c) DMP includes unmatched; non-DMP does not ---------------------------

def test_dmp_includes_unmatched_rows(db):
    logic = _logic(db, "DMP", "dmp")
    tumor = _sample(db, "T0", "DMP", TumorOrNormal.TUMOR.value, BASE_DATE)
    normal = _sample(db, "N0", "DMP", TumorOrNormal.NORMAL.value, BASE_DATE)
    _pairing(db, logic, tumor, normal, BASE_DATE)
    for i in range(2):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(minutes=10 + i))

    result = pairing_report("DMP", 1, 50, db)
    assert result.total == 3
    # The 2 unmatched DMP samples appear as normal-side rows (tumor null).
    unmatched = [r for r in result.rows if r.tumor is None and r.normal is not None]
    assert len(unmatched) == 2
    # Unmatched rows carry the logic name and an empty mismatch list.
    for r in unmatched:
        assert r.pairing_logic == "DMP"
        assert r.mismatched_keys == []


def test_non_dmp_excludes_unmatched_rows(db):
    logic = _logic(db, "ARGOS", "argos")
    # Two paired rows under the non-DMP logic (non-DMP samples).
    for i in range(2):
        tumor = _sample(db, f"T{i}", "IMPACT", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"N{i}", "IMPACT", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    # Two DMP samples with no pairing: an unmatched DMP logic would list
    # these, but a non-DMP logic must not.
    for i in range(2):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=i))

    result = pairing_report("ARGOS", 1, 50, db)
    assert result.total == 2
    assert len(result.rows) == 2
    # Every row is a fully paired row; no one-sided rows leaked in.
    assert all(r.tumor is not None and r.normal is not None for r in result.rows)


# --- (d) ordering is stable across repeated calls ---------------------------

def test_ordering_stable_across_repeated_calls(db):
    logic = _logic(db, "DMP", "dmp")
    for i in range(4):
        tumor = _sample(db, f"PT{i}", "DMP", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"PN{i}", "DMP", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    for i in range(3):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=i))

    first = pairing_report("DMP", 1, 50, db)
    second = pairing_report("DMP", 1, 50, db)
    # Same page on repeated calls: identical row order and content.
    assert [_side_ids(r) for r in first.rows] == [_side_ids(r) for r in second.rows]
    assert [r.pairing_logic for r in first.rows] == [r.pairing_logic for r in second.rows]
    assert first.total == second.total == 7


def test_empty_logic_returns_empty_page(db):
    _logic(db, "DMP", "dmp")
    result = pairing_report("DMP", 1, 50, db)
    assert result.rows == []
    assert result.total == 0


# --- misc: unknown logic, page_size clamping --------------------------------

def test_unknown_logic_404(db):
    with pytest.raises(HTTPException) as exc:
        pairing_report("NO-SUCH-LOGIC", 1, 50, db)
    assert exc.value.status_code == 404


def test_page_size_clamped_to_max(db):
    logic = _logic(db, "DMP", "dmp")
    # 250 unmatched DMP samples: requesting page_size 500 must clamp to
    # REPORT_MAX_PAGE_SIZE rows, while total still reports the full count.
    for i in range(250):
        _sample(db, f"U{i:03d}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(minutes=i))

    result = pairing_report("DMP", 1, 500, db)
    assert result.total == 250
    assert len(result.rows) == REPORT_MAX_PAGE_SIZE
