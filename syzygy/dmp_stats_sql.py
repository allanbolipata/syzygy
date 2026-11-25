"""SQL-side DMP pair-frame aggregates (summary counts + 5 QC warnings).

``syzygy.routers.pairings._dmp_report_stats`` derives the reference's summary
block and QC warning files (pair_tumor_with_normal.R;
docs/dmp_reference_alignment.md sections d/e) by walking the logic's whole
pair frame in Python — ~450k rows on a full staging import, which made every
Pair Report request pay minutes of work for three numbers and five id lists.

This module computes the same values with set-based SQL: the pair frame is a
UNION ALL of the latest paired rows and the unmatched DMP tumor rows, every
reference cell is projected as a SQL expression, and each summary count /
warning bucket is one aggregate query. Nothing is materialized in Python.

The cell expressions mirror ``syzygy.dmp_translator.remap_dmp_record`` and
``pairings._dmp_pair_cells`` exactly — including the reference's quirks (the
"NA" sentinel, the character-typed normal coverage column, the
bamname-or-anon_id fallback, the matched_tag passthrough). The frame's
first-seen order (paired segment first, then unmatched, each created_date
DESC / id DESC) is reproduced via the ``seg``/``ord_created``/``ord_id``
columns so the deduplicated warning lists come out in the same order as the
Python reference.

Portability: SQLite (tests) and Postgres (staging/prod). JSON access uses
``->>`` (both dialects support it; Postgres JSONB returns text natively).
"""

from __future__ import annotations

from sqlalchemy import Float, Text, and_, case, func, literal as lit, or_, select
from sqlalchemy.orm import aliased

from syzygy.bait_sets import consent_gate_condition
from syzygy.models import PairingTable, Sample
from syzygy.schemas import TumorOrNormal

#: The reference's NA sentinel, as rendered by ``_dmp_pair_cells``.
NA = "NA"

#: pair_tumor_with_normal.R thresholds: tumor coverage < 100 (numeric),
#: normal coverage < 50 (character column — spec e quirk).
TUMOR_COVERAGE_THRESHOLD = 100.0
NORMAL_COVERAGE_THRESHOLD = "50"

#: Frame column names, in construction order: the ten reference cells the
#: stats read, then the frame-order keys (segment, created_date, row id).
FRAME_COLUMNS = (
    "tumor_sample",
    "patient",
    "tumor_bamname",
    "tumor_bam_exists",
    "tumor_coverage",
    "tumor_matched",
    "normal_sample",
    "normal_bamname",
    "normal_bam_exists",
    "normal_coverage",
    "seg",
    "ord_created",
    "ord_id",
)


def _json_str(sample: Sample, key: str):
    """``additional_metadata ->> key`` as text (NULL when the column or the
    key is absent)."""
    return sample.additional_metadata.op("->>")(lit(key))


def _cell_na(value):
    """Reference rendering: NULL/empty → "NA", else the value as text."""
    return case(
        (or_(value.is_(None), value == lit("")), lit(NA)),
        else_=func.cast(value, Text()),
    )


def _side_cells(sample, prefix: str):
    """(bamname, bam_exists, coverage) labeled reference cells for one
    sample side.

    bamname = explicit bamname metadata else anon_id (the known format
    carries no bamname); bam_exists = the ingest-time additional_metadata
    value (flatten_acela_record's pathChecks result), "0" when absent — the
    SQL stats path has no beagle caller resolution, so 0 means unresolved
    exactly as remap_dmp_record's caller default; coverage as-is (NULL = NA).
    """
    bamname = func.coalesce(_json_str(sample, "bamname"), sample.anon_id)
    # CAST before comparing: SQLite's ->> preserves the JSON value's type
    # (int 0 for {"bam_exists": 0}) and int 0 never equals text '0' there,
    # while Postgres ->> always returns text. Casting makes both agree.
    bam_exists = func.cast(
        func.coalesce(_json_str(sample, "bam_exists"), "0"), Text()
    )
    return (
        _cell_na(bamname).label(f"{prefix}bamname"),
        case((bam_exists == lit("0"), lit("0")), else_=lit("1")).label(
            f"{prefix}bam_exists"
        ),
        _cell_na(_json_str(sample, "coverage")).label(f"{prefix}coverage"),
    )


def _matched_expr(sample, paired: bool):
    """tumor_matched: an explicit matched_tag metadata value passes through
    (spec g: audited absent from real records); otherwise the pairing result.
    """
    tag = _json_str(sample, "matched_tag")
    fallback = lit("Matched") if paired else lit("Unmatched")
    return case(
        (and_(tag.isnot(None), tag != lit("")), tag),
        else_=fallback,
    ).label("tumor_matched")


def _patient_cell(sample):
    # The stored patient__dmp column wins; it is also the DMP pairing key,
    # so matched rows always carry it (the reference's name-parse fallback
    # only ever applies to rows the pairing never matched, where the value
    # drops out of every stats formula anyway).
    return _cell_na(func.coalesce(sample.patient__dmp, lit(""))).label("patient")


