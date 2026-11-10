"""Pairing endpoints.

DMP tumors pair with their own DMP normals (same ``patient__dmp`` and gene
panel) automatically via ``POST /pairing/trigger-dmp`` under the DMP
pairing logic. DMP samples are also pairable manually, but only under the
other logics (e.g. ARGOS) — ``POST /pairing/trigger`` refuses the DMP
strategy, which is reserved for the automatic trigger.
"""
import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from typing import Optional
from uuid import UUID
import csv
import io
import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import (
    and_,
    case,
    cast,
    create_engine,
    exists,
    func,
    or_,
    select,
    String,
    text,
    union_all,
)
from sqlalchemy.orm import Session, aliased, joinedload

from dmp_translator import remap_dmp_record
from database import SessionLocal, get_db, settings
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    DMP_LOGIC_NAME,
    HARMONY_LOGIC_NAME,
    _is_dmp_strategy,
    count_unpaired,
    evaluate_logic,
    find_logic_by_name,
    resolve_pairing_logic,
    trigger_dmp_pairing,
    trigger_harmony_pairing,
    trigger_pairing,
    trigger_pairing_for_unpaired_tumors,
)
from pairing_strategies import get_pairing_strategy, strategies
from schemas import (
    DmpReportStatsRead,
    DmpSummaryRead,
    DmpTriggerRead,
    DmpWarningsRead,
    EvaluateRead,
    HarmonyFrameRead,
    HarmonyRow,
    HarmonyTriggerRead,
    PairingRead,
    PairingReportRead,
    PairingReportRowRead,
    PairingTriggerCreate,
    SampleRead,
    TumorOrNormal,
    TriggerAllRead,
    UnmatchedCountRead,
)


router = APIRouter(prefix="/pairing", tags=["pairings"])


# Sample attributes that can be filtered on either side of a pairing.
SAMPLE_FILTER_FIELDS = (
    "primary_id",
    "cmo_sample_name",
    "patient__cmo",
    "request_id",
    "sample_type",
    "gene_panel",
    "bait_set",
    "run_mode",
)


@router.get("/", response_model=list[PairingRead])
def list_pairings(
    latest: bool = False,
    sample_id: Optional[UUID] = None,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
    primary_id: Optional[str] = None,
    cmo_sample_name: Optional[str] = None,
    patient__cmo: Optional[str] = None,
    request_id: Optional[str] = None,
    sample_type: Optional[str] = None,
    gene_panel: Optional[str] = None,
    bait_set: Optional[str] = None,
    run_mode: Optional[str] = None,
    tumor_or_normal: Optional[TumorOrNormal] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(PairingTable).options(
        joinedload(PairingTable.tumor),
        joinedload(PairingTable.normal),
        joinedload(PairingTable.pairing_logic_obj),
    )
    if latest:
        stmt = stmt.where(PairingTable.latest.is_(True))
    if sample_id is not None:
        if db.get(Sample, sample_id) is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        # Match the sample in either role.
        stmt = stmt.where(
            or_(PairingTable.tumor_id == sample_id, PairingTable.normal_id == sample_id)
        )
    # Sample attribute filters: all specified fields must match the same
    # sample, which may sit on either the tumor or the normal side.
    sample_filters = [
        (field, value)
        for field, value in (
            ("primary_id", primary_id),
            ("cmo_sample_name", cmo_sample_name),
            ("patient__cmo", patient__cmo),
            ("request_id", request_id),
            ("sample_type", sample_type),
            ("gene_panel", gene_panel),
            ("bait_set", bait_set),
            ("run_mode", run_mode),
            ("tumor_or_normal", tumor_or_normal.value if tumor_or_normal is not None else None),
        )
        if value is not None
    ]
    if sample_filters:
        tumor_alias = aliased(Sample)
        normal_alias = aliased(Sample)
        stmt = stmt.join(tumor_alias, PairingTable.tumor_id == tumor_alias.id)
        stmt = stmt.join(normal_alias, PairingTable.normal_id == normal_alias.id)
        tumor_conds = [getattr(tumor_alias, field) == value for field, value in sample_filters]
        normal_conds = [getattr(normal_alias, field) == value for field, value in sample_filters]
        stmt = stmt.where(or_(and_(*tumor_conds), and_(*normal_conds)))
    if pairing_logic_id is not None:
        if db.get(PairingLogic, pairing_logic_id) is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == pairing_logic_id)
    elif pairing_logic_name is not None:
        logic = find_logic_by_name(db, pairing_logic_name)
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == logic.id)
    if created_after is not None:
        stmt = stmt.where(PairingTable.created_date >= created_after)
    if created_before is not None:
        stmt = stmt.where(PairingTable.created_date <= created_before)
    # Stable ordering so limit/offset pagination is deterministic.
    stmt = stmt.order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.get("/unmatched/", response_model=list[SampleRead])
