"""Tests for the DMP report summary counts + 5 QC warnings (5/8).

The DMP Pair Report response carries the reference's summary block and
warning files (pair_tumor_with_normal.R; docs/dmp_reference_alignment.md
sections d/e) as ``summary``/``warnings``: computed over the FULL 15-column
pair frame (latest paired rows, then unmatched DMP tumor rows — the same
cells the export renders), independent of the requested page and of the
``q`` search (the reference computes them over the whole pair file).

Each test pins one formula with a fixture that triggers it plus a clean
fixture that must not, so a regression in any predicate (row set, NA
handling, threshold) is caught.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import routers.pairings as pairings
from models import Base, PairingLogic, PairingTable, Sample
from routers.pairings import pairing_report
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


def _logic(db, name="DMP", strategy_name="dmp"):
    logic = PairingLogic(name=name, strategy_name=strategy_name, active=True)
    db.add(logic)
    db.flush()
    return logic


def _dmp_sample(
    db,
    primary_id,
    tumor,
    created,
    patient_dmp=None,
    anon_id=None,
    meta=None,
):
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


def _pairing(db, logic, tumor, normal, created):
    p = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=logic.id,
        latest=True,
        multi_pair=False,
        trigger_type="automatic",
        created_date=created,
    )
    db.add(p)
    db.flush()
    return p


def _report(db, q=None, page=1, page_size=50):
    return pairing_report("DMP", page, page_size, db, q=q)


# ---------------------------------------------------------------------------
# beagle stub — same pattern as tests/test_pairings_report.py
# ---------------------------------------------------------------------------


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


def _stub_beagle(monkeypatch, rows):
    """rows: (sample_name, has_path) pairs — every beagle lookup in the
    test returns exactly these records; names absent resolve to
    bam_exists "0" (unresolvable -> 0, the reference's semantics)."""
    monkeypatch.setattr(
        pairings.settings,
        "beagle_database_url",
        "postgresql+psycopg2://beagle:beagle@localhost/beagle",
    )
    monkeypatch.setattr(pairings, "_beagle_engine_cache", _StubBeagleEngine(rows))


def _present(*names):
    """Beagle rows: every name has a file with a path (bam_exists 1)."""
    return [(n, True) for n in names]


# ---------------------------------------------------------------------------
# summary counts
# ---------------------------------------------------------------------------


def test_summary_counts_follow_reference_formulas(db):
    """patients = distinct patient among matched rows; tn_pairs = matched
    row count; unmatched_tumors = rows whose tumor_matched != "Matched"."""
    logic = _logic(db)
    # patient A: two matched rows (one tumor fanned out to two normals)
    a_t1 = _dmp_sample(db, "P-1000001-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000001", "ANA-T1")
    a_t2 = _dmp_sample(db, "P-1000001-T02-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000001", "ANA-T2")
    a_n1 = _dmp_sample(db, "P-1000001-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000001", "ANA-N1")
    _pairing(db, logic, a_t1, a_n1, BASE_DATE + timedelta(seconds=10))
    _pairing(db, logic, a_t2, a_n1, BASE_DATE + timedelta(seconds=11))
    # patient B: one matched row
    b_t1 = _dmp_sample(db, "P-1000002-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000002", "ANB-T1")
    b_n1 = _dmp_sample(db, "P-1000002-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000002", "ANB-N1")
    _pairing(db, logic, b_t1, b_n1, BASE_DATE + timedelta(seconds=12))
    # two unmatched tumors (unpaired, no matched_tag -> pairing-derived
    # "Unmatched")
    _dmp_sample(db, "P-1000003-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000003", "ANC-T1")
    _dmp_sample(db, "P-1000004-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=2), "P-1000004", "AND-T1")

    r = _report(db)
    assert r.summary is not None
    assert r.summary.patients == 2  # P-1000001 counted once despite 2 rows
    assert r.summary.tn_pairs == 3
    assert r.summary.unmatched_tumors == 2
    assert r.total == 5


def test_summary_uses_final_matched_flag_including_explicit_tag(db):
    """The reference counts consume tumor_matched as-is: an explicit
    matched_tag on the record wins over the pairing-derived fallback, so a
    tag-Matched unpaired tumor counts as a matched row and a tag-Unmatched
    paired tumor counts as unmatched (spec g, 5/8 note)."""
    logic = _logic(db)
    m1 = _dmp_sample(
        db, "P-1000011-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000011", "ANM-T1",
        meta={"matched_tag": "Matched"},
    )
    u1 = _dmp_sample(
        db, "P-1000012-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000012", "ANU-T1",
        meta={"matched_tag": "Unmatched"},
    )
    u_n = _dmp_sample(db, "P-1000012-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000012", "ANU-N1")
    _pairing(db, logic, u1, u_n, BASE_DATE + timedelta(seconds=5))

    r = _report(db)
    assert r.summary.patients == 1  # only the tag-Matched row's patient
    assert r.summary.tn_pairs == 1
    assert r.summary.unmatched_tumors == 1


def test_summary_independent_of_page_and_q(db):
    """The reference computes the summary over the whole pair file —
    paging and searching must not change it (q filters rows only)."""
    logic = _logic(db)
    t1 = _dmp_sample(db, "P-1000021-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000021", "ANQ-T1")
    n1 = _dmp_sample(db, "P-1000021-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000021", "ANQ-N1")
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))
    _dmp_sample(db, "P-1000022-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000022", "ANQ2-T1")

    base = _report(db)
    for r in (
        _report(db, q="P-1000021"),  # matches the pair only
        _report(db, page=2, page_size=1),
    ):
        assert r.summary == base.summary
        assert r.warnings == base.warnings
    assert base.summary.tn_pairs == 1
    assert base.summary.unmatched_tumors == 1