def frame_select(logic_id, consent_gated: bool = False):
    """The DMP pair frame as a labeled subquery, one row per reference
    pair-file row, in the report's first-seen order (see module docstring).
    The pair file is tumor-driven, so unpaired normals never appear.
    """
    paired_tumor = aliased(Sample, flat=True)
    paired_normal = aliased(Sample, flat=True)
    un_tumor = aliased(Sample, flat=True)

    t_bn, t_be, t_cov = _side_cells(paired_tumor, "tumor_")
    n_bn, n_be, n_cov = _side_cells(paired_normal, "normal_")
    # Column order is exactly FRAME_COLUMNS.
    paired_cols = (
        _cell_na(paired_tumor.primary_id).label("tumor_sample"),
        _patient_cell(paired_tumor),
        t_bn,
        t_be,
        t_cov,
        _matched_expr(paired_tumor, paired=True),
        _cell_na(paired_normal.primary_id).label("normal_sample"),
        n_bn,
        n_be,
        n_cov,
        lit(0).label("seg"),
        PairingTable.created_date.label("ord_created"),
        PairingTable.id.label("ord_id"),
    )
    paired_stmt = (
        select(*paired_cols)
        .select_from(PairingTable)
        .join(paired_tumor, PairingTable.tumor_id == paired_tumor.id, isouter=True)
        .join(paired_normal, PairingTable.normal_id == paired_normal.id, isouter=True)
        .where(
            PairingTable.pairing_logic_id == logic_id,
            PairingTable.latest.is_(True),
        )
    )
    if consent_gated:
        paired_stmt = paired_stmt.where(
            and_(
                consent_gate_condition(sample=paired_tumor),
                consent_gate_condition(sample=paired_normal),
            )
        )

    not_paired = (
        select(lit(1))
        .where(
            or_(
                PairingTable.tumor_id == un_tumor.id,
                PairingTable.normal_id == un_tumor.id,
            ),
            PairingTable.pairing_logic_id == logic_id,
            PairingTable.latest.is_(True),
        )
        .exists()
    )
    u_bn, u_be, u_cov = _side_cells(un_tumor, "tumor_")
    # Column order is exactly FRAME_COLUMNS; the normal side is all NA for
    # unmatched rows (the Python frame renders na(None) = "NA" there).
    unmatched_cols = (
        _cell_na(un_tumor.primary_id).label("tumor_sample"),
        _patient_cell(un_tumor),
        u_bn,
        u_be,
        u_cov,
        _matched_expr(un_tumor, paired=False),
        lit(NA).label("normal_sample"),
        lit(NA).label("normal_bamname"),
        lit(NA).label("normal_bam_exists"),
        lit(NA).label("normal_coverage"),
        lit(1).label("seg"),
        un_tumor.created_date.label("ord_created"),
        un_tumor.id.label("ord_id"),
    )
    unmatched_stmt = (
        select(*unmatched_cols)
        .select_from(un_tumor)
        .where(
            un_tumor.sample_type == lit("DMP"),
            un_tumor.tumor_or_normal == lit(TumorOrNormal.TUMOR.value),
            ~not_paired,
        )
    )
    if consent_gated:
        unmatched_stmt = unmatched_stmt.where(consent_gate_condition(sample=un_tumor))

    return paired_stmt.union_all(unmatched_stmt).subquery("dmp_pair_frame")


def _c(frame):
    return {name: frame.c[name] for name in FRAME_COLUMNS}


def _ordered(stmt, c):
    """Reproduce the Python frame's first-seen order: paired segment first,
    each segment created_date DESC / id DESC."""
    return stmt.order_by(c["seg"], c["ord_created"].desc(), c["ord_id"].desc())


def _dedup_limited(db, stmt) -> list:
    """First-seen-order dedup (the buckets are small relative to the frame;
    the SQL side does the expensive filtering)."""
    return list(dict.fromkeys(db.execute(stmt).scalars().all()))


def summary_counts(db, frame) -> dict:
    """The three summary counts over the frame (spec d)."""
    c = _c(frame)
    matched = c["tumor_matched"] == lit("Matched")
    row = db.execute(
        select(
            func.count(func.distinct(case((matched, c["patient"]), else_=None))),
            func.sum(case((matched, 1), else_=0)),
            func.sum(case((matched, 0), else_=1)),
        ).select_from(frame)
    ).one()
    return {
        "patients": row[0] or 0,
        "tn_pairs": row[1] or 0,
        "unmatched_tumors": row[2] or 0,
    }