def list_unmatched_dmp_samples(
    pairing_logic_name: str,
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    """DMP samples that have no latest pairing under the given logic.

    The DMP report lists every DMP sample; unpaired ones are shown with
    the opposite side's columns empty. The Pairs UI used to download the
    whole DMP sample population and diff it client-side against the
    pairings (hundreds of sequential requests against a ~280k-row table
    per load); the set difference is computed here in one query instead.
    Same per-logic, latest-only semantics as the pairing list itself.
    """
    logic = find_logic_by_name(db, pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    stmt = (
        select(Sample)
        .where(Sample.sample_type == "DMP", ~paired)
        .order_by(Sample.created_date.desc(), Sample.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


# Pair Report server-side pagination. Default page size matches the
# client's previous page size; the max clamps a single response so a
# client can't request an unbounded page.
REPORT_DEFAULT_PAGE_SIZE = 50
REPORT_MAX_PAGE_SIZE = 200


# Sample fields the Pair Report free-text search (``q``) matches against —
# the former client-side search fields (app.js PAIR_SEARCH_FIELDS). A row
# matches when ANY of these is a case-insensitive substring match on
# EITHER side (tumor or normal); a NULL side simply never matches, the
# same as the client skipping null fields.
REPORT_SEARCH_FIELDS = (
    "patient__cmo",
    "patient__dmp",
    "cmo_sample_name",
    "primary_id",
    "bait_set",
)


def _search_pattern(q: str) -> str:
    """ILIKE pattern for a LITERAL case-insensitive substring.

    The client searched with ``String(value).toLowerCase().includes(q)`` —
    a plain substring — so ``%`` and ``_`` in the query are ordinary
    characters, not LIKE wildcards.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _search_conds(entity, q: str) -> list:
    """The five ILIKE conditions for one side (a Sample entity or alias)."""
    pattern = _search_pattern(q)
    return [
        getattr(entity, field).ilike(pattern, escape="\\")
        for field in REPORT_SEARCH_FIELDS
    ]


def _either_side_conds(tumor_alias, normal_alias, q: str):
    """OR'd match on any search field of EITHER side.

    Each side's conditions reference its own aliased Sample so the two
    sides never collide in one WHERE clause; columns of a NULL side
    compare to NULL (no match), exactly like the old client-side filter.
    """
    return or_(*_search_conds(tumor_alias, q), *_search_conds(normal_alias, q))


def _report_paired_page(
    db: Session, logic: PairingLogic, offset: int, limit: int, q: Optional[str] = None
) -> list[PairingTable]:
    """One slice of the logic's latest paired rows, in report order.

    Same predicate and ordering as ``GET /pairing/`` (latest rows,
    ``created_date desc, id desc``) so page boundaries are stable.
    With ``q``, the slice is restricted to rows the query matches on
    either side (NULL sides never match — the client-side semantics).
    """
    stmt = (
        select(PairingTable)
        .options(
            joinedload(PairingTable.tumor),
            joinedload(PairingTable.normal),
            joinedload(PairingTable.pairing_logic_obj),
        )
        .where(
            PairingTable.pairing_logic_id == logic.id,
            PairingTable.latest.is_(True),
        )
        .order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if q is not None:
        tumor_alias = aliased(Sample)
        normal_alias = aliased(Sample)
        stmt = stmt.join(
            tumor_alias, PairingTable.tumor_id == tumor_alias.id, isouter=True
        )
        stmt = stmt.join(
            normal_alias, PairingTable.normal_id == normal_alias.id, isouter=True
        )
        stmt = stmt.where(_either_side_conds(tumor_alias, normal_alias, q))
    return list(db.scalars(stmt).all())


def _report_paired_total(
    db: Session, logic: PairingLogic, q: str
) -> int:
    """SQL COUNT of the logic's latest paired rows matching the query.

    Same predicate as ``_report_paired_page`` with ``q``, so the count
    and the served pages always cover the same filtered set.
    """
    tumor_alias = aliased(Sample)
    normal_alias = aliased(Sample)
    return int(
        db.scalar(
            select(func.count(PairingTable.id))
            .select_from(PairingTable)
            .join(
                tumor_alias, PairingTable.tumor_id == tumor_alias.id, isouter=True
            )
            .join(
                normal_alias,
                PairingTable.normal_id == normal_alias.id,
                isouter=True,
            )
            .where(
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest.is_(True),
                _either_side_conds(tumor_alias, normal_alias, q),
            )
        )
        or 0
    )


def _report_unmatched_page(
    db: Session, logic: PairingLogic, offset: int, limit: int, q: Optional[str] = None
) -> list[Sample]:
    """One slice of the unmatched DMP TUMOR rows, in report order.

    DMP samples with no latest pairing under the logic,
    ``created_date desc, id desc``. The reference pair file is
    tumor-driven (spec b/c: every tumor row is kept; unpaired normals
    never appear in it), so the report's unmatched section is restricted
    to DMP tumors — unlike ``GET /pairing/unmatched/``, which still lists
    every unpaired DMP sample on either side.
    With ``q``, restricted to samples any report search field matches —
    an unmatched row carries the sample on the tumor side only (the
    normal side is null), so the search reduces to the tumor's own fields.
    """
    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    stmt = (
        select(Sample)
        .where(
            Sample.sample_type == "DMP",
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
            ~paired,
        )
        .order_by(Sample.created_date.desc(), Sample.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if q is not None:
        stmt = stmt.where(or_(*_search_conds(Sample, q)))
    return list(db.scalars(stmt).all())


def _report_unmatched_total(
    db: Session, logic: PairingLogic, q: Optional[str] = None
) -> int:
    """SQL COUNT of the unmatched DMP TUMOR rows for the logic.

    Uses the exact ``~paired`` predicate (plus the tumor-only restriction)
    that ``_report_unmatched_page`` serves rows with, so the count equals
    the number of rows the endpoint would actually return (DMP TUMORS with
    no latest pairing under the logic — the reference pair file is
    tumor-driven, so unpaired normals are not part of it). Unlike
    ``count_unpaired``'s ``unpaired``, this never falls back to a
    total-minus-paired estimate, so ``total`` stays exact. With ``q``,
    narrowed to the same search the page rows are filtered by.
    """
    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    stmt = select(func.count(Sample.id)).where(
        Sample.sample_type == "DMP",
        Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
        ~paired,
    )
    if q is not None:
        stmt = stmt.where(or_(*_search_conds(Sample, q)))
    return db.scalar(stmt) or 0


def _paired_row(p: PairingTable) -> PairingReportRowRead:
    """Map a latest pairing row to a report row (both sides present)."""
    return PairingReportRowRead.model_validate(p)


def _unmatched_row(s: Sample, logic: PairingLogic) -> PairingReportRowRead:
    """Map an unmatched DMP sample to a report row.

    The DMP pair file is tumor-driven (spec b/c), so the report's unmatched
    section only ever carries DMP TUMORS: the sample goes on the tumor side,
    the normal side is null, ``mismatched_keys`` is an empty list, and no
    pairing identity is set. (The normal-side branch is kept for safety;
    ``GET /pairing/unmatched/`` renders its own rows independently.)
    """
    is_tumor = s.tumor_or_normal == TumorOrNormal.TUMOR.value
    return PairingReportRowRead(
        pairing_logic=logic.name,
        tumor=s if is_tumor else None,
        normal=None if is_tumor else s,
        mismatched_keys=[],
        created_date=s.created_date,
    )


@router.get("/report/", response_model=PairingReportRead)
def pairing_report(
    pairing_logic_name: str,
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        REPORT_DEFAULT_PAGE_SIZE,
        ge=1,
        description="Rows per page (clamped to at most 200).",
    ),
    db: Session = Depends(get_db),
    q: Optional[str] = Query(
        None,
        description=(
            "Free-text search: case-insensitive substring matched against "
            "the report's search fields (patient CMO, DMP patient, sample "
            "name, primary ID, bait set) on either side of a row. Filters "
            "the FULL merged list; total and pagination reflect the "
            "filtered set."
        ),
    ),
    include_stats: bool = Query(
        True,
        description=(
            "DMP logics only: when false, summary and warnings are null "
            "and the full pair-frame walk is skipped entirely — rows and "
            "total are unchanged. The same data is served by GET "
            "/pairing/report-stats/. Default true keeps the 5/8 contract."
        ),
    ),
):
    """One page of the merged Pair Report list for the logic.

    Returns ``{"rows": [...], "total": int}``. The merged list is the
    logic's latest paired rows (both sides present), followed by — DMP
    logics only — the unmatched DMP TUMORS rendered as rows with the tumor
    on the tumor side and the normal side null: the reference pair file is
    tumor-driven (spec b/c), so unpaired DMP normals are not part of the
    report. ``total`` is the count of the FULL merged list, computed
    with a SQL COUNT (never materializing the whole unmatched set in memory),
    so the client can compute the page count.

    With ``q`` (free-text search), both sections are restricted to rows the
    query matches on either side — the server-side form of the former
    client-side ``pairMatchesQuery`` — and ``total`` becomes the count of the
    filtered list, so pages and counts reflect the search.

    DMP logics only (5/8): ``summary`` and ``warnings`` carry the
    reference's summary counts and QC warnings (spec d/e), computed over
    the FULL pair frame — independent of the requested page and of ``q``
    (the reference computes them over the whole pair file). Both are null
    for non-DMP logics.

    Walking that frame (~450k rows with per-batch beagle lookups) on every
    page request was the report's real cost, so it is cached (see the stats
    cache below) and skippable: ``include_stats=false`` returns the same
    rows/total with null stats, and ``GET /pairing/report-stats/`` serves
    the stats on their own. The UI pages with ``include_stats=false`` and
    fetches the stats separately.

    ``page`` is 1-based; ``page_size`` defaults to 50 and is clamped to
    ``[1, 200]``. Ordering is stable and deterministic: paired rows are
    ordered by (created_date desc, id desc) — the ``GET /pairing/`` ordering
    — then unmatched DMP rows by (created_date desc, id desc), so page
    boundaries never shift between repeated requests for the same logic.
    """
    logic = find_logic_by_name(db, pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    # Normalize q to the form the old client-side filter compared in
    # (trimmed, lowercased); blank means "no filter", like the client's
    # falsy-q branch.
    if q is not None:
        q = q.strip().lower() or None

    # paired section: the logic's latest rows with both sides present
    # (count_unpaired's "paired" counts exactly these); with a query,
    # count the matching subset instead.
    if q is None:
        paired_total = count_unpaired(db, pairing_logic_name)["paired"]
    else:
        paired_total = _report_paired_total(db, logic, q)
    # unmatched section (DMP logics only): count it with the SAME predicate
    # the rows are served with. We do NOT reuse count_unpaired's "unpaired"
    # here: for a bait-set-less DMP logic it falls back to
    # total-minus-paired, which can overcount and would make "total" drift
    # from the rows actually returned.
    if _is_dmp_strategy(logic):
        unmatched_total = _report_unmatched_total(db, logic, q)
    else:
        unmatched_total = 0
    total = paired_total + unmatched_total

    offset = (page - 1) * page_size
    limit = min(page_size, REPORT_MAX_PAGE_SIZE)

    rows: list[PairingReportRowRead] = []
    # The merged list is [paired rows [0, paired_total)) followed by
    # [unmatched rows [paired_total, total)). The requested slice
    # [offset, offset + limit) falls entirely within one section or straddles
    # the boundary; fetch the part of each section it covers.
    if offset < paired_total:
        take = min(limit, paired_total - offset)
        for p in _report_paired_page(db, logic, offset, take, q=q):
            rows.append(_paired_row(p))
    remaining = limit - len(rows)
    if remaining > 0 and unmatched_total > 0:
        # The slice has reached the unmatched section; its local offset is
        # 0 when the slice started inside the paired section.
        unmatched_offset = max(0, offset - paired_total)
        for s in _report_unmatched_page(db, logic, unmatched_offset, remaining, q=q):
            rows.append(_unmatched_row(s, logic))

    # DMP logics only (5/8): the reference's summary counts + QC warnings
    # over the FULL pair frame (spec d/e) — the reference computes them
    # over the whole pair file, so they ignore the requested page and the
    # q search (which filters rows only).
    summary = None
    warnings = None
    if _is_dmp_strategy(logic) and include_stats:
        summary, warnings = _dmp_report_stats_cached(db, logic)

    return PairingReportRead(
        rows=rows, total=total, summary=summary, warnings=warnings
    )


@router.get("/report-stats/", response_model=DmpReportStatsRead)
def pairing_report_stats(
    pairing_logic_name: str,
    db: Session = Depends(get_db),
):
    """The DMP summary counts + 5 QC warning buckets on their own (5/8).

    The same payload ``GET /pairing/report/`` carries for DMP logics, served
    separately so paging the report never re-walks the full pair frame.
    Cached per logic until the frame's watermark moves or the entry ages out
    (see the stats cache below). ``summary``/``warnings`` are null for
    non-DMP logics, matching the report's contract.
    """
    logic = find_logic_by_name(db, pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    if not _is_dmp_strategy(logic):
        return DmpReportStatsRead(summary=None, warnings=None)
    summary, warnings = _dmp_report_stats_cached(db, logic)
    return DmpReportStatsRead(summary=summary, warnings=warnings)


# ---------------------------------------------------------------------------
# Pair Report server-side download/export (T6).
#
# The Pair Report table holds only one server page (T4), so the old
# client-side "download the loaded set" only exported the visible page.
# The export is computed server-side and STREAMED: the merged list (latest
# paired rows, then — DMP logics only — the unmatched DMP tumor rows) is
# fetched in keyset batches (created_date desc, id desc), never
# materialized whole, and rendered to CSV/TSV one line at a time. Cell
# formatting for non-DMP logics mirrors the frontend table renderers
# (pairText / pairDate) so the file matches what the user sees.
#
# DMP logics (4/8) instead export the reference's 15-column pair file —
# tumor rows only, no placeholders: values come from the remapped DMP
# record (dmp_translator.remap_dmp_record, the 2/8 contract) and the
# bam_exists cells from the sample's beagle file record (spec f).
# ---------------------------------------------------------------------------

# Rows fetched per SQL round-trip while streaming the export. Keyset
# pagination (not OFFSET) means this stays O(1) in memory regardless of the
# logic's row count (DMP: ~284k samples / ~111k pairs).
EXPORT_BATCH_SIZE = 500


def _s(v) -> str:
    """Mirror the frontend ``x || ""`` for a scalar cell: null -> "", enums
    yield their value, everything else stringifies."""
    if v is None:
        return ""
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)


def _pair_text(v) -> str:
    """Mirror the frontend ``pairText``: how the table renders a possibly
    list / JSON / null sample field — arrays join with ", " (recursively),
    objects JSON-stringify, null/empty -> ""."""
    if v is None or v == "":
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_pair_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"))
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)


def _pair_date(v) -> str:
    """Mirror the frontend ``pairDate``: UTC minute precision
    ("YYYY-MM-DD HH:mm"); null -> "". A string passes through untouched."""
    if not v:
        return ""
    if isinstance(v, str):
        return v
    try:
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(v)


def _t_attr(f):
    return lambda r: _s(getattr(r.tumor, f)) if r.tumor is not None else ""


def _n_attr(f):
    return lambda r: _s(getattr(r.normal, f)) if r.normal is not None else ""


def _t_text(f):
    return lambda r: _pair_text(getattr(r.tumor, f)) if r.tumor is not None else ""


def _n_text(f):
    return lambda r: _pair_text(getattr(r.normal, f)) if r.normal is not None else ""


def _t_date(f):
    return lambda r: _pair_date(getattr(r.tumor, f)) if r.tumor is not None else ""


def _n_date(f):
    return lambda r: _pair_date(getattr(r.normal, f)) if r.normal is not None else ""


def _t_errs(f):
    return lambda r: "; ".join(getattr(r.tumor, f) or []) if r.tumor is not None else ""


def _n_errs(f):
    return lambda r: "; ".join(getattr(r.normal, f) or []) if r.normal is not None else ""


# Python mirror of app.js PAIR_COLUMNS: the report column model for a
# (non-DMP) logic, same order, with text renderers that reproduce the table
# display (c.text).
PAIR_EXPORT_COLUMNS = [
    {"key": "tumor_primary_id", "header": "TUMOR_SAMPLE", "text": _t_attr("primary_id")},
    {"key": "normal_primary_id", "header": "NORMAL_SAMPLE", "text": _n_attr("primary_id")},
    {"key": "tumor_bait_set", "header": "BAIT_SET", "text": _t_attr("bait_set")},
    {"key": "tumor_patient__cmo", "header": "TUMOR_PATIENT__CMO", "text": _t_attr("patient__cmo")},
    {"key": "tumor_patient__dmp", "header": "TUMOR_PATIENT__DMP", "text": _t_attr("patient__dmp")},
    {"key": "tumor_anon_id", "header": "TUMOR_ANON_ID", "text": _t_attr("anon_id")},
    {"key": "tumor_cmo_sample_name", "header": "TUMOR_CMO_SAMPLE_NAME", "text": _t_text("cmo_sample_name")},
    {"key": "tumor_role", "header": "TUMOR_ROLE", "text": _t_attr("tumor_or_normal")},
    {"key": "tumor_sample_type", "header": "TUMOR_SAMPLE_TYPE", "text": _t_attr("sample_type")},
    {"key": "tumor_gene_panel", "header": "TUMOR_GENE_PANEL", "text": _t_attr("gene_panel")},
    {"key": "tumor_request_id", "header": "TUMOR_REQUEST_ID", "text": _t_attr("request_id")},
    {"key": "tumor_run_mode", "header": "TUMOR_RUN_MODE", "text": _t_attr("run_mode")},
    {"key": "tumor_file_group", "header": "TUMOR_FILE_GROUP", "text": _t_attr("file_group")},
    {"key": "tumor_preservation", "header": "TUMOR_PRESERVATION", "text": _t_text("preservation")},
    {"key": "tumor_sample_origin", "header": "TUMOR_SAMPLE_ORIGIN", "text": _t_text("sample_origin")},
    {"key": "tumor_sequencing_date", "header": "TUMOR_SEQUENCING_DATE", "text": _t_date("sequencing_date")},
    {"key": "tumor_additional_metadata", "header": "TUMOR_ADDITIONAL_METADATA", "text": _t_text("additional_metadata")},
    {"key": "tumor_errors", "header": "TUMOR_ERRORS", "text": _t_errs("errors")},
    {"key": "tumor_id", "header": "TUMOR_ID", "text": _t_attr("id")},
    {"key": "normal_bait_set", "header": "NORMAL_BAIT_SET", "text": _n_attr("bait_set")},
    {"key": "normal_patient__cmo", "header": "NORMAL_PATIENT__CMO", "text": _n_attr("patient__cmo")},
    {"key": "normal_patient__dmp", "header": "NORMAL_PATIENT__DMP", "text": _n_attr("patient__dmp")},
    {"key": "normal_anon_id", "header": "NORMAL_ANON_ID", "text": _n_attr("anon_id")},
    {"key": "normal_cmo_sample_name", "header": "NORMAL_CMO_SAMPLE_NAME", "text": _n_text("cmo_sample_name")},
    {"key": "normal_role", "header": "NORMAL_ROLE", "text": _n_attr("tumor_or_normal")},
    {"key": "normal_sample_type", "header": "NORMAL_SAMPLE_TYPE", "text": _n_attr("sample_type")},
    {"key": "normal_gene_panel", "header": "NORMAL_GENE_PANEL", "text": _n_attr("gene_panel")},
    {"key": "normal_request_id", "header": "NORMAL_REQUEST_ID", "text": _n_attr("request_id")},
    {"key": "normal_run_mode", "header": "NORMAL_RUN_MODE", "text": _n_attr("run_mode")},
    {"key": "normal_file_group", "header": "NORMAL_FILE_GROUP", "text": _n_attr("file_group")},
    {"key": "normal_preservation", "header": "NORMAL_PRESERVATION", "text": _n_text("preservation")},
    {"key": "normal_sample_origin", "header": "NORMAL_SAMPLE_ORIGIN", "text": _n_text("sample_origin")},
    {"key": "normal_sequencing_date", "header": "NORMAL_SEQUENCING_DATE", "text": _n_date("sequencing_date")},
    {"key": "normal_additional_metadata", "header": "NORMAL_ADDITIONAL_METADATA", "text": _n_text("additional_metadata")},
    {"key": "normal_errors", "header": "NORMAL_ERRORS", "text": _n_errs("errors")},
    {"key": "normal_id", "header": "NORMAL_ID", "text": _n_attr("id")},
    {"key": "pairing_logic", "header": "PAIRING_LOGIC", "text": lambda r: r.pairing_logic or ""},
    {"key": "trigger_type", "header": "TRIGGER_TYPE", "text": lambda r: r.trigger_type or ""},
    {"key": "mismatched_keys", "header": "MISMATCHED_KEYS", "text": lambda r: "; ".join(r.mismatched_keys or [])},
    {"key": "created_date", "header": "CREATED_DATE", "text": lambda r: _pair_date(r.created_date)},
]

# The DMP logic's export column model: the reference's 15-column pair file
# (docs/dmp_reference_alignment.md section c; bam-mirror-tools
# pair_tumor_with_normal.R write.table block) — exact names and order.
# These defs carry no "text" renderer: DMP cells are derived per row from
# the remapped record + beagle bam_exists in _dmp_pair_cells (4/8).
DMP_EXPORT_COLUMNS = [
    {"key": "tumor_sample", "header": "tumor_sample"},
    {"key": "patient", "header": "patient"},
    {"key": "tumor_idx", "header": "tumor_idx"},
    {"key": "tumor_IM_ver", "header": "tumor_IM_ver"},
    {"key": "tumor_bamname", "header": "tumor_bamname"},
    {"key": "tumor_group", "header": "tumor_group"},
    {"key": "tumor_matched", "header": "tumor_matched"},
    {"key": "tumor_bam_exists", "header": "tumor_bam_exists"},
    {"key": "tumor_bam_coverage", "header": "tumor_bam_coverage"},
    {"key": "normal_sample", "header": "normal_sample"},
    {"key": "normal_bamname", "header": "normal_bamname"},
    {"key": "normal_IM_ver", "header": "normal_IM_ver"},
    {"key": "pool_id", "header": "pool_id"},
    {"key": "normal_bam_exists", "header": "normal_bam_exists"},
    {"key": "normal_bam_coverage", "header": "normal_bam_coverage"},
]
# Position of each pair-file column within _dmp_pair_cells' output.
_DMP_COLUMN_INDEX = {c["key"]: i for i, c in enumerate(DMP_EXPORT_COLUMNS)}


def _dmp_pair_cells(row: PairingReportRowRead, bam_map: dict) -> list:
    """The 15 reference pair-file cells for one DMP report row, in the
    reference's exact column order (spec c).

    Values come from the remapped DMP record (remap_dmp_record — the 2/8
    contract: bamname over anon_id, group/coverage/pool_id/matched_tag from
    additional_metadata, patient/idx/ver from the stored patient__dmp or the
    name). A missing value renders as the reference's literal "NA"
    (write.table default); bam_exists is "1"/"0" from the sample's beagle
    file record (spec f) and is never NA. ``tumor_matched`` passes the
    record's matched_tag through and falls back to the pairing result
    (Matched/Unmatched) — the flag is audited absent from real records
    (spec g, caveat). ``normal_IM_ver`` backfills the tumor's own IM_ver
    whenever the normal's is missing — unmatched rows, and paired rows
    whose normal name carries no version segment (the reference applies
    the ifelse to every row).
    """
    if row.tumor is None:
        # The DMP pair file is tumor-driven; callers emit tumor rows only.
        return ["NA"] * len(DMP_EXPORT_COLUMNS)
    t = remap_dmp_record(
        row.tumor, bam_exists=int(bam_map.get(row.tumor.primary_id, "0"))
    )
    n = (
        remap_dmp_record(
            row.normal, bam_exists=int(bam_map.get(row.normal.primary_id, "0"))
        )
        if row.normal is not None
        else None
    )

    def na(v):
        return "NA" if v in (None, "") else str(v)

    matched = (
        t["is_matched"]
        if t["is_matched"] not in (None, "")
        else ("Matched" if row.normal is not None else "Unmatched")
    )
    normal_im_ver = (n["ver"] if n is not None else None) or t["ver"]
    return [
        na(t["sample"]),
        na(t["patient"]),
        na(t["idx"]),
        na(t["ver"]),
        na(t["bamname"]),
        t["group"] if t["group"] not in (None, "") else "NA",
        matched,
        str(t["bam_exists"]),
        na(t["coverage"]),
        na(n["sample"]) if n is not None else "NA",
        na(n["bamname"]) if n is not None else "NA",
        na(normal_im_ver),
        na(t["pool_id"]),
        str(n["bam_exists"]) if n is not None else "NA",
        na(n["coverage"]) if n is not None else "NA",
    ]


def _dmp_batch_names(rows) -> set:
    """Sample names (both sides) present in one batch of report rows —
    the key set for the batch's bam_exists resolution."""
    names = set()
    for r in rows:
        if r.tumor is not None:
            names.add(r.tumor.primary_id)
        if r.normal is not None:
            names.add(r.normal.primary_id)
    return names


# Lazily-built read-only engine for the optional beagle DB (bam_exists);
# module-level so tests can reset it between cases.
_beagle_engine_cache = None


def _beagle_engine():
    """Lazily-built read-only engine for the optional beagle DB. None when
    BEAGLE_DATABASE_URL is unset or the engine fails to build."""
    global _beagle_engine_cache
    if not settings.beagle_database_url:
        return None
    if _beagle_engine_cache is None:
        try:
            _beagle_engine_cache = create_engine(
                settings.beagle_database_url, pool_pre_ping=True
            )
        except Exception:
            return None
    return _beagle_engine_cache


def _beagle_bam_exists_map(sample_names) -> dict:
    """Resolve DMP bam_exists for a batch of sample names (spec f, the
    sanctioned deviation from the reference's BAM-mirror scan): the
    sample's beagle file record — file_system_filemetadata (keyed by the
    metadata ``sample`` field, latest rows) -> file_system_file — has a
    non-empty path for any of its files -> "1", else "0". An
    unset/unreachable beagle DB, or a name with no file record, resolves
    to "0" (unresolvable -> 0)."""
    engine = _beagle_engine()
    if engine is None or not sample_names:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    select fm.metadata->>'sample' as name,
                           bool_or(f.path is not null and f.path <> '') as bam_exists
                      from file_system_filemetadata fm
                      join file_system_file f on f.id = fm.file_id and fm.latest
                     where fm.metadata->>'sample' = any(:names)
                     group by 1
                    """
                ),
                {"names": list(sample_names)},
            ).all()
    except Exception:
        return {}
    return {name: ("1" if bam else "0") for name, bam in rows if name}

# The frontend default column sets (app.js PAIR_DEFAULT_KEYS / DMP_COLUMNS).
PAIR_DEFAULT_EXPORT_KEYS = ["tumor_primary_id", "normal_primary_id", "tumor_bait_set"]
DMP_DEFAULT_EXPORT_KEYS = [
    "tumor_sample", "patient", "tumor_idx", "tumor_IM_ver", "tumor_bamname",
    "tumor_group", "tumor_matched", "tumor_bam_exists", "tumor_bam_coverage",
    "normal_sample", "normal_bamname", "normal_IM_ver", "pool_id",
    "normal_bam_exists", "normal_bam_coverage",
]

_EXPORT_COLUMNS_BY_KEY = {c["key"]: c for c in PAIR_EXPORT_COLUMNS}
_EXPORT_COLUMNS_BY_KEY.update({c["key"]: c for c in DMP_EXPORT_COLUMNS})


def _resolve_export_columns(logic: PairingLogic, columns: Optional[str]) -> list:
    """Resolve the requested export columns (comma-separated keys) into an
    ordered list of column defs, or the logic's default set when omitted /
    blank. Unknown keys are a 400.

    DMP logics export the reference pair file (4/8): the selectable columns
    are exactly its 15 columns (spec c) — pair-report keys are not valid
    there. Other logics keep the pair-report column set.
    """
    keys = None
    if columns is not None:
        keys = [k.strip() for k in columns.split(",") if k.strip()]
    dmp = _is_dmp_strategy(logic)
    if not keys:
        keys = DMP_DEFAULT_EXPORT_KEYS if dmp else PAIR_DEFAULT_EXPORT_KEYS
    defs = []
    for k in keys:
        if dmp and k not in _DMP_COLUMN_INDEX:
            raise HTTPException(
                status_code=400, detail=f"Unknown DMP pair-file column: {k}"
            )
        c = _EXPORT_COLUMNS_BY_KEY.get(k)
        if c is None:
            raise HTTPException(status_code=400, detail=f"Unknown export column: {k}")
        defs.append(c)
    return defs


def _escape_cell(value: str, sep: str) -> str:
    """Mirror the frontend cell escaping: TSV never quotes (control chars
    blanked); CSV quotes only when the field holds the separator, a quote,
    or a newline, doubling internal quotes; tab/CR/LF never survive."""
    if sep == "\t":
        return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if '"' in value or sep in value or "\n" in value:
        return '"' + value.replace('"', '""') + '"'
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _csv_line(cells: list, sep: str) -> str:
    return sep.join(_escape_cell(c, sep) for c in cells) + "\n"


def _iter_paired_export_rows(db: Session, logic: PairingLogic, q: Optional[str]):
    """Stream every latest paired row for the logic in report order
    (created_date desc, id desc) via keyset pagination, so the full set is
    never materialized. Same predicate as _report_paired_page (incl. the q
    either-side search); the keyset cursor replaces OFFSET."""
    base_stmt = (
        select(PairingTable)
        .options(
            joinedload(PairingTable.tumor),
            joinedload(PairingTable.normal),
            joinedload(PairingTable.pairing_logic_obj),
        )
        .where(PairingTable.pairing_logic_id == logic.id, PairingTable.latest.is_(True))
        .order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
    )
    if q is not None:
        tumor_alias = aliased(Sample)
        normal_alias = aliased(Sample)
        base_stmt = base_stmt.join(tumor_alias, PairingTable.tumor_id == tumor_alias.id, isouter=True)
        base_stmt = base_stmt.join(normal_alias, PairingTable.normal_id == normal_alias.id, isouter=True)
        base_stmt = base_stmt.where(_either_side_conds(tumor_alias, normal_alias, q))
    last_created = None
    last_id = None
    while True:
        stmt = base_stmt.limit(EXPORT_BATCH_SIZE)
        if last_created is not None:
            stmt = stmt.where(
                or_(
                    PairingTable.created_date < last_created,
                    and_(PairingTable.created_date == last_created, PairingTable.id < last_id),
                )
            )
        batch = list(db.scalars(stmt).all())
        if not batch:
            break
        for p in batch:
            yield _paired_row(p)
        if len(batch) < EXPORT_BATCH_SIZE:
            break
        last_created = batch[-1].created_date
        last_id = batch[-1].id


def _iter_unmatched_export_rows(db: Session, logic: PairingLogic, q: Optional[str]):
    """Stream every unmatched DMP TUMOR row for the logic in report order via
    keyset pagination. Same predicate as _report_unmatched_page (DMP TUMORS
    with no latest pairing under the logic — the pair file is tumor-driven,
    so unpaired normals never appear — plus the q search)."""
    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    base = [
        Sample.sample_type == "DMP",
        Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
        ~paired,
    ]
    if q is not None:
        base.append(or_(*_search_conds(Sample, q)))
    last_created = None
    last_id = None
    while True:
        stmt = (
            select(Sample)
            .where(*base)
            .order_by(Sample.created_date.desc(), Sample.id.desc())
            .limit(EXPORT_BATCH_SIZE)
        )
        if last_created is not None:
            stmt = stmt.where(
                or_(
                    Sample.created_date < last_created,
                    and_(Sample.created_date == last_created, Sample.id < last_id),
                )
            )
        batch = list(db.scalars(stmt).all())
        if not batch:
            break
        for s in batch:
            yield _unmatched_row(s, logic)
        if len(batch) < EXPORT_BATCH_SIZE:
            break
        last_created = batch[-1].created_date
        last_id = batch[-1].id


def _iter_dmp_pair_lines(rows, col_defs, sep: str):
    """Yield one reference pair-file line per DMP report row, in the
    reference's 15-column cell order (spec c), restricted/reordered to
    ``col_defs``.

    Wraps a row iterator (paired or unmatched section) and resolves
    bam_exists for each EXPORT_BATCH_SIZE chunk of rows at once, so the
    beagle file-record lookup is batched and the stream stays O(batch) in
    memory like the row iterators it wraps.
    """
    it = iter(rows)
    while True:
        batch = list(islice(it, EXPORT_BATCH_SIZE))
        if not batch:
            break
        bam_map = _beagle_bam_exists_map(_dmp_batch_names(batch))
        for row in batch:
            cells = _dmp_pair_cells(row, bam_map)
            yield _csv_line(
                [cells[_DMP_COLUMN_INDEX[c["key"]]] for c in col_defs], sep
            )


def _sanitize_filename(name: str) -> str:
    """Mirror the frontend filename sanitize (/[^\\w.-]+/g -> "_")."""
    import re
    return re.sub(r"[^\w.-]+", "_", name or "")


# ---------------------------------------------------------------------------
# DMP report summary counts + QC warnings (5/8).
#
# The reference's summary block and 5 warning files (pair_tumor_with_normal.R;
# spec d/e) are computed over the FINAL 15-column pair frame — here, the same
# cells _dmp_pair_cells renders for the DMP export, one row per report row
# (latest paired rows, then unmatched DMP tumor rows), over the FULL frame:
# the reference computes them over the whole pair file, so they are
# independent of the requested page and of the report's q search (which
# filters rows only).
# ---------------------------------------------------------------------------

# The reference's NA sentinel as rendered by _dmp_pair_cells.
DMP_REF_NA = "NA"

# The reference's low-coverage thresholds (pair_tumor_with_normal.R): tumor
# coverage < 100 (numeric), normal coverage < 50 (character column — see
# _dmp_report_stats warning 5 and the spec e quirk).
DMP_TUMOR_COVERAGE_THRESHOLD = 100.0
DMP_NORMAL_COVERAGE_THRESHOLD = "50"


def _ref_coverage_numeric(value: str):
    """R's ``as.numeric`` cast for a rendered coverage cell: a finite float
    when the cell parses as one, None (R's NA) for the literal "NA", empty,
    or unparseable values — as.numeric coerces non-numeric strings (including
    NaN/Inf tokens) to NA."""
    if value is None or value == DMP_REF_NA:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dedup(values) -> list:
    """Deduplicate, preserving first-seen order — the warning buckets list
    affected samples, not pair rows (a shared normal or a fan-out tumor
    appears once)."""
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _iter_dmp_pair_cells(db: Session, logic: PairingLogic):
    """Yield the 15 reference pair-file cells (spec c) for every row of the
    logic's FULL pair frame — latest paired rows, then unmatched DMP tumor
    rows, no search filter — with bam_exists resolved per
    EXPORT_BATCH_SIZE chunk via the beagle file records, exactly as the DMP
    export renders them."""
    for row_iter in (
        _iter_paired_export_rows(db, logic, None),
        _iter_unmatched_export_rows(db, logic, None),
    ):
        it = iter(row_iter)
        while True:
            batch = list(islice(it, EXPORT_BATCH_SIZE))
            if not batch:
                break
            bam_map = _beagle_bam_exists_map(_dmp_batch_names(batch))
            for row in batch:
                yield _dmp_pair_cells(row, bam_map)


# ---------------------------------------------------------------------------
# Caching for the full-frame DMP stats
#
# _dmp_report_stats walks the logic's whole pair frame (~450k rows, with
# per-batch beagle bam_exists lookups) no matter which page was requested,
# so running it on every page request dominated the Pair Report's load time
# (~17s/page, ~8MB/page of warning ids). Entries are cached per logic on the
# engine they were computed against and reused while a cheap watermark
# (this logic's latest pairing rows and the sample table, each as a row
# count + max modified_date) is unchanged. The watermark cannot see the
# beagle file records behind bam_exists, so entries also expire after
# DMP_STATS_TTL_SECONDS; the pairing triggers clear the cache outright.
# Keying the cache on the engine keeps per-test engines (each with its own
# monkeypatched beagle fixtures) from sharing results.
# ---------------------------------------------------------------------------

#: How long a watermark-matching DMP stats entry may be reused.
DMP_STATS_TTL_SECONDS = 300


def _dmp_stats_cache(db: Session):
    """The per-engine DMP stats cache, created on first use."""
    engine = db.get_bind()
    cache = getattr(engine, "_dmp_report_stats_cache", None)
    if cache is None:
        cache = {}
        try:
            engine._dmp_report_stats_cache = cache
        except Exception:
            return None
    return cache


def _dmp_stats_watermark(db: Session, logic: PairingLogic):
    """Cheap freshness token for the DMP pair frame: this logic's latest
    pairing rows and the sample table, each as (row count, max
    modified_date). Any pairing or sample edit moves it."""
    pairing = db.query(
        func.count(PairingTable.id),
        func.max(PairingTable.modified_date),
    ).filter(
        PairingTable.pairing_logic_id == logic.id,
        PairingTable.latest.is_(True),
    ).one()
    sample = db.query(
        func.count(Sample.id),
        func.max(Sample.modified_date),
    ).one()
    return (pairing[0], pairing[1], sample[0], sample[1])


def _invalidate_dmp_report_stats(db: Session) -> None:
    """Drop every cached DMP stats entry (a pairing run just changed the frame)."""
    cache = _dmp_stats_cache(db)
    if cache:
        cache.clear()


#: Bulk pairing passes run for minutes and hold a pooled DB connection for their
#: whole Python loop. Two passes for the same logic — the re-click after a
#: request the browser already gave up on, a second tab, trigger-all racing a
#: per-logic trigger — double the pairing work AND the connection pressure, and
#: that combination is what exhausted the connection pool. This registry is the
#: guard: it never touches the DB (a Postgres advisory lock would have to ride a
#: Session connection that commit() returns to the pool mid-pass), it is
#: per-process (uvicorn runs a single worker here), and a restart clears it.
_active_pairing_passes: set = set()
_active_pairing_passes_lock = threading.Lock()


@contextmanager
def _pairing_pass(db: Session, logic: Optional[PairingLogic], label: str):
    """Claim one bulk pairing pass.

    ``409`` when a pass for this logic is already running: the first pass keeps
    running even after the browser gives up, so a second click only adds work
    and another pooled connection. ``logic=None`` (the unscoped bulk backfill,
    or a trigger naming a logic row that does not exist) claims a shared key so
    two bulk backfills cannot stack either. The claim is released on the way out
    even if the pass raises.
    """
    key = logic.id if logic is not None else None
    with _active_pairing_passes_lock:
        if key in _active_pairing_passes:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A {label} pairing run is already in progress on this "
                    "server. It keeps running even if this tab gives up — wait "
                    "for it instead of triggering again."
                ),
            )
        _active_pairing_passes.add(key)
    try:
        yield
    finally:
        with _active_pairing_passes_lock:
            _active_pairing_passes.discard(key)


def _dmp_report_stats_cached(db: Session, logic: PairingLogic):
    """_dmp_report_stats, reused across requests while the frame looks the
    same (watermark + TTL; see the cache notes above)."""
    cache = _dmp_stats_cache(db)
    if cache is None:
        return _dmp_report_stats(db, logic)
    key = str(logic.id)
    watermark = _dmp_stats_watermark(db, logic)
    hit = cache.get(key)
    if (
        hit is not None
        and hit[0] == watermark
        and (datetime.now(timezone.utc) - hit[1]).total_seconds() < DMP_STATS_TTL_SECONDS
    ):
        return hit[2], hit[3]
    summary, warnings = _dmp_report_stats(db, logic)
    cache[key] = (watermark, datetime.now(timezone.utc), summary, warnings)
    return summary, warnings


def _dmp_report_stats(db: Session, logic: PairingLogic):
    """The reference's 3 summary counts and 5 QC warnings (spec d/e) for
    the logic's full DMP pair frame, in the exact row sets and conditions
    pair_tumor_with_normal.R implements:

    summary (over the final 15-column frame):
      patients         = distinct patient among rows with tumor_matched
                         == "Matched"
      tn_pairs         = rows with tumor_matched == "Matched"
      unmatched_tumors = rows with tumor_matched != "Matched"
    warnings (each a deduplicated list of affected samples):
      1 matched_tumors_without_normal
        rows with tumor_matched == "Matched" and NA normal_bamname
      2 tumor_samples_with_multiple_ambiguous_normals
        count: tumor_bamname groups of >1 row among MATCHED rows;
        list: the tumor_sample of ALL rows (matched or not) whose
        tumor_bamname is in that set — the reference's file is written
        without the matched filter
      3 samples_with_bams_missing
        rows with tumor_bam_exists == "0" or normal_bam_exists == "0";
        both sides' samples are listed (a NA normal side contributes
        nothing)
      4 low_coverage_tumors
        rows with (!is.na(tumor_bamname) & is.na(coverage)) |
        (coverage < 100) — coverage is the reference's as.numeric cast of
        the rendered cell; the second disjunct applies regardless of the
        bamname (a no-bamname row with low present coverage flags), and a
        row that is NA on both is dropped
      5 low_coverage_normals
        rows with (!is.na(normal_bamname) & is.na(coverage)) |
        (coverage < 50) — the reference's normal-side column is
        character-typed (spec e quirk), so the threshold applies
        lexicographically to the rendered cell; same NA propagation as 4
    """
    ci = _DMP_COLUMN_INDEX
    patients: set = set()
    tn_pairs = 0
    unmatched_tumors = 0
    w_no_normal: list = []
    w_bams_missing: list = []
    w_low_tumors: list = []
    w_low_normals: list = []
    row_bams: list = []            # (tumor_bamname, tumor_sample) per row
    matched_bam_counts: dict = {}
    for cells in _iter_dmp_pair_cells(db, logic):
        tumor_sample = cells[ci["tumor_sample"]]
        matched = cells[ci["tumor_matched"]] == "Matched"
        if matched:
            tn_pairs += 1
            patients.add(cells[ci["patient"]])
            bamname = cells[ci["tumor_bamname"]]
            matched_bam_counts[bamname] = matched_bam_counts.get(bamname, 0) + 1
            # The reference's core QC case: the DMP program flagged the
            # tumor matched but no normal BAM could be found.
            if cells[ci["normal_bamname"]] == DMP_REF_NA:
                w_no_normal.append(tumor_sample)
        else:
            unmatched_tumors += 1
        row_bams.append((cells[ci["tumor_bamname"]], tumor_sample))
        if cells[ci["tumor_bam_exists"]] == "0" or cells[ci["normal_bam_exists"]] == "0":
            w_bams_missing.append(tumor_sample)
            if cells[ci["normal_sample"]] != DMP_REF_NA:
                w_bams_missing.append(cells[ci["normal_sample"]])
        # Warnings 4/5 mirror the reference's exact NA propagation:
        #   (!is.na(bamname) & is.na(coverage)) | (coverage < threshold)
        # The second disjunct does NOT require a non-NA bamname — a row
        # with no bamname but a present, low coverage is flagged too; a
        # row that is NA on both sides evaluates NA and is dropped.
        t_bn = cells[ci["tumor_bamname"]]
        t_cov = _ref_coverage_numeric(cells[ci["tumor_bam_coverage"]])
        if (t_bn != DMP_REF_NA and t_cov is None) or (
            t_cov is not None and t_cov < DMP_TUMOR_COVERAGE_THRESHOLD
        ):
            if tumor_sample != DMP_REF_NA:
                w_low_tumors.append(tumor_sample)
        n_bn = cells[ci["normal_bamname"]]
        n_cov_cell = cells[ci["normal_bam_coverage"]]
        # The reference's normal-side column is character-typed (spec e
        # quirk), so the threshold applies lexicographically to the
        # rendered cell.
        if (n_bn != DMP_REF_NA and n_cov_cell == DMP_REF_NA) or (
            n_cov_cell != DMP_REF_NA and n_cov_cell < DMP_NORMAL_COVERAGE_THRESHOLD
        ):
            if cells[ci["normal_sample"]] != DMP_REF_NA:
                w_low_normals.append(cells[ci["normal_sample"]])
    ambiguous_bams = {b for b, n in matched_bam_counts.items() if n > 1 and b != DMP_REF_NA}
    # The reference's file lists all rows (matched or not) whose
    # tumor_bamname is ambiguous; an NA tumor_bamname never matches
    # (dplyr's group_by drops NA groups, and NA %in% ... evaluates NA).
    w_ambiguous = [s for b, s in row_bams if b in ambiguous_bams]
    return (
        DmpSummaryRead(
            patients=len(patients),
            tn_pairs=tn_pairs,
            unmatched_tumors=unmatched_tumors,
        ),
        DmpWarningsRead(
            matched_tumors_without_normal=_dedup(w_no_normal),
            tumor_samples_with_multiple_ambiguous_normals=_dedup(w_ambiguous),
            samples_with_bams_missing=_dedup(w_bams_missing),
            low_coverage_tumors=_dedup(w_low_tumors),
            low_coverage_normals=_dedup(w_low_normals),
        ),
    )


def _open_stream_session() -> Session:
    """Own session for the export stream.

    The request-scoped session (``get_db``) may be closed before a streaming
    body has finished sending, so the generator opens and closes its own
    session. Module-level so tests can point it at the in-memory engine.
    """
    return SessionLocal()


@router.get("/export/")
def export_pair_report(
    pairing_logic_name: str = Query(..., description="Pairing logic name"),
    q: Optional[str] = Query(
        None,
        description="Case-insensitive literal substring on any of the report "
        "search fields, on either side — same filter as /pairing/report/ (T5)",
    ),
    columns: Optional[str] = Query(
        None,
        description="Comma-separated report column keys to export; defaults to "
        "the logic's default column set when omitted",
    ),
    fmt: str = Query("csv", description="csv or tsv"),
    db: Session = Depends(get_db),
):
    """Stream the FULL merged Pair Report list for a logic as CSV/TSV.

    The client holds only one page (T4), so the Download/Export button fetches
    this endpoint for the searched set instead of exporting the loaded page.
    Rows are produced in report order (latest paired rows, then — DMP logics
    only — the unmatched DMP tumor rows), in the same cell format the table
    displays. Keyset pagination keeps memory flat for large logics.

    DMP logics (4/8) export the reference's 15-column pair file: tumor rows
    only (unpaired normals never appear), real values from the remapped DMP
    record — no placeholders (spec c).
    """
    # Validate the logic, search, format, and column set up front so the
    # response is a clean 4xx rather than a broken stream.
    logic = find_logic_by_name(db, pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    if q is not None:
        q = q.strip().lower() or None
    fmt = (fmt or "").strip().lower()
    if fmt not in ("csv", "tsv"):
        raise HTTPException(status_code=400, detail="fmt must be 'csv' or 'tsv'")
    col_defs = _resolve_export_columns(logic, columns)
    is_dmp = _is_dmp_strategy(logic)
    logic_id = logic.id
    filename = "pairs_" + _sanitize_filename(pairing_logic_name) + (".csv" if fmt == "csv" else ".tsv")

    sep = "," if fmt == "csv" else "\t"
    media_type = "text/csv" if fmt == "csv" else "text/tab-separated-values"

    def gen():
        db = _open_stream_session()
        try:
            logic = db.get(PairingLogic, logic_id)
            if logic is None:
                return
            yield _csv_line([c["header"] for c in col_defs], sep)
            if is_dmp:
                # DMP pair file: each row renders as the reference's 15
                # cells (spec c) restricted to col_defs; the unmatched
                # section is tumor rows only.
                for it in (
                    _iter_paired_export_rows(db, logic, q),
                    _iter_unmatched_export_rows(db, logic, q),
                ):
                    yield from _iter_dmp_pair_lines(it, col_defs, sep)
            else:
                for row in _iter_paired_export_rows(db, logic, q):
                    yield _csv_line([c["text"](row) for c in col_defs], sep)
        finally:
            db.close()

    return StreamingResponse(
        gen(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/trigger", response_model=PairingRead)
def trigger_pairing_endpoint(
    payload: PairingTriggerCreate,
    db: Session = Depends(get_db),
) -> PairingTable:
    tumor = db.get(Sample, payload.tumor_id)
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        raise HTTPException(status_code=404, detail="Tumor sample not found")

    pairing_logic = resolve_pairing_logic(
        db,
        payload.pairing_logic_id,
        payload.pairing_logic_name,
        settings.default_pairing_logic_name,
    )
    if pairing_logic is None:
        raise HTTPException(
            status_code=400,
            detail="pairing_logic_id or pairing_logic_name is required",
        )
    if strategies.get(pairing_logic.strategy_name) is strategies["dmp"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "The DMP strategy is automatic: DMP tumors pair with their "
                "own DMP normals via POST /pairing/trigger-dmp. Pair this "
                "sample under another logic (e.g. ARGOS) instead."
            ),
        )

    pairing = trigger_pairing(
        db,
        tumor,
        pairing_logic,
        payload.trigger_type,
        payload.normal_id,
        strict=payload.strict,
    )
    if pairing is None:
        detail = "No valid pairing could be created"
        if payload.normal_id is None:
            # Re-run the (read-only) selection to explain why, with tier notes.
            selection = get_pairing_strategy(pairing_logic).select_latest_normal(db, tumor, pairing_logic)
            if selection.notes:
                detail += ": " + " | ".join(selection.notes)
        raise HTTPException(status_code=404, detail=detail)
    _invalidate_dmp_report_stats(db)
    return pairing


@router.post("/trigger-all", response_model=TriggerAllRead)
def trigger_all_pairings_endpoint(
    pairing_logic_name: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict:
    """Backfill pairings for every tumor that has no latest pairing yet.

    Occasional bulk operation (e.g. after a bulk ingest): each tumor's
    pairing logic is resolved from its assigned bait set, DMP tumors
    (paired automatically under the DMP logic) and tumors with no
    assigned or inactive logic are skipped and counted, and existing
    pairings are never touched.
    New pairings are recorded with ``trigger_type="automatic"``.

    With ``pairing_logic_name`` the backfill is scoped to that one
    non-DMP logic (counts cover that scope only); without the param the
    all-logics behavior is unchanged. DMP still runs via ``POST
    /trigger-dmp``.
    """
    scope = find_logic_by_name(db, pairing_logic_name) if pairing_logic_name else None
    with _pairing_pass(db, scope, "bulk pairing"):
        result = trigger_pairing_for_unpaired_tumors(db, logic_name=pairing_logic_name)
        _invalidate_dmp_report_stats(db)
        return result


@router.post("/trigger-dmp", response_model=DmpTriggerRead)
def trigger_dmp_pairing_endpoint(db: Session = Depends(get_db)) -> dict:
    """Run automatic DMP pairing under the DMP pairing logic.

    Every unpaired DMP tumor is paired with its DMP normal (same
    ``patient__dmp`` and gene panel) via the DMP strategy. Existing
    pairings are never modified or replaced. DMP tumors without a
    ``patient__dmp`` value cannot be matched and are reported in
    ``missing_patient__dmp``.
    """
    with _pairing_pass(db, find_logic_by_name(db, DMP_LOGIC_NAME), "DMP"):
        result = trigger_dmp_pairing(db)
        _invalidate_dmp_report_stats(db)
        return result


# ---------------------------------------------------------------------------
# HARMONY: its own pairing space + 8-column normal-oriented export
# ---------------------------------------------------------------------------

#: The HARMONY output columns — exact headers, exact order (user-confirmed).
#: One row per (normal, tumor) pair; a normal repeats across rows (1-to-many).
HARMONY_COLUMNS = (
    "normal_sample",
    "tumor_sample",
    "normal_bamname",
    "normal_bam_path",
    "tumor_bamname",
    "tumor_bam_path",
    "patient",
    "consent",
)

#: Placeholder for the bam-path columns: syzygy stores no bam paths (future
#: DMP-payload field), so the column is emitted as an em dash.
HARMONY_BAM_PATH_PLACEHOLDER = "\u2014"



def _harmony_csv_field(value) -> str:
    """Render one cell for the CSV export (nulls -> empty string)."""
    return "" if value is None else str(value)


def _harmony_csv_consent(consent) -> str:
    """CSV rendering of consent: None -> empty, False -> "false", True -> "true"."""
    if consent is None:
        return ""
    return "true" if consent is True else "false"


# Frame search semantics — the server-side replacement of the old
# client-side harmonyMatchesQuery. The query is a case-insensitive literal
# substring (the SAME _search_pattern as the Pair Report search) matched
# against the frame's searchable columns on either side. Two deliberate
# departures from what the client filter used to do: the *_bam_path columns
# are the constant em-dash placeholder, so matching them matched every row
# with any present side, and consent as a substring matched "true"/"false"
# for almost every row — both made short searches match everything. Consent
# therefore matches only when the whole query is exactly true/false.
HARMONY_SEARCH_FIELDS = ("primary_id", "anon_id", "patient__dmp")


def _harmony_side_conds(side, q: str) -> list:
    """The search conditions for one sample side (an entity or an alias)."""
    pattern = _search_pattern(q)
    conds = [getattr(side, f).ilike(pattern, escape="\\") for f in HARMONY_SEARCH_FIELDS]
    if q == "true":
        conds.append(side.consent.is_(True))
    elif q == "false":
        conds.append(side.consent.is_(False))
    return conds


def _harmony_paired_select(logic: PairingLogic, q: Optional[str]):
    """SELECT of the frame's paired section: the logic's latest pairings.

    The column labels are the UNION contract shared with
    :func:`_harmony_unmatched_select` — the frame's 8 response columns plus
    three ORDER BY keys the frame never returns: ``sort_normal``/``sort_tumor``
    are the null-free sort keys (the old Python sort's keys: normal
    ``primary_id``, then tumor ``primary_id``, absent sides first) and
    ``row_id`` breaks ties, so pages and keyset batches never overlap or skip.

    ``q`` narrows to pairings whose TUMOR or NORMAL side matches — the same
    either-side search semantics the report's paired section has.
    """
    tumor = aliased(Sample)
    normal = aliased(Sample)
    stmt = (
        select(
            normal.primary_id.label("normal_sample"),
            tumor.primary_id.label("tumor_sample"),
            normal.anon_id.label("normal_bamname"),
            cast(HARMONY_BAM_PATH_PLACEHOLDER, String).label("normal_bam_path"),
            tumor.anon_id.label("tumor_bamname"),
            cast(HARMONY_BAM_PATH_PLACEHOLDER, String).label("tumor_bam_path"),
            func.coalesce(tumor.patient__dmp, normal.patient__dmp).label("patient"),
            func.coalesce(tumor.consent, normal.consent).label("consent"),
            func.coalesce(normal.primary_id, "").label("sort_normal"),
            func.coalesce(tumor.primary_id, "").label("sort_tumor"),
            PairingTable.id.label("row_id"),
        )
        .join(tumor, PairingTable.tumor_id == tumor.id)
        .join(normal, PairingTable.normal_id == normal.id)
        .where(
            PairingTable.pairing_logic_id == logic.id,
            PairingTable.latest.is_(True),
        )
    )
    if q is not None:
        stmt = stmt.where(or_(*_harmony_side_conds(tumor, q), *_harmony_side_conds(normal, q)))
    return stmt


def _harmony_unmatched_select(logic: PairingLogic, q: Optional[str]):
    """SELECT of the frame's unmatched section: samples with no latest pairing.

    The same unmatched visibility the Python builder had, in SQL: the HARMONY
    logic is configured over DMP samples, so unmatched stays scoped to that
    cohort (``sample_type == "DMP"``), each sample a one-sided row with the
    opposite side's columns null. A sample counts as paired when it is the
    tumor OR the normal of a latest pairing for this logic — NOT EXISTS over
    the pairing table, never an ``id NOT IN <big set>``: a full pairing pass
    writes >100k rows and a 100k-element IN list makes every later call
    re-plan and re-scan it. Nothing paired yet, the same NOT EXISTS is simply
    false for every DMP sample (the freshly-initialized-database case).
    """
    already_paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    is_normal = Sample.tumor_or_normal == TumorOrNormal.NORMAL.value
    is_tumor = Sample.tumor_or_normal == TumorOrNormal.TUMOR.value
    stmt = select(
        case((is_normal, Sample.primary_id), else_=None).label("normal_sample"),
        case((is_tumor, Sample.primary_id), else_=None).label("tumor_sample"),
        case((is_normal, Sample.anon_id), else_=None).label("normal_bamname"),
        case((is_normal, cast(HARMONY_BAM_PATH_PLACEHOLDER, String)), else_=None).label(
            "normal_bam_path"
        ),
        case((is_tumor, Sample.anon_id), else_=None).label("tumor_bamname"),
        case((is_tumor, cast(HARMONY_BAM_PATH_PLACEHOLDER, String)), else_=None).label(
            "tumor_bam_path"
        ),
        Sample.patient__dmp.label("patient"),
        Sample.consent.label("consent"),
        case((is_normal, Sample.primary_id), else_=cast("", String)).label("sort_normal"),
        case((is_tumor, Sample.primary_id), else_=cast("", String)).label("sort_tumor"),
        Sample.id.label("row_id"),
    ).where(Sample.sample_type == "DMP", ~already_paired)
    if q is not None:
        stmt = stmt.where(or_(*_harmony_side_conds(Sample, q)))
    return stmt


#: The frame's response/CSV columns, exactly as the UNION labels them.
HARMONY_FRAME_COLUMNS = HARMONY_COLUMNS
#: ORDER BY keys the frame carries but never returns.
HARMONY_FRAME_ORDER = ("sort_normal", "sort_tumor", "row_id")


def _harmony_frame_subquery(logic: PairingLogic, q: Optional[str]):
    """The whole merged frame as one subquery: paired rows UNION ALL unmatched.

    UNION ALL, not UNION: the two sections are disjoint BY CONSTRUCTION (the
    unmatched section excludes paired samples) and UNION's dedup would sort
    the entire frame on every request. Merging in SQL is what makes the frame
    pageable at all — pairing rows and unmatched samples interleave by normal
    primary_id, so a page cannot be assembled from the two sections
    independently (the old code built the whole frame as a list of dicts,
    twice per request, and the browser then held all 169k rows).
    """
    return union_all(
        _harmony_paired_select(logic, q),
        _harmony_unmatched_select(logic, q),
    ).subquery("harmony_frame")


def _harmony_section_total(db: Session, section_select) -> int:
    """SQL COUNT of one frame section, never materializing it."""
    return db.scalar(select(func.count()).select_from(section_select.subquery())) or 0


def _harmony_frame_page(
    db: Session, logic: PairingLogic, q: Optional[str], offset: int, limit: int
) -> tuple[list[dict], Optional[str]]:
    """One slice of the merged frame, in frame order: ``(rows, prev_normal)``.

    LIMIT/OFFSET over the frame subquery — constant-size regardless of frame
    size. The frame row just BEFORE the page is fetched with it (one extra
    row, dropped from the rows) and its normal returned, so a page's first
    row can still be marked as the tail of a group that began on an earlier
    page.
    """
    frame = _harmony_frame_subquery(logic, q)
    stmt = select(*(frame.c[name] for name in HARMONY_FRAME_COLUMNS)).order_by(
        *(frame.c[name] for name in HARMONY_FRAME_ORDER)
    )
    fetch_offset = max(0, offset - 1)  # the boundary row, unless page 1
    # One extra row only when a boundary row exists to fetch: page 1 has none,
    # and asking for limit+1 there would hand back a page one row too big.
    fetch_limit = limit + (1 if offset > 0 else 0)
    recs = db.execute(stmt.offset(fetch_offset).limit(fetch_limit)).all()
    rows = [dict(zip(HARMONY_FRAME_COLUMNS, rec)) for rec in recs]
    prev_normal_sample = None
    if fetch_offset < offset and rows:
        prev_normal_sample = rows.pop(0)["normal_sample"]
    return rows, prev_normal_sample


def _iter_harmony_frame_rows(db: Session, logic: PairingLogic, q: Optional[str]):
    """Stream the WHOLE merged frame in frame order, via keyset pagination.

    The export path: ``(sort_normal, sort_tumor, row_id)`` replaces OFFSET, so
    a full-frame download is the same constant-size batches from the first row
    to the last, at any frame size, and never materializes the frame.
    """
    frame = _harmony_frame_subquery(logic, q)
    base = (
        select(
            *(frame.c[name] for name in HARMONY_FRAME_COLUMNS),
            *(frame.c[name] for name in HARMONY_FRAME_ORDER),
        )
        .order_by(*(frame.c[name] for name in HARMONY_FRAME_ORDER))
        .limit(EXPORT_BATCH_SIZE)
    )
    last = None  # (sort_normal, sort_tumor, row_id) of the last row yielded
    while True:
        stmt = base
        if last is not None:
            sort_normal, sort_tumor, row_id = last
            stmt = stmt.where(
                or_(
                    frame.c.sort_normal > sort_normal,
                    and_(
                        frame.c.sort_normal == sort_normal,
                        frame.c.sort_tumor > sort_tumor,
                    ),
                    and_(
                        frame.c.sort_normal == sort_normal,
                        frame.c.sort_tumor == sort_tumor,
                        frame.c.row_id > row_id,
                    ),
                )
            )
        batch = list(db.execute(stmt).all())
        if not batch:
            break
        yield from batch
        if len(batch) < EXPORT_BATCH_SIZE:
            break
        last = tuple(batch[-1][-len(HARMONY_FRAME_ORDER):])


def _iter_harmony_csv_lines(db: Session, logic: PairingLogic, q: Optional[str]):
    """Yield the HARMONY CSV as batch-sized chunks of text.

    The same dialect the single-string export used (csv.writer: exact headers,
    CRLF lines, minimal quoting), fed row by row from the keyset stream so
    memory stays O(EXPORT_BATCH_SIZE) instead of holding a 169k-row string.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HARMONY_COLUMNS)

    def drain() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return chunk

    written = 0
    for rec in _iter_harmony_frame_rows(db, logic, q):
        writer.writerow(
            [_harmony_csv_field(rec[i]) for i in range(len(HARMONY_COLUMNS) - 1)]
            + [_harmony_csv_consent(rec[len(HARMONY_COLUMNS) - 1])]
        )
        written += 1
        if written % EXPORT_BATCH_SIZE == 0:
            chunk = drain()
            if chunk:
                yield chunk
    tail = drain()
    if tail:
        yield tail


@router.get("/harmony/", response_model=HarmonyFrameRead)
def get_harmony_pairings(
    format: Optional[str] = Query(
        None, pattern="^(json|csv)$",
        description='Return "json" (default: one page of rows) or "csv" '
        '(an 8-column, normal-oriented download of the full searched frame).',
    ),
    page: int = Query(1, ge=1, description="1-based page (json only)"),
    page_size: int = Query(
        REPORT_DEFAULT_PAGE_SIZE,
        ge=1,
        description="Rows per page (json only, clamped to at most 200) like the "
        "report's — clamped, not rejected, so a 500-row page request answers "
        "instead of 422ing",
    ),
    q: Optional[str] = Query(
        None,
        description="Free-text search (case-insensitive literal substring) over "
        "the frame's searchable columns on either side — sample primary_id, bam "
        "name (anon_id), patient; consent matches only the exact words true or "
        "false. Filters the FULL frame: for json, total/matched/unmatched and "
        "every page reflect the search; for csv, the export covers the searched "
        "set",
    ),
    db: Session = Depends(get_db),
):
    """One page of the HARMONY pairing output, normal-oriented.

    The frame is the latest HARMONY-logic pairings (one row per normal/tumor
    pair) merged with the unmatched DMP samples in the HARMONY scope (no
    HARMONY pair yet), shown one-sided with the opposite side's columns null,
    ordered by normal ``primary_id`` then tumor ``primary_id`` (absent sides
    first) — the order the CSV download has always had. Merge, order, count
    and search all happen in SQL (see :func:`_harmony_frame_subquery`); the
    frame is never materialized, which is what makes a >150k-row panel load at
    all.

    Server-side pagination, same contract as the Pair Report's page envelope:
    ``rows`` is the requested ``page`` of ``page_size`` rows (clamped like the
    report's), ``total`` is the size of the FULL searched frame, and
    ``matched``/``unmatched`` are the both-sides / one-sided row counts of that
    same set — counts a paged client cannot derive from one page.
    ``prev_normal_sample`` is the normal of the frame row immediately before
    this page (null on page 1): the group-divider context a paged client would
    otherwise lose mid-group.

    ``?format=csv`` returns a ``text/csv`` attachment whose header is exactly
    the 8 HARMONY columns (``normal_sample, tumor_sample, normal_bamname,
    normal_bam_path, tumor_bamname, tumor_bam_path, patient, consent``), one
    row per (normal, tumor) pair. ``normal_bam_path``/``tumor_bam_path`` are
    em-dash placeholders (no bam paths in syzygy yet). ``consent`` renders as
    lowercase ``true``/``false`` (empty when unknown). The export covers the
    FULL searched frame, ignoring page/page_size — the download is the whole
    set, not the loaded page (same as GET /pairing/export/) — streamed in
    keyset batches on its own session.
    """
    logic = find_logic_by_name(db, HARMONY_LOGIC_NAME)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    if q is not None:
        q = q.strip().lower() or None

    if format == "csv":
        logic_id = logic.id

        def gen():
            # The export generator outlives the request's session dependency,
            # so it borrows its own pool connection (same as _stream_pairing_export).
            db2 = _open_stream_session()
            try:
                stream_logic = db2.get(PairingLogic, logic_id)
                if stream_logic is None:
                    return
                yield from _iter_harmony_csv_lines(db2, stream_logic, q)
            finally:
                db2.close()

        return StreamingResponse(
            gen(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="harmony_pairs.csv"'},
        )

    page_size = min(page_size, REPORT_MAX_PAGE_SIZE)  # same clamp as the report
    matched = _harmony_section_total(db, _harmony_paired_select(logic, q))
    unmatched = _harmony_section_total(db, _harmony_unmatched_select(logic, q))
    total = matched + unmatched
    offset = (page - 1) * page_size
    if offset >= total:
        return HarmonyFrameRead(rows=[], total=total, matched=matched, unmatched=unmatched)
    rows, prev_normal_sample = _harmony_frame_page(db, logic, q, offset, page_size)
    return HarmonyFrameRead(
        rows=rows,
        total=total,
        matched=matched,
        unmatched=unmatched,
        prev_normal_sample=prev_normal_sample,
    )


@router.post("/trigger-harmony", response_model=HarmonyTriggerRead)
def trigger_harmony_pairing_endpoint(db: Session = Depends(get_db)) -> dict:
    """Run automatic HARMONY pairing under the HARMONY pairing logic.

    HARMONY is its own pairing logic and pairing space, separate from the
    DMP logic (which may drift independently). For now it reuses the DMP
    strategy (DMP-group only, same ``patient__dmp`` and gene panel): every
    unpaired DMP tumor is paired with its best DMP normal, recorded under
    the HARMONY logic id. Existing pairings are never modified or replaced.
    DMP tumors without a ``patient__dmp`` value cannot be matched and are
    reported in ``missing_patient__dmp``.
    """
    with _pairing_pass(db, find_logic_by_name(db, HARMONY_LOGIC_NAME), "HARMONY"):
        result = trigger_harmony_pairing(db)
        _invalidate_dmp_report_stats(db)
        return result


@router.get("/unmatched-count/", response_model=UnmatchedCountRead)
def unmatched_count_endpoint(
    pairing_logic_name: str,
    db: Session = Depends(get_db),
) -> dict:
    """Per-logic unpaired count for the Pairing overview summary strip.

    DMP: mirrors ``GET /unmatched/`` (every unpaired DMP sample). Any
    other logic: the logic's unpaired tumor population — the same scope
    a backfill for that logic would consider. Read-only.
    """
    return count_unpaired(db, pairing_logic_name)


@router.get("/evaluate/", response_model=EvaluateRead)
def evaluate_pairing_endpoint(
    pairing_logic_name: str,
    db: Session = Depends(get_db),
) -> dict:
    """Dry-run: how many of this logic's unpaired tumors would pair.

    Strictly read-only — nothing is written. Over the logic's backfill
    scope, ``already_paired + would_pair + would_stay_unpaired ==
    scope_total``; ``missing_patient__dmp`` (DMP only) is a subset of
    ``would_stay_unpaired``.
    """
    return evaluate_logic(db, pairing_logic_name)
