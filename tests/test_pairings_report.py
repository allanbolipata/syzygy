"""Tests for the server-side Pair Report endpoint (GET /pairing/report/).

The endpoint returns one page of the *merged* report list (paired rows,
then — for DMP logics — the unmatched DMP TUMORS rendered as one-sided
rows: the reference pair file is tumor-driven, so unpaired DMP normals
never appear) plus the full-list ``total`` computed in SQL. The frontend
used to build this merge client-side (app.js ``appendUnmatchedDmpRows``).
These tests pin the total, page boundaries, the DMP vs non-DMP split, and
stable ordering.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, PairingLogic, PairingTable, Sample
from routers.pairings import (
    REPORT_DEFAULT_PAGE_SIZE,
    REPORT_MAX_PAGE_SIZE,
    pairing_report,
    export_pair_report,
    _iter_paired_export_rows,
    _iter_unmatched_export_rows,
    _resolve_export_columns,
    _csv_line,
    DMP_EXPORT_COLUMNS,
    PAIR_EXPORT_COLUMNS,
    DMP_DEFAULT_EXPORT_KEYS,
    PAIR_DEFAULT_EXPORT_KEYS,
)
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


# The reference's 15-column pair file, names and order verbatim (spec c;
# bam-mirror-tools/scripts/pair_tumor_with_normal.R write.table).
DMP_REF_PAIR_COLUMNS = [
    "tumor_sample", "patient", "tumor_idx", "tumor_IM_ver", "tumor_bamname",
    "tumor_group", "tumor_matched", "tumor_bam_exists", "tumor_bam_coverage",
    "normal_sample", "normal_bamname", "normal_IM_ver", "pool_id",
    "normal_bam_exists", "normal_bam_coverage",
]


def _dmp_sample(db, primary_id, tumor, created, patient_dmp=None, anon_id=None, meta=None):
    """A DMP sample carrying the reference-format fields the 4/8 value
    tests exercise (patient__dmp, anon_id, additional_metadata)."""
    s = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"CMO_{primary_id}",
        gene_panel="AC1",
        sample_type="DMP",
        tumor_or_normal=tumor,
        patient__dmp=patient_dmp,
        anon_id=anon_id,
        additional_metadata=meta,
        created_date=created,
    )
    db.add(s)
    db.flush()
    return s


def _pairing(db, logic, tumor, normal, created, multi_pair: bool = False):
    p = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        latest=True,
        multi_pair=multi_pair,
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
    # Three DMP TUMORS with no latest pairing -> 3 unmatched rows (4/8:
    # the pair file is tumor-driven).
    for i in range(3):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.TUMOR.value,
                BASE_DATE + timedelta(hours=1, minutes=i))
    # An unpaired DMP NORMAL: not part of the pair file, not counted.
    _sample(db, "UN", "DMP", TumorOrNormal.NORMAL.value,
            BASE_DATE + timedelta(hours=2))

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
    # 1 latest paired row. normal_a's pairing is superseded, but it is an
    # unpaired NORMAL and the pair file is tumor-driven (4/8), so it does
    # not count at all.
    assert result.total == 1


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
    # 4 unmatched DMP TUMORS, newest first: U3 (newest) -> U0 (oldest).
    for i in range(4):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.TUMOR.value,
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
    # The fixture's unmatched samples are TUMORS (4/8: the pair file is
    # tumor-driven), so the row's sample sits on the tumor side (the
    # pairing FK normal_id stays null by design).
    assert page2.rows[0].tumor is not None and page2.rows[0].normal is None


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
    # 2 unmatched DMP TUMORS (4/8: the pair file is tumor-driven).
    for i in range(2):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.TUMOR.value,
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
    """4/8: the DMP unmatched section is tumor rows only — an unpaired DMP
    NORMAL never appears (stakeholder: tumor rows only, as
    bam-mirror-tools does); an unpaired DMP TUMOR appears as a tumor-side
    row (normal null)."""
    logic = _logic(db, "DMP", "dmp")
    tumor = _sample(db, "T0", "DMP", TumorOrNormal.TUMOR.value, BASE_DATE)
    normal = _sample(db, "N0", "DMP", TumorOrNormal.NORMAL.value, BASE_DATE)
    _pairing(db, logic, tumor, normal, BASE_DATE)
    unmatched_tumor = _sample(db, "UT", "DMP", TumorOrNormal.TUMOR.value,
                              BASE_DATE + timedelta(minutes=10))
    unmatched_normal = _sample(db, "UN", "DMP", TumorOrNormal.NORMAL.value,
                               BASE_DATE + timedelta(minutes=11))

    result = pairing_report("DMP", 1, 50, db, q=None)
    assert result.total == 2  # 1 paired + 1 unmatched tumor (normal excluded)

    # The paired row is two-sided; the unmatched row is tumor-side only.
    # (Unmatched rows carry no pairing identity — the sample object holds
    # the id.)
    u_row = next(r for r in result.rows
                 if r.tumor is not None and r.tumor.id == unmatched_tumor.id)
    assert u_row.tumor is not None and u_row.normal is None
    # No normal-side-only row exists (the unpaired normal is absent).
    for r in result.rows:
        assert r.tumor is not None and r.tumor.id != unmatched_normal.id
        assert r.normal is None or r.normal.id != unmatched_normal.id
    # Unmatched rows carry the logic name and an empty mismatch list.
    assert u_row.pairing_logic == "DMP"
    assert u_row.mismatched_keys == []


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
    # 3 unmatched DMP TUMORS (4/8: the pair file is tumor-driven).
    for i in range(3):
        _sample(db, f"U{i}", "DMP", TumorOrNormal.TUMOR.value,
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
    # 250 unmatched DMP TUMORS: requesting page_size 500 must clamp to
    # REPORT_MAX_PAGE_SIZE rows, while total still reports the full count.
    for i in range(250):
        _sample(db, f"U{i:03d}", "DMP", TumorOrNormal.TUMOR.value,
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
    and one unmatched DMP TUMOR (U): the pair file is tumor-driven (4/8),
    so the unmatched row carries its sample on the tumor side. Every row
    has distinct values in all five search fields, so any value unique to
    one row selects exactly that row.

    Row A (oldest):   tumor CMO-ALPHA / normal CMO-BRAVO
    Row B (newest):   tumor CMO-DELTA / normal CMO-CHARLIE
    Unmatched (U):    tumor side only, CMO-UNMATCHED
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
    u = _full_sample(db, "PRI-U", "DMP", TumorOrNormal.TUMOR.value,
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


def test_q_unmatched_dmp_row_is_tumor_side(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # The unmatched row has a NULL normal side (4/8: tumor rows only): a
    # value that exists only on a paired row's normal side must NOT return
    # the unmatched row (a null side never matches).
    result = pairing_report("DMP", 1, 50, db, q="CMO-CHARLIE")
    assert result.total == 1
    assert result.rows[0].tumor is not None and result.rows[0].normal is not None
    # The unmatched row IS found via its own (tumor-side) fields and comes
    # back as a one-sided row (tumor set, normal null).
    result = pairing_report("DMP", 1, 50, db, q="DMP-UNMATCHED")
    assert result.total == 1
    row = result.rows[0]
    assert row.tumor is not None and row.normal is None
    # Unmatched rows carry no pairing identity (T3 contract: the sample id
    # lives on the side object itself).
    assert row.tumor.id == u.id


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
    _full_sample(db, "U", "DMP", TumorOrNormal.TUMOR.value,
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
    # unmatched DMP TUMOR row (tumor set, normal null — 4/8).
    assert all(r.tumor is not None and r.normal is not None for r in p1.rows)
    assert p2.rows[0].tumor is not None and p2.rows[0].normal is None


def test_q_treats_like_wildcards_as_literals(db):
    logic, at, an, bt, bn, u = _search_fixture(db)
    # The client searched with String.prototype.includes: '%' and '_' are
    # ordinary characters, not LIKE wildcards — a query of just one must
    # match nothing (no fixture value contains either character).
    for q in ("%", "_"):
        result = pairing_report("DMP", 1, 50, db, q=q)
        assert result.total == 0, q


# --- (f) server-side export (GET /pairing/export/) -------------------------
#
# The client holds only one page (T4), so the Download button fetches the
# full searched set from the server. The endpoint streams CSV/TSV (one
# StreamingResponse, keyset-batched), with the same q filter as the report
# endpoint (T5) and the report column model as its header.

@pytest.fixture()
def export_stream(db, engine, monkeypatch):
    """Point the export's stream session at the in-memory test engine.

    The endpoint validates with the request session but streams through its
    own session (the request session may be closed before the body finishes);
    redirecting that factory makes the stream see the fixture data.
    """
    monkeypatch.setattr(
        "routers.pairings._open_stream_session", sessionmaker(bind=engine)
    )


def _export_lines(logic, db, q=None, columns=None, fmt="csv"):
    """Call the endpoint and return the non-empty lines of its body.

    The fixture data is flushed, not committed, and the stream session
    shares the single StaticPool connection — committing first makes the
    data visible to the stream and safe against the stream session's
    close-rollback on that shared connection.

    Starlette wraps the sync generator in an async iterator (threadpool),
    so the response must be drained with asyncio.
    """
    db.commit()
    resp = export_pair_report(logic, q=q, columns=columns, fmt=fmt, db=db)

    async def _drain():
        return "".join([chunk async for chunk in resp.body_iterator])

    return [line for line in asyncio.run(_drain()).splitlines() if line]


def test_export_returns_csv_content_type(db, export_stream):
    logic = _logic(db, "DMP", "dmp")
    tumor = _sample(db, "T0", "DMP", TumorOrNormal.TUMOR.value, BASE_DATE)
    normal = _sample(db, "N0", "DMP", TumorOrNormal.NORMAL.value, BASE_DATE)
    _pairing(db, logic, tumor, normal, BASE_DATE)

    resp = export_pair_report("DMP", q=None, columns=None, fmt="csv", db=db)
    assert resp.media_type == "text/csv"
    assert "pairs_DMP.csv" in resp.headers["content-disposition"]

    resp = export_pair_report("DMP", q=None, columns=None, fmt="tsv", db=db)
    assert resp.media_type == "text/tab-separated-values"
    assert "pairs_DMP.tsv" in resp.headers["content-disposition"]

    # An invalid fmt is rejected up front (clean 4xx, not a broken stream).
    with pytest.raises(HTTPException) as exc:
        export_pair_report("DMP", q=None, columns=None, fmt="xlsx", db=db)
    assert exc.value.status_code == 400


def test_export_streams_full_set_not_one_page(db, export_stream):
    logic = _logic(db, "ARGOS", "argos")
    # 55 latest pairings — more than one report page of 50 — proves the
    # export streams the FULL set, not the client's single page.
    for i in range(55):
        tumor = _sample(db, f"T{i:02d}", "IMPACT", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=i))
        normal = _sample(db, f"N{i:02d}", "IMPACT", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=i))
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))

    lines = _export_lines("ARGOS", db)
    assert len(lines) == 1 + 55  # header + every paired row
    assert len(lines) - 1 > REPORT_DEFAULT_PAGE_SIZE
    # Non-DMP logic: paired rows only, no unmatched section.
    assert all(row.split(",")[:2] for row in lines[1:])


def test_export_respects_q_filter(db, export_stream):
    _search_fixture(db)  # 2 latest pairings (A, B) + 1 unmatched DMP tumor (U)

    # 4/8: DMP accepts only the pair file's 15 columns as export keys —
    # tumor_sample/normal_sample are the pair-file names for the sample ids.
    # Unfiltered export: header + all 3 rows.
    lines = _export_lines("DMP", db, columns="tumor_sample,normal_sample")
    assert len(lines) == 1 + 3

    # q narrows the export to the matching rows — same filter as the report
    # endpoint: ALPHA picks row A (the matched pair); UNMATCHED picks the
    # one-sided unmatched tumor row, whose normal side renders as the
    # reference's NA.
    lines = _export_lines("DMP", db, q="ALPHA",
                          columns="tumor_sample,normal_sample")
    assert len(lines) == 1 + 1
    assert lines[1] == "PRI-A,PRI-A-N"

    lines = _export_lines("DMP", db, q="UNMATCHED",
                          columns="tumor_sample,normal_sample")
    assert len(lines) == 1 + 1
    assert lines[1] == "PRI-U,NA"

    # A no-match query streams only the header.
    lines = _export_lines("DMP", db, q="ZZZ-NOMATCH",
                          columns="tumor_sample,normal_sample")
    assert len(lines) == 1


def test_export_dmp_pair_file_is_tumor_rows_only(db, export_stream):
    """4/8: the DMP export is the reference pair file — every DMP tumor
    gets a row (paired or not); an unpaired DMP NORMAL never appears
    (stakeholder: tumor rows only, as bam-mirror-tools does)."""
    logic = _logic(db, "DMP", "dmp")
    for i in range(2):
        tumor = _dmp_sample(db, f"P-000000{i + 1}-T01-IM6",
                            TumorOrNormal.TUMOR.value,
                            BASE_DATE + timedelta(minutes=i),
                            patient_dmp=f"P-000000{i + 1}")
        normal = _dmp_sample(db, f"P-000000{i + 1}-N01-IM6",
                             TumorOrNormal.NORMAL.value,
                             BASE_DATE + timedelta(minutes=i),
                             patient_dmp=f"P-000000{i + 1}")
        _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=i))
    # One unpaired DMP TUMOR (appears) and one unpaired DMP NORMAL (absent).
    _dmp_sample(db, "P-0000099-T01-IM6", TumorOrNormal.TUMOR.value,
                BASE_DATE + timedelta(hours=1), patient_dmp="P-0000099")
    _dmp_sample(db, "P-0000098-N01-IM6", TumorOrNormal.NORMAL.value,
                BASE_DATE + timedelta(hours=1, minutes=1),
                patient_dmp="P-0000098")

    lines = _export_lines("DMP", db, fmt="tsv")
    assert len(lines) == 1 + 3  # header + 2 paired + 1 unmatched tumor
    header = lines[0].split("\t")
    t_idx = header.index("tumor_sample")
    n_idx = header.index("normal_sample")
    nb_idx = header.index("normal_bamname")
    nver_idx = header.index("normal_IM_ver")
    nbe_idx = header.index("normal_bam_exists")
    ncov_idx = header.index("normal_bam_coverage")
    data = [line.split("\t") for line in lines[1:]]
    # Paired rows lead (newest first), both sides present.
    assert [d[t_idx] for d in data[:2]] == ["P-0000002-T01-IM6",
                                            "P-0000001-T01-IM6"]
    for d in data[:2]:
        assert d[n_idx] == d[t_idx].replace("-T01-", "-N01-")
        # No bamname/anon_id/coverage in the fixture -> NA; beagle DB unset
        # -> bam_exists unresolvable -> 0.
        assert d[nb_idx] == "NA" and d[ncov_idx] == "NA" and d[nbe_idx] == "0"
    # The unmatched tumor row: normal side NA except normal_IM_ver, which
    # is backfilled with the tumor's own IM_ver (reference fill rule).
    u = data[2]
    assert u[t_idx] == "P-0000099-T01-IM6"
    assert u[n_idx] == "NA" and u[nb_idx] == "NA"
    assert u[nbe_idx] == "NA" and u[ncov_idx] == "NA"
    assert u[nver_idx] == "IM6"
    # The unpaired DMP normal never appears in the pair file.
    assert all("P-0000098" not in line for line in lines)


def test_export_header_columns_match_report_model(db, export_stream):
    _logic(db, "DMP", "dmp")
    _logic(db, "ARGOS", "argos")

    # DMP logic defaults to the reference's 15-column pair file — names
    # and order verbatim (4/8, spec c). An empty logic still yields just
    # the header.
    lines = _export_lines("DMP", db, fmt="tsv")
    assert len(lines) == 1
    assert lines[0].split("\t") == DMP_REF_PAIR_COLUMNS

    # Non-DMP logic defaults to the pair report's default subset.
    lines = _export_lines("ARGOS", db)
    assert lines[0] == "TUMOR_SAMPLE,NORMAL_SAMPLE,BAIT_SET"

    # An explicit columns param selects exactly those columns, in order.
    lines = _export_lines("ARGOS", db,
                          columns="normal_primary_id,tumor_primary_id")
    assert lines[0] == "NORMAL_SAMPLE,TUMOR_SAMPLE"
    # DMP accepts pair-file columns only, in the requested order (4/8).
    lines = _export_lines("DMP", db, columns="pool_id,tumor_sample")
    assert lines[0] == "pool_id,tumor_sample"
    # ... and a pair-report key is a 400 for a DMP logic.
    with pytest.raises(HTTPException) as exc:
        export_pair_report("DMP", q=None, columns="tumor_primary_id",
                           fmt="csv", db=db)
    assert exc.value.status_code == 400

    # Unknown column keys are rejected up front (clean 400).
    with pytest.raises(HTTPException) as exc:
        export_pair_report("ARGOS", q=None, columns="no_such_column",
                           fmt="csv", db=db)
    assert exc.value.status_code == 400


# --- (g) DMP pair-file values: the 2/8 remap contract (4/8) -----------------


def test_export_dmp_ambiguous_tumor_multiple_rows(db, export_stream):
    """An ambiguous tumor (two latest pairs under the 3/8 no-winner
    fan-out) yields one row per (tumor, normal) pair, exactly as stored."""
    logic = _logic(db, "DMP", "dmp")
    tumor = _dmp_sample(db, "P-0000010-T01-IM6", TumorOrNormal.TUMOR.value,
                        BASE_DATE, patient_dmp="P-0000010")
    n1 = _dmp_sample(db, "P-0000010-N01-IM6", TumorOrNormal.NORMAL.value,
                     BASE_DATE, patient_dmp="P-0000010")
    n2 = _dmp_sample(db, "P-0000010-N02-IM6", TumorOrNormal.NORMAL.value,
                     BASE_DATE + timedelta(minutes=1),
                     patient_dmp="P-0000010")
    _pairing(db, logic, tumor, n1, BASE_DATE, multi_pair=True)
    _pairing(db, logic, tumor, n2, BASE_DATE + timedelta(minutes=1),
             multi_pair=True)

    lines = _export_lines("DMP", db, columns="tumor_sample,normal_sample")
    assert len(lines) == 1 + 2
    rows = [line.split(",") for line in lines[1:]]
    assert all(r[0] == "P-0000010-T01-IM6" for r in rows)
    assert {r[1] for r in rows} == {"P-0000010-N01-IM6",
                                    "P-0000010-N02-IM6"}


def test_export_dmp_values_per_remap_contract(db, export_stream):
    """Cell values follow the 2/8 remap contract: bamname over anon_id,
    explicit pool_id over project_name, patient__dmp, idx/ver parsed from
    the name, coverage as-is, matched from the pairing result when the
    record carries no matched_tag."""
    logic = _logic(db, "DMP", "dmp")
    # Newest pair: explicit bamname + explicit pool_id both win.
    tumor = _dmp_sample(
        db, "P-0000020-T01-IM7", TumorOrNormal.TUMOR.value,
        BASE_DATE + timedelta(minutes=1),
        patient_dmp="P-0000020", anon_id="ANON-T",
        meta={"bamname": "BAMX-T", "pool_id": "POOL-7",
              "project_name": "PROJECT-7", "coverage": 687})
    normal = _dmp_sample(
        db, "P-0000020-N01-IM7", TumorOrNormal.NORMAL.value,
        BASE_DATE + timedelta(minutes=1),
        patient_dmp="P-0000020", anon_id="ANON-N",
        meta={"coverage": 452})
    _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=1))
    # Older pair: no bamname (and no anon_id -> NA), no pool_id (the
    # project_name fallback applies), no coverage.
    tumor2 = _dmp_sample(
        db, "P-0000021-T01-IM6", TumorOrNormal.TUMOR.value,
        BASE_DATE, patient_dmp="P-0000021",
        meta={"project_name": "PROJECT-9"})
    normal2 = _dmp_sample(db, "P-0000021-N01-IM6",
                          TumorOrNormal.NORMAL.value,
                          BASE_DATE, patient_dmp="P-0000021")
    _pairing(db, logic, tumor2, normal2, BASE_DATE)

    lines = _export_lines("DMP", db, fmt="tsv")
    header = lines[0].split("\t")
    by = {h: i for i, h in enumerate(header)}
    d1 = lines[1].split("\t")  # newest first: the P-0000020 pair
    d2 = lines[2].split("\t")
    assert d1[by["tumor_sample"]] == "P-0000020-T01-IM7"
    assert d1[by["patient"]] == "P-0000020"
    assert d1[by["tumor_idx"]] == "T01" and d1[by["tumor_IM_ver"]] == "IM7"
    assert d1[by["tumor_bamname"]] == "BAMX-T"    # bamname beats anon_id
    assert d1[by["normal_bamname"]] == "ANON-N"   # anon_id fallback
    assert d1[by["tumor_group"]] == "NA"          # no source in the format
    assert d1[by["tumor_matched"]] == "Matched"   # pairing-derived fallback
    assert d1[by["tumor_bam_coverage"]] == "687"
    assert d1[by["normal_bam_coverage"]] == "452"
    assert d1[by["pool_id"]] == "POOL-7"          # explicit pool_id wins
    assert d1[by["normal_IM_ver"]] == "IM7"
    # beagle DB unset -> bam_exists unresolvable -> 0
    assert d1[by["tumor_bam_exists"]] == "0" and d1[by["normal_bam_exists"]] == "0"
    # Fallback pair: no bamname/anon_id -> NA, project_name -> pool_id.
    assert d2[by["tumor_bamname"]] == "NA"
    assert d2[by["pool_id"]] == "PROJECT-9"
    assert d2[by["tumor_bam_coverage"]] == "NA"


def test_export_dmp_normal_im_ver_backfills_tumor_ver_when_missing(
        db, export_stream):
    """Reference fill rule (pair_tumor_with_normal.R): normal_IM_ver is
    the normal's ver when it has one, else the tumor's own IM_ver —
    applied to paired rows as well as unmatched ones."""
    logic = _logic(db, "DMP", "dmp")
    tumor = _dmp_sample(db, "P-0000060-T01-IM6", TumorOrNormal.TUMOR.value,
                        BASE_DATE + timedelta(minutes=1),
                        patient_dmp="P-0000060")
    # Normal whose name carries no version segment (ver parse -> NA).
    normal = _dmp_sample(db, "P-0000060-N01", TumorOrNormal.NORMAL.value,
                         BASE_DATE + timedelta(minutes=1),
                         patient_dmp="P-0000060")
    _pairing(db, logic, tumor, normal, BASE_DATE + timedelta(minutes=1))

    lines = _export_lines("DMP", db, fmt="tsv")
    header = lines[0].split("\t")
    by = {h: i for i, h in enumerate(header)}
    data = lines[1].split("\t")
    assert data[by["tumor_IM_ver"]] == "IM6"
    assert data[by["normal_IM_ver"]] == "IM6"  # backfilled, not NA


def test_export_dmp_matched_flag_is_data_not_computed(db, export_stream):
    """tumor_matched carries the record's matched_tag verbatim (spec g):
    a tumor unmatched in syzygy whose DMP record says Matched still
    exports Matched — the flag is data, not a pairing-derived boolean."""
    _logic(db, "DMP", "dmp")
    _dmp_sample(db, "P-0000030-T01-IM6", TumorOrNormal.TUMOR.value,
                BASE_DATE, patient_dmp="P-0000030",
                meta={"matched_tag": "Matched", "project_name": "P1"})

    lines = _export_lines("DMP", db, fmt="tsv")
    header = lines[0].split("\t")
    data = lines[1].split("\t")
    by = {h: i for i, h in enumerate(header)}
    assert data[by["tumor_sample"]] == "P-0000030-T01-IM6"
    assert data[by["tumor_matched"]] == "Matched"  # from the record
    assert data[by["normal_IM_ver"]] == "IM6"      # backfill rule
    assert data[by["normal_bam_exists"]] == "NA"   # unmatched: NA, not 0


class _StubBeagleCursor:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubBeagleConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return _StubBeagleCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubBeagleEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _StubBeagleConn(self._rows)


def test_export_dmp_bam_exists_from_beagle_record(db, export_stream, monkeypatch):
    """bam_exists derives from the sample's beagle file record (spec f):
    non-empty path -> 1, empty/absent -> 0, resolved per export batch."""
    import routers.pairings as pairings
    monkeypatch.setattr(
        pairings.settings, "beagle_database_url",
        "postgresql+psycopg2://beagle:beagle@localhost/beagle",
    )
    monkeypatch.setattr(
        pairings, "_beagle_engine_cache",
        _StubBeagleEngine(
            [("P-0000040-T01-IM6", True), ("P-0000040-N01-IM6", False)]
        ),
    )
    logic = _logic(db, "DMP", "dmp")
    tumor = _dmp_sample(db, "P-0000040-T01-IM6", TumorOrNormal.TUMOR.value,
                        BASE_DATE, patient_dmp="P-0000040")
    normal = _dmp_sample(db, "P-0000040-N01-IM6", TumorOrNormal.NORMAL.value,
                         BASE_DATE, patient_dmp="P-0000040")
    _pairing(db, logic, tumor, normal, BASE_DATE)

    lines = _export_lines("DMP", db, fmt="tsv")
    header = lines[0].split("\t")
    data = lines[1].split("\t")
    by = {h: i for i, h in enumerate(header)}
    assert data[by["tumor_bam_exists"]] == "1"  # non-empty path -> 1
    assert data[by["normal_bam_exists"]] == "0"  # empty path -> 0


def test_bam_exists_map_empty_when_beagle_unset(monkeypatch):
    import routers.pairings as pairings
    monkeypatch.setattr(pairings.settings, "beagle_database_url", None)
    monkeypatch.setattr(pairings, "_beagle_engine_cache", None)
    assert pairings._beagle_bam_exists_map({"P-1-T01-IM6"}) == {}