def _numeric_coverage_for(dialect, cell):
    """R's ``as.numeric`` cast of a rendered coverage cell as a SQL
    expression: NULL (R's NA) for the sentinel/empty/non-numeric, else the
    float. Dialect-split because Postgres raises when casting garbage while
    SQLite silently yields 0; both branches return NULL for non-numerics."""
    if dialect == "postgresql":
        numeric = cell.op("~")(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
        return case(
            (and_(cell != lit(NA), cell != lit(""), numeric), func.cast(cell, Float())),
            else_=None,
        )
    # SQLite: no REGEXP by default. A cell is numeric when stripping the
    # numeric character set from both ends leaves nothing.
    stripped = func.trim(cell, lit("0123456789+-.eE"))
    numeric = and_(cell != lit(NA), cell != lit(""), stripped == lit(""))
    return case((numeric, func.cast(cell, Float())), else_=None)


def warning_matched_tumors_without_normal(db, frame) -> list:
    """W1: matched rows whose normal side has no BAM name."""
    c = _c(frame)
    return _dedup_limited(
        db,
        _ordered(
            select(c["tumor_sample"])
            .select_from(frame)
            .where(
                c["tumor_matched"] == lit("Matched"),
                c["normal_bamname"] == lit(NA),
            ),
            c,
        ),
    )


def ambiguous_bamnames(db, frame) -> set:
    """Tumor bamnames appearing on >1 MATCHED row (NA bamnames never count:
    dplyr's group_by drops NA groups)."""
    c = _c(frame)
    rows = (
        db.execute(
            select(c["tumor_bamname"])
            .select_from(frame)
            .where(
                c["tumor_matched"] == lit("Matched"),
                c["tumor_bamname"] != lit(NA),
            )
            .group_by(c["tumor_bamname"])
            .having(func.count() > 1)
        )
        .scalars()
        .all()
    )
    return set(rows)


def warning_ambiguous_normals(db, frame, ambiguous: set) -> list:
    """W2: the tumor_sample of ALL rows (matched or not) whose tumor_bamname
    is ambiguous — the reference writes that file without the matched filter."""
    if not ambiguous:
        return []
    c = _c(frame)
    return _dedup_limited(
        db,
        _ordered(
            select(c["tumor_sample"])
            .select_from(frame)
            .where(c["tumor_bamname"].in_(sorted(ambiguous))),
            c,
        ),
    )


def warning_bams_missing(db, frame) -> list:
    """W3: rows with either side's bam_exists == "0"; both sides' samples are
    listed (an NA normal side contributes nothing)."""
    c = _c(frame)
    rows = db.execute(
        _ordered(
            select(c["tumor_sample"], c["normal_sample"])
            .select_from(frame)
            .where(
                or_(
                    c["tumor_bam_exists"] == lit("0"),
                    c["normal_bam_exists"] == lit("0"),
                )
            ),
            c,
        )
    ).all()
    out: list = []
    for tumor_sample, normal_sample in rows:
        out.append(tumor_sample)
        if normal_sample != NA:
            out.append(normal_sample)
    return list(dict.fromkeys(out))


def warning_low_coverage_tumors(db, frame, dialect) -> list:
    """W4: (!is.na(bamname) & is.na(coverage)) | (coverage < 100). The second
    disjunct applies regardless of the bamname (a no-bamname row with present
    low coverage flags); a row NA on both is dropped (NA propagation)."""
    c = _c(frame)
    cov = _numeric_coverage_for(dialect, c["tumor_coverage"])
    return _dedup_limited(
        db,
        _ordered(
            select(c["tumor_sample"])
            .select_from(frame)
            .where(
                or_(
                    and_(
                        c["tumor_bamname"] != lit(NA),
                        c["tumor_coverage"] == lit(NA),
                    ),
                    cov < lit(TUMOR_COVERAGE_THRESHOLD),
                ),
                c["tumor_sample"] != lit(NA),
            ),
            c,
        ),
    )


def warning_low_coverage_normals(db, frame, dialect) -> list:
    """W5: same shape as W4, but the reference's normal-side column is
    character-typed (spec e quirk), so the threshold applies
    lexicographically to the rendered cell."""
    c = _c(frame)
    return _dedup_limited(
        db,
        _ordered(
            select(c["normal_sample"])
            .select_from(frame)
            .where(
                or_(
                    and_(
                        c["normal_bamname"] != lit(NA),
                        c["normal_coverage"] == lit(NA),
                    ),
                    and_(
                        c["normal_coverage"] != lit(NA),
                        c["normal_coverage"] < lit(NORMAL_COVERAGE_THRESHOLD),
                    ),
                ),
                c["normal_sample"] != lit(NA),
            ),
            c,
        ),
    )


def compute_stats(db, logic_id, consent_gated: bool = False):
    """(summary, warnings) for one logic's full DMP pair frame, computed
    entirely in SQL. Same contract as ``_dmp_report_stats``."""
    dialect = db.get_bind().dialect.name
    frame = frame_select(logic_id, consent_gated)
    summary = summary_counts(db, frame)
    ambiguous = ambiguous_bamnames(db, frame)
    warnings = {
        "matched_tumors_without_normal": warning_matched_tumors_without_normal(
            db, frame
        ),
        "tumor_samples_with_multiple_ambiguous_normals": warning_ambiguous_normals(
            db, frame, ambiguous
        ),
        "samples_with_bams_missing": warning_bams_missing(db, frame),
        "low_coverage_tumors": warning_low_coverage_tumors(db, frame, dialect),
        "low_coverage_normals": warning_low_coverage_normals(db, frame, dialect),
    }
    return summary, warnings
