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

    result = pairing_report("DMP", 1, 50, db, q=None)
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

    result = pairing_report("DMP", 1, 50, db, q=None)
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

    assert pairing_report("DMP", 1, 30, db, q=None).total == 7

    page1 = pairing_report("DMP", 1, 3, db, q=None)
    page2 = pairing_report("DMP", 2, 3, db, q=None)
    page3 = pairing_report("DMP", 3, 3, db, q=None)

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
    page1 = pairing_report("DMP", 1, 4, db, q=None)
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

    result = pairing_report("DMP", 1, 50, db, q=None)
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

    result = pairing_report("ARGOS", 1, 50, db, q=None)
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

    first = pairing_report("DMP", 1, 50, db, q=None)
    second = pairing_report("DMP", 1, 50, db, q=None)
    # Same page on repeated calls: identical row order and content.
    assert [_side_ids(r) for r in first.rows] == [_side_ids(r) for r in second.rows]
    assert [r.pairing_logic for r in first.rows] == [r.pairing_logic for r in second.rows]
    assert first.total == second.total == 7


def test_empty_logic_returns_empty_page(db):
    _logic(db, "DMP", "dmp")
    result = pairing_report("DMP", 1, 50, db, q=None)
    assert result.rows == []
    assert result.total == 0


# --- misc: unknown logic, page_size clamping --------------------------------

def test_unknown_logic_404(db):
    with pytest.raises(HTTPException) as exc:
        pairing_report("NO-SUCH-LOGIC", 1, 50, db, q=None)
    assert exc.value.status_code == 404


def test_page_size_clamped_to_max(db):
    logic = _logic(db, "DMP", "dmp")
    # 250 unmatched DMP samples: requesting page_size 500 must clamp to
    # REPORT_MAX_PAGE_SIZE rows, while total still reports the full count.
    for i in range(250):
        _sample(db, f"U{i:03d}", "DMP", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(minutes=i))

    result = pairing_report("DMP", 1, 500, db, q=None)
    assert result.total == 250
    assert len(result.rows) == REPORT_MAX_PAGE_SIZE


# --- (e) server-side free-text search (q) ----------------------------------
#
# GET /pairing/report/?q= must replicate the old client-side
# pairMatchesQuery semantics: q (trimmed, lowercased, case-insensitive
# substring — the client compared with String.prototype.includes, so '%'
# and '_' are ordinary characters) matches a row when ANY of the five
# report search fields (patient__cmo, patient__dmp, cmo_sample_name,
# primary_id, bait_set) contains it on EITHER the tumor or normal side;
# a NULL side never matches. total and pagination reflect the filtered
# set, not the full list.

def _full_sample(db, primary_id, sample_type, tumor, created,
                 patient_cmo, patient_dmp, cmo_name, bait):
    s = Sample(
        primary_id=primary_id,
        cmo_sample_name=cmo_name,
        gene_panel="AC1",
        sample_type=sample_type,
        tumor_or_normal=tumor,
        patient__cmo=patient_cmo,
        patient__dmp=patient_dmp,
        bait_set=bait,
        created_date=created,
    )
    db.add(s)
    db.flush()
    return s


def _search_fixture(db):
    """A DMP logic with two latest pairings (rows A and B, newest B first)
    and one unmatched DMP sample (U) on the NORMAL side. Every row has
    distinct values in all five search fields, so any value unique to one
    row selects exactly that row.

    Row A (oldest):   tumor CMO-ALPHA / normal CMO-BRAVO
    Row B (newest):   tumor CMO-DELTA / normal CMO-CHARLIE
    Unmatched (U):    normal side only, CMO-UNMATCHED
    """
    logic = _logic(db, "DMP", "dmp")
    at = _full_sample(db, "PRI-A", "DMP", TumorOrNormal.TUMOR.value,
                      BASE_DATE, "CMO-ALPHA", "DMP-ALPHA", "CSA-ALPHA", "BSET-9")
    an = _full_sample(db, "PRI-A-N", "DMP", TumorOrNormal.NORMAL.value,
                      BASE_DATE, "CMO-BRAVO", "DMP-BRAVO", "CSA-BRAVO", "BSET-3")
    _pairing(db, logic, at, an, BASE_DATE)
    bt = _full_sample(db, "PRI-B", "DMP", TumorOrNormal.TUMOR.value,
                      BASE_DATE + timedelta(minutes=1),
                      "CMO-DELTA", "DMP-DELTA", "CSB-DELTA", "BSET-1")
    bn = _full_sample(db, "PRI-B-N", "DMP", TumorOrNormal.NORMAL.value,
                      BASE_DATE + timedelta(minutes=1),
                      "CMO-CHARLIE", "DMP-CHARLIE", "CSB-CHARLIE", "BSET-7")
    _pairing(db, logic, bt, bn, BASE_DATE + timedelta(minutes=1))
    u = _full_sample(db, "PRI-U", "DMP", TumorOrNormal.NORMAL.value,
                     BASE_DATE + timedelta(hours=1),
                     "CMO-UNMATCHED", "DMP-UNMATCHED", "CSU-UNMATCHED", "BSET-4")
    return logic, at, an, bt, bn, u


def _side_pair(row):
    return {row.tumor_id, row.normal_id} if row.tumor and row.normal else None