# ---------------------------------------------------------------------------
# warning 1: matched tumors without a normal
# ---------------------------------------------------------------------------


def test_matched_tumors_without_normal(db):
    """Rows with tumor_matched == "Matched" and NA normal_bamname — the
    reference's core QC case. An unpaired tumor whose pairing-derived flag
    is "Unmatched" does NOT count (only the DMP-program flag does)."""
    logic = _logic(db)
    m1 = _dmp_sample(
        db, "P-1000031-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000031", "ANM-T1",
        meta={"matched_tag": "Matched"},
    )
    c1 = _dmp_sample(db, "P-1000032-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000032", "ANC-T1")
    c_n = _dmp_sample(db, "P-1000032-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000032", "ANC-N1")
    _pairing(db, logic, c1, c_n, BASE_DATE + timedelta(seconds=5))
    # unpaired, no tag -> "Unmatched": not in this bucket
    _dmp_sample(db, "P-1000033-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000033", "ANX-T1")

    r = _report(db)
    assert r.warnings.matched_tumors_without_normal == [m1.primary_id]


def test_matched_tumor_with_normal_but_no_bamname_is_listed(db):
    """The reference's predicate is is.na(normal_bamname) — a paired row
    whose NORMAL side has no bamname/anon_id still has no findable normal
    BAM and is listed."""
    logic = _logic(db)
    t1 = _dmp_sample(db, "P-1000034-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000034", "ANT-T1")
    n1 = _dmp_sample(db, "P-1000034-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000034")  # no anon_id
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))

    r = _report(db)
    assert r.warnings.matched_tumors_without_normal == [t1.primary_id]


# ---------------------------------------------------------------------------
# warning 2: tumor samples with multiple ambiguous normals
# ---------------------------------------------------------------------------


