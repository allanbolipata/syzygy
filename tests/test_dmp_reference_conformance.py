"""DMP reference conformance (ticket 7/8 of DMP Reference Alignment).

End-to-end pin of syzygy's DMP behavior to the authoritative reference
(``pair_tumor_with_normal.R`` + ``generate_dmp_tumor_normal_key.pl``):

  one shared fixture set (26 samples, 13 DMP patients, 13 triggered tumors)
  -> real strategy pairing (two-tier: pool_id primary, IM_ver rescue,
     no-winner fan-out)
  -> the 15-column reference pair file (tumor rows: latest paired rows
     created-desc, then unmatched tumor rows created-desc)
  -> the 3 reference summary counts
  -> the 5 reference QC warning buckets
  -> the report response rows and the TSV export.

Every expectation below is derived from the reference logic
(``docs/dmp_reference_alignment.md`` sections b-f, which specify the R script
line by line). The fixtures are synthetic DMP records in the beagle
``DMP_data.json`` filemetadata shape (the 2/8 ingest contract), so the whole
chain under test is: ingest record -> remap -> pairing -> report.

Reference stretch (Rscript): SKIPPED — ``Rscript`` is not installed on this
host (checked 2026-11-05), so the suite relies on the pinned expectations
and does not depend on R.

The only sanctioned syzygy <-> reference deviations are exercised and pinned
here:
  * ``bam_exists`` is derived from the beagle file record's non-empty path
    (stubbed below) instead of the reference's Perl mirror check;
  * ``pool_id`` is the renamed ``project_name``.

The reference's warning-5 character-column quirk (spec e) is pinned by case
1: its normal's coverage is 100 — numerically fine, but the reference's
normal-side column is character-typed after the rescue ``ifelse``, so
``"100" < "50"`` evaluates TRUE lexicographically and the normal is flagged.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import routers.pairings as pairings
from database import Base
from models import PairingLogic, PairingTable, Sample
from pairing_service import trigger_pairing
from routers.pairings import (
    DMP_EXPORT_COLUMNS,
    _dmp_pair_cells,
    _dmp_report_stats,
    _iter_dmp_pair_cells,
    export_pair_report,
    pairing_report,
)
from dmp_translator import to_dmp_sample_kwargs

UTC = timezone.utc


def dt(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture set — synthetic DMP records in the beagle DMP_data.json shape
# ---------------------------------------------------------------------------

def _meta(sample, ttype, patient_dmp, anon_id, pool=None, coverage=600,
          matched_tag=None):
    """A beagle ``file_system.filemetadata``-style DMP record (2/8 shape)."""
    meta = {
        "type": ttype,
        "assay": "IM6",
        "sample": sample,
        "anon_id": anon_id,
        "patient": {"cmo": f"CMO-{patient_dmp}", "dmp": patient_dmp},
        "updated": "2021-08-05 21:14:29.400530+00:00",
        "version": 1,
        "imported": "2021-08-05 21:14:29.400495+00:00",
        "cmo_assay": "IMPACT468",
        "tumor_type": "lung",
        "external_id": f"EXT-{sample}",
        "sample_type": "1" if ttype == "T" else "0",
        "tissue_type": "fresh",
        "primary_site": None,
        "project_name": pool,
        "part_c_consent": 1,
        "metastasis_site": None,
        "somatic_calling_status": None,
        "major_allele_contamination": 0.451,
        "minor_allele_contamination": 0.00085,
    }
    if coverage is not None:
        meta["coverage"] = coverage
    if matched_tag is not None:
        meta["matched_tag"] = matched_tag
    return meta


# (sample name, type, anon_id, pool, coverage, matched_tag)
RECORDS = [
    # 1. clean single pair (same patient + pool_id). The normal's coverage
    #    is 100 — pins the reference's warning-5 character-column quirk:
    #    "100" < "50" is TRUE lexicographically, so it is flagged even
    #    though 100 >= 50 numerically.
    ("P-7000001-T01-IM6", "T", "AT01T", "POOL1", 600, None),
    ("P-7000001-N01-IM6", "N", "AT01N", "POOL1", 100, None),
    # 2. ambiguous: two same-pool normals -> fan-out, no winner
    ("P-7000002-T01-IM6", "T", "AT02T", "POOL2", 600, None),
    ("P-7000002-N01-IM6", "N", "AT02N1", "POOL2", 588, None),
    ("P-7000002-N02-IM7", "N", "AT02N2", "POOL2", 588, None),
    # 3. rescue-only: same patient, different pool, same IM_ver
    ("P-7000003-T01-IM6", "T", "AT03T", "POOL3A", 600, None),
    ("P-7000003-N01-IM6", "N", "AT03N", "POOL3B", 588, None),
    # 4. pool beats IM_ver: same-pool normal present (IM8) + other-pool
    #    normal with the tumor's own ver (IM7) -> only the pool normal pairs
    ("P-7000004-T01-IM7", "T", "AT04T", "POOL4A", 600, None),
    ("P-7000004-N01-IM8", "N", "AT04N1", "POOL4A", 588, None),
    ("P-7000004-N02-IM7", "N", "AT04N2", "POOL4B", 588, None),
    # 5. one normal shared by two same-pool tumors
    ("P-7000005-N01-IM6", "N", "AT05N", "POOL5", 588, None),
    ("P-7000005-T01-IM6", "T", "AT05T1", "POOL5", 600, None),
    ("P-7000005-T02-IM6", "T", "AT05T2", "POOL5", 600, None),
    # 6. matched flag, no normal at all
    ("P-7000006-T01-IM6", "T", "AT06T", "POOL6", 600, "Matched"),
    # 7. low-coverage tumor (<100)
    ("P-7000007-T01-IM6", "T", "AT07T", "POOL7", 45, None),
    ("P-7000007-N01-IM6", "N", "AT07N", "POOL7", 588, None),
    # 8. low-coverage normal (<50)
    ("P-7000008-T01-IM6", "T", "AT08T", "POOL8", 600, None),
    ("P-7000008-N01-IM6", "N", "AT08N", "POOL8", 45, None),
    # 9. NA coverage (paired tumor)
    ("P-7000009-T01-IM6", "T", "AT09T", "POOL9", None, None),
    ("P-7000009-N01-IM6", "N", "AT09N", "POOL9", 588, None),
    # 10. missing BAM: the normal's beagle file record has no path
    ("P-7000010-T01-IM6", "T", "AT10T", "POOL10", 600, None),
    ("P-7000010-N01-IM6", "N", "AT10N", "POOL10", 588, None),
    # 11. unparseable tumor name (no IM_ver segment)
    ("P-7000011-T01", "T", "AT11T", "POOL11A", 600, None),
    ("P-7000011-N01-IM6", "N", "AT11N", "POOL11B", 588, None),
    # 12. missing pool_id on both sides (tier-1 skipped, tier-2 rescue)
    ("P-7000012-T01-IM6", "T", "AT12T", None, 600, None),
    ("P-7000012-N01-IM6", "N", "AT12N", None, 588, None),
    # 13. unpaired normal (no tumor for this patient)
    ("P-7000013-N01-IM6", "N", "AT13N", "POOL13", 588, None),
]

TUMOR_NAMES = [r[0] for r in RECORDS if r[1] == "T"]  # 13 tumors
MISSING_BAM = {"P-7000010-N01-IM6"}  # beagle stub: no file path -> "0"

# Pairing-row created dates (pin the created-desc report order).
PAIRING_DATES = {
    ("P-7000012-T01-IM6", "P-7000012-N01-IM6"): dt(2026, 3, 10, 10, 0, 10),
    ("P-7000010-T01-IM6", "P-7000010-N01-IM6"): dt(2026, 3, 10, 10, 0, 9),
    ("P-7000009-T01-IM6", "P-7000009-N01-IM6"): dt(2026, 3, 10, 10, 0, 8),
    ("P-7000008-T01-IM6", "P-7000008-N01-IM6"): dt(2026, 3, 10, 10, 0, 7),
    ("P-7000007-T01-IM6", "P-7000007-N01-IM6"): dt(2026, 3, 10, 10, 0, 6),
    ("P-7000005-T02-IM6", "P-7000005-N01-IM6"): dt(2026, 3, 10, 10, 0, 5),
    ("P-7000005-T01-IM6", "P-7000005-N01-IM6"): dt(2026, 3, 10, 10, 0, 4),
    ("P-7000004-T01-IM7", "P-7000004-N01-IM8"): dt(2026, 3, 10, 10, 0, 3),
    ("P-7000003-T01-IM6", "P-7000003-N01-IM6"): dt(2026, 3, 10, 10, 0, 2),
    ("P-7000002-T01-IM6", "P-7000002-N02-IM7"): dt(2026, 3, 10, 10, 0, 1),
    ("P-7000002-T01-IM6", "P-7000002-N01-IM6"): dt(2026, 3, 10, 10, 0, 0),
    ("P-7000001-T01-IM6", "P-7000001-N01-IM6"): dt(2026, 3, 10, 9, 59, 59),
}

# Unmatched tumor samples get later created dates so they pin the tail order
# (created-desc) of the unmatched section.
UNMATCHED_CREATED = {
    "P-7000006-T01-IM6": dt(2026, 3, 11, 9, 0, 0),
    "P-7000011-T01": dt(2026, 3, 11, 8, 0, 0),
}
DEFAULT_SAMPLE_CREATED = dt(2026, 3, 1, 0, 0, 0)

# ---------------------------------------------------------------------------
# Pinned expectations — derived from the reference (spec b-e)
# ---------------------------------------------------------------------------

COLUMNS = [c["key"] for c in DMP_EXPORT_COLUMNS]

# The full 15-column pair file, in reference order (12 paired rows
# created-desc, then 2 unmatched rows created-desc).
EXPECTED_FRAME = [
    # 12. missing pool both sides -> tier-2 rescue match, pool_id NA
    ["P-7000012-T01-IM6", "P-7000012", "T01", "IM6", "AT12T", "NA",
     "Matched", "1", "600", "P-7000012-N01-IM6", "AT12N", "IM6", "NA", "1",
     "588"],
    # 10. normal BAM missing -> normal_bam_exists "0"
    ["P-7000010-T01-IM6", "P-7000010", "T01", "IM6", "AT10T", "NA",
     "Matched", "1", "600", "P-7000010-N01-IM6", "AT10N", "IM6", "POOL10",
     "0", "588"],
    # 9. NA tumor coverage (paired) -> "NA" cell + low-coverage-tumor warning
    ["P-7000009-T01-IM6", "P-7000009", "T01", "IM6", "AT09T", "NA",
     "Matched", "1", "NA", "P-7000009-N01-IM6", "AT09N", "IM6", "POOL9",
     "1", "588"],
    # 8. low-coverage normal
    ["P-7000008-T01-IM6", "P-7000008", "T01", "IM6", "AT08T", "NA",
     "Matched", "1", "600", "P-7000008-N01-IM6", "AT08N", "IM6", "POOL8",
     "1", "45"],
    # 7. low-coverage tumor
    ["P-7000007-T01-IM6", "P-7000007", "T01", "IM6", "AT07T", "NA",
     "Matched", "1", "45", "P-7000007-N01-IM6", "AT07N", "IM6", "POOL7",
     "1", "588"],
    # 5b. shared normal, second tumor
    ["P-7000005-T02-IM6", "P-7000005", "T02", "IM6", "AT05T2", "NA",
     "Matched", "1", "600", "P-7000005-N01-IM6", "AT05N", "IM6", "POOL5",
     "1", "588"],
    # 5a. shared normal, first tumor
    ["P-7000005-T01-IM6", "P-7000005", "T01", "IM6", "AT05T1", "NA",
     "Matched", "1", "600", "P-7000005-N01-IM6", "AT05N", "IM6", "POOL5",
     "1", "588"],
    # 4. pool beats IM_ver: the same-pool IM8 normal wins; no rescue row
    ["P-7000004-T01-IM7", "P-7000004", "T01", "IM7", "AT04T", "NA",
     "Matched", "1", "600", "P-7000004-N01-IM8", "AT04N1", "IM8", "POOL4A",
     "1", "588"],
    # 3. rescue-only: different pool, same IM_ver
    ["P-7000003-T01-IM6", "P-7000003", "T01", "IM6", "AT03T", "NA",
     "Matched", "1", "600", "P-7000003-N01-IM6", "AT03N", "IM6", "POOL3A",
     "1", "588"],
    # 2b. ambiguous fan-out, second normal
    ["P-7000002-T01-IM6", "P-7000002", "T01", "IM6", "AT02T", "NA",
     "Matched", "1", "600", "P-7000002-N02-IM7", "AT02N2", "IM7", "POOL2",
     "1", "588"],
    # 2a. ambiguous fan-out, first normal
    ["P-7000002-T01-IM6", "P-7000002", "T01", "IM6", "AT02T", "NA",
     "Matched", "1", "600", "P-7000002-N01-IM6", "AT02N1", "IM6", "POOL2",
     "1", "588"],
    # 1. clean single pair; the "100" normal coverage is the spec-e
    #    character-column quirk (lexicographically < "50", numerically fine)
    ["P-7000001-T01-IM6", "P-7000001", "T01", "IM6", "AT01T", "NA",
     "Matched", "1", "600", "P-7000001-N01-IM6", "AT01N", "IM6", "POOL1",
     "1", "100"],
    # 6. matched flag, no normal -> unmatched row, normal_IM_ver backfilled
    ["P-7000006-T01-IM6", "P-7000006", "T01", "IM6", "AT06T", "NA",
     "Matched", "1", "600", "NA", "NA", "IM6", "POOL6", "NA", "NA"],
    # 11. unparseable name -> no tier-2 match, idx/ver NA, Unmatched
    ["P-7000011-T01", "P-7000011", "NA", "NA", "AT11T", "NA", "Unmatched",
     "1", "600", "NA", "NA", "NA", "POOL11A", "NA", "NA"],
]

# (tumor sample, normal sample|None) in report-row order (== frame order).
EXPECTED_ROW_ORDER = [
    ("P-7000012-T01-IM6", "P-7000012-N01-IM6"),
    ("P-7000010-T01-IM6", "P-7000010-N01-IM6"),
    ("P-7000009-T01-IM6", "P-7000009-N01-IM6"),
    ("P-7000008-T01-IM6", "P-7000008-N01-IM6"),
    ("P-7000007-T01-IM6", "P-7000007-N01-IM6"),
    ("P-7000005-T02-IM6", "P-7000005-N01-IM6"),
    ("P-7000005-T01-IM6", "P-7000005-N01-IM6"),
    ("P-7000004-T01-IM7", "P-7000004-N01-IM8"),
    ("P-7000003-T01-IM6", "P-7000003-N01-IM6"),
    ("P-7000002-T01-IM6", "P-7000002-N02-IM7"),
    ("P-7000002-T01-IM6", "P-7000002-N01-IM6"),
    ("P-7000001-T01-IM6", "P-7000001-N01-IM6"),
    ("P-7000006-T01-IM6", None),
    ("P-7000011-T01", None),
]

# 3 summary counts (spec e.1): patients (distinct, both sides),
# tn_pairs (distinct matched rows), unmatched_tumors (unmatched rows).
EXPECTED_SUMMARY = {
    "patients": 11,  # P-7000013 has no tumor -> not counted
    "tn_pairs": 13,  # 12 rows + case-6 matched-tumor-without-normal row
    "unmatched_tumors": 1,  # P-7000011-T01 only (case 6 is matched)
}

# 5 QC warning buckets (spec e.2), pair-frame order, deduped.
# low_coverage_normals is lexicographic (spec e quirk): "45" < "50" AND
# "100" < "50" as strings — P-7000001's numerically-fine 100 coverage is
# flagged on purpose; the healthy "588" normals are not.
EXPECTED_WARNINGS = {
    "matched_tumors_without_normal": ["P-7000006-T01-IM6"],
    "tumor_samples_with_multiple_ambiguous_normals": ["P-7000002-T01-IM6"],
    "samples_with_bams_missing": ["P-7000010-T01-IM6", "P-7000010-N01-IM6"],
    "low_coverage_tumors": ["P-7000009-T01-IM6", "P-7000007-T01-IM6"],
    "low_coverage_normals": ["P-7000008-N01-IM6", "P-7000001-N01-IM6"],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


@pytest.fixture()
def stub(monkeypatch):
    """Beagle bam_exists stub: every fixture sample resolves to a path,
    except the ones in MISSING_BAM (no file path -> "0"). Returns the
    name -> "1"/"0" map, in the shape ``_beagle_bam_exists_map`` emits."""
    names = sorted({r[0] for r in RECORDS})
    monkeypatch.setattr(
        pairings.settings,
        "beagle_database_url",
        "postgresql+psycopg2://beagle:beagle@localhost/beagle",
    )
    monkeypatch.setattr(
        pairings, "_beagle_engine_cache",
        _StubBeagleEngine([(name, name not in MISSING_BAM) for name in names]),
    )
    return {name: ("1" if name not in MISSING_BAM else "0") for name in names}


@pytest.fixture()
def conformance(db):
    """Build the fixture set, run the real strategy, pin row dates."""
    logic = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    db.add(logic)
    db.flush()

    for sample, ttype, anon, pool, coverage, matched_tag in RECORDS:
        # The stored patient__dmp keeps the reference's full "P-" prefix.
        patient = "P-" + sample.split("-")[1]
        meta = _meta(sample, ttype, patient, anon, pool=pool,
                     coverage=coverage, matched_tag=matched_tag)
        s = Sample(**to_dmp_sample_kwargs(meta["sample"], meta))
        s.created_date = UNMATCHED_CREATED.get(sample, DEFAULT_SAMPLE_CREATED)
        db.add(s)
    db.commit()

    tumors = db.scalars(
        select(Sample).where(Sample.primary_id.in_(TUMOR_NAMES))
    ).all()
    assert len(tumors) == len(TUMOR_NAMES)
    for t in sorted(tumors, key=lambda s: s.primary_id):
        trigger_pairing(db, t, logic, "automatic")
    db.commit()

    rows = db.scalars(
        select(PairingTable)
        .where(PairingTable.pairing_logic_id == logic.id)
    ).all()
    assert len(rows) == len(PAIRING_DATES), (
        "pairing rows do not match the pinned set: "
        + str([(r.tumor.primary_id, r.normal.primary_id) for r in rows])
    )
    for r in rows:
        key = (r.tumor.primary_id, r.normal.primary_id)
        r.created_date = PAIRING_DATES[key]
    db.commit()

    by_name = {s.primary_id: s for s in
               db.scalars(select(Sample)).all()}
    return SimpleNamespace(db=db, logic=logic, by_name=by_name)


@pytest.fixture()
def export_stream(engine, monkeypatch):
    """Point the report's stream session at the test engine (same pattern
    as tests/test_pairings_report.py)."""
    monkeypatch.setattr(
        "routers.pairings._open_stream_session", sessionmaker(bind=engine)
    )


def _frame(db, logic):
    """The full 15-column pair frame (bam_exists resolved internally via
    the beagle stub)."""
    return [list(cells) for cells in _iter_dmp_pair_cells(db, logic)]


def _stats(db, logic):
    """The reference summary + warning objects for the full pair frame."""
    return _dmp_report_stats(db, logic)


# ---------------------------------------------------------------------------
# Beagle stub (same pattern as tests/test_pairings_report.py)
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


# ---------------------------------------------------------------------------
# Whole-frame pins
# ---------------------------------------------------------------------------

def test_full_pair_file_frame(conformance, stub):
    """The entire 15-column reference pair file (14 rows, order, fill
    rules) equals the reference-derived expectation."""
    assert _frame(conformance.db, conformance.logic) == EXPECTED_FRAME


def test_column_set_is_the_15_reference_columns():
    assert COLUMNS == [
        "tumor_sample", "patient", "tumor_idx", "tumor_IM_ver",
        "tumor_bamname", "tumor_group", "tumor_matched", "tumor_bam_exists",
        "tumor_bam_coverage", "normal_sample", "normal_bamname",
        "normal_IM_ver", "pool_id", "normal_bam_exists", "normal_bam_coverage",
    ]


def test_summary_counts(conformance, stub):
    summary, _ = _stats(conformance.db, conformance.logic)
    assert (summary.patients, summary.tn_pairs,
            summary.unmatched_tumors) == (
        EXPECTED_SUMMARY["patients"],
        EXPECTED_SUMMARY["tn_pairs"],
        EXPECTED_SUMMARY["unmatched_tumors"],
    )


def test_all_five_warning_buckets(conformance, stub):
    _, warnings = _stats(conformance.db, conformance.logic)
    for bucket, expected in EXPECTED_WARNINGS.items():
        assert getattr(warnings, bucket) == expected, bucket


# ---------------------------------------------------------------------------
# Per-case pins (work item 1, one test per reference behavior)
# ---------------------------------------------------------------------------

def test_case01_clean_single_pair(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000001-T01-IM6"]
    assert rows == [EXPECTED_FRAME[-3]]  # the clean pair row (12th)
    assert rows[0][10] == "AT01N" and rows[0][12] == "POOL1"


def test_case01b_normal_coverage_100_flagged_lexicographically(conformance,
                                                               stub):
    """The spec-e quirk: the reference's normal-side coverage column is
    character-typed after the rescue ifelse, so the < 50 threshold applies
    lexicographically. 100 is numerically fine but "100" < "50" is TRUE —
    the normal must be flagged, exactly like the reference does it."""
    _, warnings = _stats(conformance.db, conformance.logic)
    assert "P-7000001-N01-IM6" in warnings.low_coverage_normals
    # the numerically-healthy 588 normals are NOT lexicographically < "50"
    assert "P-7000007-N01-IM6" not in warnings.low_coverage_normals


def test_case02_ambiguous_two_same_pool_normals_fan_out(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000002-T01-IM6"]
    assert len(rows) == 2  # no winner: both normals are kept
    assert {r[10] for r in rows} == {"AT02N1", "AT02N2"}
    assert all(r[6] == "Matched" for r in rows)
    # the reference's warning 2 (tumor_bamname on >1 matched row) fires
    _, warnings = _stats(conformance.db, conformance.logic)
    assert (warnings.tumor_samples_with_multiple_ambiguous_normals
            == ["P-7000002-T01-IM6"])


def test_case03_rescue_only_same_patient_different_pool_same_ver(
        conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000003-T01-IM6"]
    assert len(rows) == 1
    # tier-1 (pool POOL3A) found nothing -> tier-2 matched the POOL3B normal
    assert rows[0][9] == "P-7000003-N01-IM6"
    assert rows[0][12] == "POOL3A"  # pool_id is the tumor-side pool


def test_case04_pool_beats_im_ver_no_rescue_row(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000004-T01-IM7"]
    assert len(rows) == 1
    # the same-pool normal (IM8) is the pair; the other-pool normal that
    # shares the tumor's IM_ver (IM7) must NOT produce a rescue row
    assert rows[0][9] == "P-7000004-N01-IM8"
    assert not any(r[10] == "AT04N2" for r in frame)


def test_case05_normal_shared_by_two_same_pool_tumors(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[1] == "P-7000005" and r[9] is not None]
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"P-7000005-T01-IM6", "P-7000005-T02-IM6"}
    assert all(r[9] == "P-7000005-N01-IM6" for r in rows)


def test_case06_matched_flag_without_normal(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000006-T01-IM6"]
    assert len(rows) == 1
    row = rows[0]
    # tumor row present and UNMATCHED (no normal), explicit tag preserved,
    # normal_IM_ver backfilled with the tumor's own ver
    assert row[6] == "Matched"
    assert row[9:11] == ["NA", "NA"]
    assert row[11] == "IM6"
    # warning 1 + counted as matched in the summary (not unmatched_tumors)
    summary, warnings = _stats(conformance.db, conformance.logic)
    assert warnings.matched_tumors_without_normal == ["P-7000006-T01-IM6"]
    assert summary.unmatched_tumors == 1
    assert summary.tn_pairs == 13


def test_case07_low_coverage_tumor_and_normal(conformance, stub):
    _, warnings = _stats(conformance.db, conformance.logic)
    assert "P-7000007-T01-IM6" in warnings.low_coverage_tumors
    assert "P-7000008-N01-IM6" in warnings.low_coverage_normals
    # the healthy sides are not flagged
    assert "P-7000007-N01-IM6" not in warnings.low_coverage_normals
    assert "P-7000008-T01-IM6" not in warnings.low_coverage_tumors


def test_case08_na_coverage_and_missing_bam(conformance, stub):
    _, warnings = _stats(conformance.db, conformance.logic)
    # NA tumor coverage (paired) -> low-coverage-tumor warning
    assert "P-7000009-T01-IM6" in warnings.low_coverage_tumors
    # the NA-coverage tumor's normal is fine -> not flagged
    assert "P-7000009-N01-IM6" not in warnings.low_coverage_normals
    # missing BAM -> both sides of the pair listed (reference writes both)
    assert warnings.samples_with_bams_missing == [
        "P-7000010-T01-IM6", "P-7000010-N01-IM6"]
    frame = _frame(conformance.db, conformance.logic)
    row = [r for r in frame if r[0] == "P-7000010-T01-IM6"][0]
    assert row[13] == "0"  # normal_bam_exists
    assert row[7] == "1"  # tumor_bam_exists


def test_case09_unparseable_name_no_tier2_match(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000011-T01"]
    assert len(rows) == 1
    row = rows[0]
    # no IM_ver segment: idx/ver NA, no tier-1 (different pools) and no
    # tier-2 (empty ver) -> unmatched row
    assert row[2:4] == ["NA", "NA"]
    assert row[6] == "Unmatched"
    assert row[9:11] == ["NA", "NA"]
    # and the patient's normal never appears paired
    assert not any(r[9] == "P-7000011-N01-IM6" for r in frame)


def test_case10_missing_pool_id_both_sides(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    rows = [r for r in frame if r[0] == "P-7000012-T01-IM6"]
    assert len(rows) == 1
    # tier-1 skipped (no pool on either side), tier-2 rescue matched
    assert rows[0][12] == "NA"  # pool_id cell
    assert rows[0][9] == "P-7000012-N01-IM6"
    assert rows[0][11] == "IM6"


def test_case11_unpaired_normal_absent_from_pair_file(conformance, stub):
    frame = _frame(conformance.db, conformance.logic)
    # tumor rows only: the unpaired normal appears nowhere
    assert not any("P-7000013" in cell for row in frame
                   for cell in row if cell is not None)


# ---------------------------------------------------------------------------
# Report response + export (reference pair file as served)
# ---------------------------------------------------------------------------

def test_report_response_rows_total_and_order(conformance, stub):
    resp = pairing_report("DMP", 1, 200, conformance.db, q=None)
    assert resp.total == 14  # 12 paired rows + 2 unmatched tumor rows
    got = [
        (
            r.tumor.primary_id if r.tumor is not None else None,
            r.normal.primary_id if r.normal is not None else None,
        )
        for r in resp.rows
    ]
    assert got == EXPECTED_ROW_ORDER
    assert resp.summary.patients == 11
    assert resp.summary.tn_pairs == 13
    assert resp.summary.unmatched_tumors == 1
    assert resp.warnings.matched_tumors_without_normal == \
        ["P-7000006-T01-IM6"]
    assert resp.warnings.low_coverage_tumors == \
        ["P-7000009-T01-IM6", "P-7000007-T01-IM6"]


def test_report_response_rows_carry_pinned_cells(conformance, stub):
    resp = pairing_report("DMP", 1, 200, conformance.db, q=None)
    for i, row in enumerate(resp.rows):
        cells = _dmp_pair_cells(row, stub)
        assert list(cells) == EXPECTED_FRAME[i]


def test_export_tsv_is_the_reference_pair_file(conformance, stub, db,
                                               export_stream):
    async def drain():
        resp = export_pair_report("DMP", q=None, columns=None, fmt="tsv",
                                  db=db)
        lines = []
        async for chunk in resp.body_iterator:
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8")
            elif isinstance(chunk, memoryview):
                text = chunk.tobytes().decode("utf-8")
            else:
                text = chunk
            lines.extend(text.splitlines())
        return lines

    lines = asyncio.run(drain())
    assert lines[0] == "\t".join(COLUMNS)
    assert lines[1:] == ["\t".join(row) for row in EXPECTED_FRAME]