def test_q_matches_each_search_field(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # A value unique to row A in each of the five fields selects row A
    # alone (total reflects the filtered set, not the full list of 3).
    cases = {
        "patient__cmo": "CMO-ALPHA",
        "patient__dmp": "DMP-ALPHA",
        "cmo_sample_name": "CSA-ALPHA",
        "primary_id": "PRI-A",
        "bait_set": "BSET-9",
    }
    for field, value in cases.items():
        result = pairing_report("DMP", 1, 50, db, q=value)
        assert result.total == 1, field
        assert len(result.rows) == 1, field
        row = result.rows[0]
        assert row.tumor is not None and row.normal is not None
        assert _side_pair(row) == {at.id, an.id}, field


def test_q_matches_either_side(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # A value present only on row B's tumor side selects row B (tumor
    # side match), and one only on row B's normal side also selects row B
    # (normal side match) — a row matches when ANY side matches.
    for q in ("CMO-DELTA", "CMO-CHARLIE"):
        result = pairing_report("DMP", 1, 50, db, q=q)
        assert result.total == 1, q
        assert _side_pair(result.rows[0]) == {bt.id, bn.id}, q


def test_q_is_case_insensitive_and_trimmed(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # The client compared in trimmed, lowercased form; the server must
    # match case-insensitively regardless of the query's casing or padding.
    for q in ("cmo-alpha", "Cmo-BrAvO", "Dmp-Alpha", "  cmo-alpha  "):
        result = pairing_report("DMP", 1, 50, db, q=q)
        assert result.total == 1, q
        assert _side_pair(result.rows[0]) == {at.id, an.id}, q


def test_q_no_match_returns_empty(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    result = pairing_report("DMP", 1, 50, db, q="ZZZ-NOMATCH")
    assert result.total == 0
    assert result.rows == []


def test_q_unmatched_dmp_matches_via_normal_side_only(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # The unmatched row has a NULL tumor side: a value that exists only on
    # a paired row's tumor side must NOT return the unmatched row (a null
    # side never matches, same as the client skipping null fields).
    result = pairing_report("DMP", 1, 50, db, q="CMO-DELTA")
    assert result.total == 1
    assert result.rows[0].tumor is not None and result.rows[0].normal is not None
    # The unmatched row IS found via its own (normal-side) fields and comes
    # back as a one-sided row (tumor null, sample on the normal side).
    result = pairing_report("DMP", 1, 50, db, q="DMP-UNMATCHED")
    assert result.total == 1
    row = result.rows[0]
    assert row.tumor is None and row.normal is not None
    # Unmatched rows carry no pairing identity (T3 contract: the sample id
    # lives on the side object itself).
    assert row.normal.id == u.id


def test_q_paginates_over_filtered_set(db):
    logic = _logic(db, "DMP", "dmp")
    # 3 latest pairings: only the SHARED-* rows carry the marker, plus one
    # unmatched DMP sample that also carries it.
    for i, marker in enumerate(["SHARED-1", "UNIQUE-0", "SHARED-2"]):
        tumor = _full_sample(db, f"T{i}", "DMP", TumorOrNormal.TUMOR.value,
                             BASE_DATE + timedelta(minutes=i),
                             f"CMO-{marker}", None, f"CS-{i}", None)
        normal = _full_sample(db, f"N{i}", "DMP", TumorOrNormal.NORMAL.value,
                              BASE_DATE + timedelta(minutes=i),
                              None, None, f"CN-{i}", None)
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    _full_sample(db, "U", "DMP", TumorOrNormal.NORMAL.value,
                 BASE_DATE + timedelta(hours=1),
                 "CMO-SHARED-3", None, "CU", None)

    # Unfiltered: 3 paired + 1 unmatched = 4.
    assert pairing_report("DMP", 1, 50, db, q=None).total == 4
    # Filtered: the SHARED query narrows to 2 paired rows + 1 unmatched = 3,
    # and total reports the filtered count.
    result = pairing_report("DMP", 1, 50, db, q="SHARED")
    assert result.total == 3
    assert len(result.rows) == 3

    # Pagination runs over the filtered set: page_size 2 -> pages of 2 and 1,
    # page 3 is empty; the pages are disjoint.
    p1 = pairing_report("DMP", 1, 2, db, q="SHARED")
    p2 = pairing_report("DMP", 2, 2, db, q="SHARED")
    p3 = pairing_report("DMP", 3, 2, db, q="SHARED")
    assert len(p1.rows) == 2 and len(p2.rows) == 1 and p3.rows == []
    keys = [
        (r.tumor.id if r.tumor else None, r.normal.id if r.normal else None)
        for r in p1.rows + p2.rows
    ]
    assert len(keys) == 3 and len(set(keys)) == 3
    # Merged order is preserved: paired rows (newest first) lead, then the
    # unmatched row.
    assert all(r.tumor is not None and r.normal is not None for r in p1.rows)
    assert p2.rows[0].tumor is None and p2.rows[0].normal is not None


def test_q_treats_like_wildcards_as_literals(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # The client searched with String.prototype.includes: '%' and '_' are
    # ordinary characters, not LIKE wildcards — a query of just one must
    # match nothing (no fixture value contains either character).
    for q in ("%", "_"):
        result = pairing_report("DMP", 1, 50, db, q=q)
        assert result.total == 0, q