def test_ambiguous_normals_count_matched_but_list_all_rows(db):
    """Ambiguity = a tumor_bamname appearing on >1 pair row (the same
    tumor sample name on several rows — e.g. duplicated DMP tumor
    records; pairing rows are unique per tumor record, so this is across
    records, not fan-out). Count: tumor_bamname groups of >1 row among
    MATCHED rows. List: ALL rows (matched or not) of an ambiguous
    bamname — the reference writes the file without the matched filter."""
    logic = _logic(db)
    # two tumor records sharing bamname ANA-T1, both paired + matched
    a1 = _dmp_sample(db, "P-1000041-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000041", "ANA-T1")
    a1n = _dmp_sample(db, "P-1000041-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000041", "ANA-N1")
    _pairing(db, logic, a1, a1n, BASE_DATE + timedelta(seconds=10))
    a2 = _dmp_sample(db, "P-1000042-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000042", "ANA-T1")
    a2n = _dmp_sample(db, "P-1000042-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=1), "P-1000042", "ANA-N2")
    _pairing(db, logic, a2, a2n, BASE_DATE + timedelta(seconds=11))
    # an UNMATCHED tumor record sharing the same bamname: its row is in
    # the reference's file too
    a3 = _dmp_sample(db, "P-1000043-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000043", "ANA-T1")
    # control: two tag-Unmatched rows sharing a bamname — matched count
    # for that bamname is 0, so it must not be flagged
    c1 = _dmp_sample(
        db, "P-1000044-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=2), "P-1000044", "ANC-T1",
        meta={"matched_tag": "Unmatched"},
    )
    c1n = _dmp_sample(db, "P-1000044-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000044", "ANC-N1")
    _pairing(db, logic, c1, c1n, BASE_DATE + timedelta(seconds=20))
    c2 = _dmp_sample(
        db, "P-1000045-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=3), "P-1000045", "ANC-T1",
        meta={"matched_tag": "Unmatched"},
    )
    c2n = _dmp_sample(db, "P-1000045-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000045", "ANC-N2")
    _pairing(db, logic, c2, c2n, BASE_DATE + timedelta(seconds=21))

    r = _report(db)
    # frame (paired created desc): c2 s21, c1 s20, a2 s11, a1 s10; then
    # unmatched a3. Only ANA-T1 is ambiguous (2 matched rows); ANC-T1 has
    # 0 matched rows. Listed rows of ANA-T1 in frame order: a2, a1, a3.
    assert r.warnings.tumor_samples_with_multiple_ambiguous_normals == [
        a2.primary_id,
        a1.primary_id,
        a3.primary_id,
    ]


def test_single_bamname_row_not_ambiguous(db):
    """One row per tumor_bamname (even a shared NORMAL) is not
    ambiguous — the grouping is on the tumor side only."""
    logic = _logic(db)
    # two tumors sharing one normal: two bamnames, one row each
    t1 = _dmp_sample(db, "P-1000044-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000044", "ANT-T1")
    t2 = _dmp_sample(db, "P-1000045-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000045", "ANT-T2")
    n1 = _dmp_sample(db, "P-1000044-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000044", "ANT-N1")
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=10))
    _pairing(db, logic, t2, n1, BASE_DATE + timedelta(seconds=11))

    r = _report(db)
    assert r.warnings.tumor_samples_with_multiple_ambiguous_normals == []


# ---------------------------------------------------------------------------
# warning 3: samples with bams missing
# ---------------------------------------------------------------------------


def test_samples_with_bams_missing_lists_both_sides(db, monkeypatch):
    """Rows with either side's bam_exists == "0"; both sides' samples are
    listed (a NA normal side contributes nothing)."""
    logic = _logic(db)
    a_t = _dmp_sample(db, "P-1000051-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000051", "ANA-T1")
    a_n = _dmp_sample(db, "P-1000051-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000051", "ANA-N1")
    _pairing(db, logic, a_t, a_n, BASE_DATE + timedelta(seconds=10))
    b_t = _dmp_sample(db, "P-1000052-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000052", "ANB-T1")
    b_n = _dmp_sample(db, "P-1000052-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=1), "P-1000052", "ANB-N1")
    _pairing(db, logic, b_t, b_n, BASE_DATE + timedelta(seconds=11))
    c_t = _dmp_sample(db, "P-1000053-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000053", "ANC-T1")
    d_t = _dmp_sample(db, "P-1000054-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=2), "P-1000054", "AND-T1")
    d_n = _dmp_sample(db, "P-1000054-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(hours=2), "P-1000054", "AND-N1")
    _pairing(db, logic, d_t, d_n, BASE_DATE + timedelta(seconds=12))
    # beagle: A's normal has an empty path; B's tumor has none at all;
    # D's sides are both present (clean).
    _stub_beagle(monkeypatch, [
        (a_t.primary_id, True),
        (a_n.primary_id, False),
        (b_n.primary_id, True),
        (d_t.primary_id, True),
        (d_n.primary_id, True),
    ])

    r = _report(db)
    assert set(r.warnings.samples_with_bams_missing) == {
        a_t.primary_id, a_n.primary_id,   # normal side missing
        b_t.primary_id, b_n.primary_id,   # tumor side missing — the
                                          # reference lists BOTH sides of
                                          # every flagged row
        c_t.primary_id,                   # unmatched, no beagle record
    }
    # order: frame order, first-seen (paired rows before unmatched;
    # paired created desc: d s12, b s11, a s10 — d is clean)
    assert r.warnings.samples_with_bams_missing == [
        b_t.primary_id,
        b_n.primary_id,
        a_t.primary_id,
        a_n.primary_id,
        c_t.primary_id,
    ]


def test_bams_present_clean(db, monkeypatch):
    """Every side with a beagle path is clean: the bucket stays empty."""
    logic = _logic(db)
    t1 = _dmp_sample(db, "P-1000055-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000055", "ANT-T1")
    n1 = _dmp_sample(db, "P-1000055-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000055", "ANT-N1")
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))
    _stub_beagle(monkeypatch, _present(t1.primary_id, n1.primary_id))

    r = _report(db)
    assert r.warnings.samples_with_bams_missing == []


# ---------------------------------------------------------------------------
# warning 4: low-coverage tumors (numeric, < 100 strict)
# ---------------------------------------------------------------------------


def test_low_coverage_tumors(db, monkeypatch):
    """Rows with a non-NA tumor_bamname whose coverage is NA or < 100
    (numeric). Coverage 100 is NOT low (strict <); a row with NA bamname
    is not evaluated at all."""
    logic = _logic(db)
    rows = [
        # (tumor id, tumor coverage meta, expect listed?)
        ("P-1000061-T01-IM6", {"coverage": 45}, True),      # < 100
        ("P-1000062-T01-IM6", None, True),                  # NA coverage
        ("P-1000063-T01-IM6", {"coverage": 100}, False),    # == 100: not low
        ("P-1000064-T01-IM6", {"coverage": 150}, False),    # > 100
        ("P-1000065-T01-IM6", None, False),                 # NA bamname -> skipped
    ]
    for i, (pid, meta, _expected) in enumerate(rows):
        patient = pid.split("-")[0]
        t = _dmp_sample(
            db, pid, TumorOrNormal.TUMOR,
            BASE_DATE + timedelta(seconds=i), patient,
            anon_id=None if pid == "P-1000065-T01-IM6" else f"AN-{pid[-1]}-T",
            meta=meta,
        )
        n = _dmp_sample(
            db, pid.replace("-T01-", "-N01-"), TumorOrNormal.NORMAL,
            BASE_DATE + timedelta(seconds=i), patient, f"AN-{pid[-1]}-N",
            meta={"coverage": 300},
        )
        _pairing(db, logic, t, n, BASE_DATE + timedelta(seconds=10 + i))
    _stub_beagle(monkeypatch, [])  # bam_exists irrelevant to this warning

    r = _report(db)
    # frame order is created_date desc: 1000062 (s11) before 1000061 (s10)
    assert r.warnings.low_coverage_tumors == [
        "P-1000062-T01-IM6",
        "P-1000061-T01-IM6",
    ]


# ---------------------------------------------------------------------------
# warning 5: low-coverage normals (reference's character-typed < 50)
# ---------------------------------------------------------------------------


def test_low_coverage_normals_mirrors_reference_character_comparison(db, monkeypatch):
    """The reference's normal-side coverage column is character-typed
    (spec e quirk), so < 50 is lexicographic on the rendered cell:
    "45" < "50" flags, "50" does not, NA flags, and the quirks —
    "100" < "50" TRUE (flags) and "9" < "50" FALSE (does not flag) — are
    mirrored exactly. The 7/8 conformance harness checks these against the
    reference's own output."""
    logic = _logic(db)
    # (normal coverage meta, expect listed?, why)
    cases = [
        ({"coverage": 45}, True, '"45" < "50"'),
        ({"coverage": 50}, False, '"50" < "50" is false (strict)'),
        (None, True, 'NA coverage with a bamname'),
        ({"coverage": 100}, True, 'quirk: "100" < "50" lexicographically'),
        ({"coverage": 588}, False, '"588" > "50"'),
        ({"coverage": 9}, False, 'quirk: "9" > "50" lexicographically'),
    ]
    for i, (meta, _expected, _why) in enumerate(cases):
        patient = f"P-100007{i + 1}"
        t = _dmp_sample(
            db, f"{patient}-T01-IM6", TumorOrNormal.TUMOR,
            BASE_DATE + timedelta(seconds=i), patient, f"AN{i}T",
            meta={"coverage": 300},
        )
        n = _dmp_sample(
            db, f"{patient}-N01-IM6", TumorOrNormal.NORMAL,
            BASE_DATE + timedelta(seconds=i), patient, f"AN{i}N",
            meta=meta,
        )
        _pairing(db, logic, t, n, BASE_DATE + timedelta(seconds=10 + i))
    # an unmatched row has no normal side at all: never listed, even with
    # no bamname/coverage.
    _dmp_sample(db, "P-1000077-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000077", "AN7T")
    _stub_beagle(monkeypatch, [])  # bam_exists irrelevant to this warning

    r = _report(db)
    # frame order is created_date desc: 1000074 (s13), 1000073 (s12),
    # 1000071 (s10)
    assert r.warnings.low_coverage_normals == [
        "P-1000074-N01-IM6",  # 100 (lexicographic quirk)
        "P-1000073-N01-IM6",  # NA
        "P-1000071-N01-IM6",  # 45
    ]


def test_low_coverage_warnings_flag_rows_without_bamname_when_coverage_present(db, monkeypatch):
    """NA-propagation subtlety (the script wins): the reference's
    predicate is (!is.na(bamname) & is.na(coverage)) | (coverage <
    threshold) — the second disjunct does NOT require a non-NA bamname.
    So a row with no bamname but a present, low coverage IS flagged; a
    row that is NA on both sides evaluates NA and is dropped."""
    logic = _logic(db)
    # tumor: no anon_id (NA bamname) but coverage 60 < 100 -> flagged
    t1 = _dmp_sample(db, "P-1000096-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000096", meta={"coverage": 60})
    n1 = _dmp_sample(db, "P-1000096-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000096", "ANN-N1", meta={"coverage": 588})
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))
    # normal: exists but no anon_id (NA bamname), coverage 45 -> "45" <
    # "50" -> flagged; its tumor has a bamname but high coverage (clean)
    t2 = _dmp_sample(db, "P-1000097-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000097", "ANT2-T", meta={"coverage": 300})
    n2 = _dmp_sample(db, "P-1000097-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=1), "P-1000097", meta={"coverage": 45})
    _pairing(db, logic, t2, n2, BASE_DATE + timedelta(seconds=6))
    # control: NA on both sides (no bamname, no coverage) -> dropped
    t3 = _dmp_sample(db, "P-1000098-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=2), "P-1000098")
    n3 = _dmp_sample(db, "P-1000098-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=2), "P-1000098")
    _pairing(db, logic, t3, n3, BASE_DATE + timedelta(seconds=7))
    _stub_beagle(monkeypatch, [])  # bam_exists irrelevant to these warnings

    r = _report(db)
    assert r.warnings.low_coverage_tumors == [t1.primary_id]
    assert r.warnings.low_coverage_normals == [n2.primary_id]


def test_ambiguous_count_drops_na_bamnames(db):
    """dplyr's group_by drops NA groups: matched rows with no bamname
    (no anon_id) do not form an ambiguous group even when >1."""
    logic = _logic(db)
    a1 = _dmp_sample(db, "P-1000099-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000099", meta={"coverage": 300})
    a1n = _dmp_sample(db, "P-1000099-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000099", "ANA-N1", meta={"coverage": 588})
    _pairing(db, logic, a1, a1n, BASE_DATE + timedelta(seconds=5))
    a2 = _dmp_sample(db, "P-1000100-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000100", meta={"coverage": 300})
    a2n = _dmp_sample(db, "P-1000100-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=1), "ANA-N2", meta={"coverage": 588})
    _pairing(db, logic, a2, a2n, BASE_DATE + timedelta(seconds=6))

    r = _report(db)
    assert r.warnings.tumor_samples_with_multiple_ambiguous_normals == []


# ---------------------------------------------------------------------------
# combined case + clean set + non-DMP contract
# ---------------------------------------------------------------------------


def test_combined_case(db, monkeypatch):
    """One fixture triggering every bucket at once; also pins the frame
    order (paired rows newest-first, then unmatched rows newest-first)
    the bucket lists follow."""
    logic = _logic(db)
    # A: matched pair, low tumor coverage (80 < 100), normal fine. Normal
    # coverage 700: NOT flagged by warning 5 — its string "700" is not
    # lexicographically < "50" (the reference's character-typed
    # comparison, spec e quirk).
    a_t = _dmp_sample(db, "P-1000081-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000081", "ANA-T", meta={"coverage": 80})
    a_n = _dmp_sample(db, "P-1000081-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000081", "ANA-N", meta={"coverage": 700})
    _pairing(db, logic, a_t, a_n, BASE_DATE + timedelta(seconds=2))
    # B: two tumor records sharing bamname ANB-T, both paired + matched
    # (ambiguity — the same tumor sample name on two rows; pairing rows
    # are unique per tumor record)
    b1 = _dmp_sample(db, "P-1000082-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000082", "ANB-T", meta={"coverage": 300})
    b1n = _dmp_sample(db, "P-1000082-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000082", "ANB-N1", meta={"coverage": 800})
    _pairing(db, logic, b1, b1n, BASE_DATE + timedelta(seconds=0))
    b2 = _dmp_sample(db, "P-1000082-T02-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=1), "P-1000082", "ANB-T", meta={"coverage": 300})
    b2n = _dmp_sample(db, "P-1000082-N02-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=1), "P-1000082", "ANB-N2", meta={"coverage": 750})
    _pairing(db, logic, b2, b2n, BASE_DATE + timedelta(seconds=1))
    # C: unpaired, explicit matched_tag Matched -> warning 1 (+ summary
    # matched row), no beagle record -> warning 3, NA coverage with a
    # bamname -> warning 4
    c_t = _dmp_sample(
        db, "P-1000083-T01-IM6", TumorOrNormal.TUMOR,
        BASE_DATE + timedelta(hours=1, minutes=5), "P-1000083", "ANC-T",
        meta={"matched_tag": "Matched"},
    )
    # D: unpaired, no tag -> Unmatched; no beagle record -> warning 3.
    # Coverage 300 keeps it out of warning 4 (NA-coverage-with-bamname
    # would flag it there too).
    d_t = _dmp_sample(db, "P-1000084-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(hours=1), "P-1000084", "AND-T", meta={"coverage": 300})
    # E: matched pair, normal coverage NA (with bamname) -> warning 5
    e_t = _dmp_sample(db, "P-1000085-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE + timedelta(seconds=3), "P-1000085", "ANE-T", meta={"coverage": 300})
    e_n = _dmp_sample(db, "P-1000085-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE + timedelta(seconds=3), "P-1000085", "ANE-N")
    _pairing(db, logic, e_t, e_n, BASE_DATE + timedelta(seconds=3))
    # beagle: only the paired A/B/E sides have records.
    _stub_beagle(monkeypatch, _present(
        a_t.primary_id, a_n.primary_id,
        b1.primary_id, b1n.primary_id, b2.primary_id, b2n.primary_id,
        e_t.primary_id, e_n.primary_id,
    ))

    r = _report(db)
    # summary: matched rows = A, B1, B2, E, C (explicit tag) — 4 patients
    # (B1/B2 share patient P-1000082)
    assert r.summary.patients == 4
    assert r.summary.tn_pairs == 5
    assert r.summary.unmatched_tumors == 1
    # warnings (frame order: E s3, A s2, B2 s1, B1 s0, then unmatched C, D)
    assert r.warnings.matched_tumors_without_normal == [c_t.primary_id]
    assert r.warnings.tumor_samples_with_multiple_ambiguous_normals == [
        b2.primary_id,
        b1.primary_id,
    ]
    assert r.warnings.samples_with_bams_missing == [c_t.primary_id, d_t.primary_id]
    assert r.warnings.low_coverage_tumors == [a_t.primary_id, c_t.primary_id]
    assert r.warnings.low_coverage_normals == [e_n.primary_id]


def test_clean_set_produces_zero_counts_and_empty_warnings(db, monkeypatch):
    """A fully clean frame: every count zero/one as expected, every
    warning bucket empty."""
    logic = _logic(db)
    t1 = _dmp_sample(db, "P-1000091-T01-IM6", TumorOrNormal.TUMOR, BASE_DATE, "P-1000091", "ANT-T1", meta={"coverage": 300})
    # normal coverage 588: "588" is not lexicographically < "50" (the
    # reference's character-typed comparison — spec e quirk), so the clean
    # set stays clean.
    n1 = _dmp_sample(db, "P-1000091-N01-IM6", TumorOrNormal.NORMAL, BASE_DATE, "P-1000091", "ANT-N1", meta={"coverage": 588})
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))
    _stub_beagle(monkeypatch, _present(t1.primary_id, n1.primary_id))

    r = _report(db)
    assert r.summary.patients == 1
    assert r.summary.tn_pairs == 1
    assert r.summary.unmatched_tumors == 0
    assert r.warnings.matched_tumors_without_normal == []
    assert r.warnings.tumor_samples_with_multiple_ambiguous_normals == []
    assert r.warnings.samples_with_bams_missing == []
    assert r.warnings.low_coverage_tumors == []
    assert r.warnings.low_coverage_normals == []


def test_empty_dmp_logic_zero_summary(db):
    logic = _logic(db)
    r = _report(db)
    assert r.summary.patients == 0
    assert r.summary.tn_pairs == 0
    assert r.summary.unmatched_tumors == 0
    assert all(getattr(r.warnings, f) == [] for f in (
        "matched_tumors_without_normal",
        "tumor_samples_with_multiple_ambiguous_normals",
        "samples_with_bams_missing",
        "low_coverage_tumors",
        "low_coverage_normals",
    ))


def test_non_dmp_logic_keeps_contract(db):
    """Non-DMP logics: summary/warnings stay null — the existing response
    shape is unchanged."""
    logic = _logic(db, name="ARGOS", strategy_name="argos")
    t1 = Sample(
        primary_id="ARG-T1",
        cmo_sample_name="ARG-T1",
        gene_panel="AC1",
        bait_set="AC1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        sample_type="LIMS",
        created_date=BASE_DATE,
    )
    n1 = Sample(
        primary_id="ARG-N1",
        cmo_sample_name="ARG-N1",
        gene_panel="AC1",
        bait_set="AC1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sample_type="LIMS",
        created_date=BASE_DATE,
    )
    db.add_all([t1, n1])
    db.flush()
    _pairing(db, logic, t1, n1, BASE_DATE + timedelta(seconds=5))

    # Query-constrained param passed explicitly in direct calls (suite
    # convention): a bare call would hand the Query() default object.
    r = pairing_report("ARGOS", 1, 50, db, q=None)
    assert r.summary is None
    assert r.warnings is None
    assert len(r.rows) == 1
