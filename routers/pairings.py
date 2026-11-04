"""Pairing endpoints.

DMP tumors pair with their own DMP normals (same ``patient__dmp`` and gene
panel) automatically via ``POST /pairing/trigger-dmp`` under the DMP
pairing logic. DMP samples are also pairable manually, but only under the
other logics (e.g. ARGOS) — ``POST /pairing/trigger`` refuses the DMP
strategy, which is reserved for the automatic trigger.
"""
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from database import SessionLocal, get_db, settings
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    _is_dmp_strategy,
    count_unpaired,
    evaluate_logic,
    find_logic_by_name,
    resolve_pairing_logic,
    trigger_dmp_pairing,
    trigger_pairing,
    trigger_pairing_for_unpaired_tumors,
)
from pairing_strategies import get_pairing_strategy, strategies
from schemas import (
    DmpTriggerRead,
    EvaluateRead,
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
    """One slice of the unmatched DMP rows, in report order.

    Same predicate and ordering as ``GET /pairing/unmatched/``: DMP samples
    with no latest pairing under the logic, ``created_date desc, id desc``.
    With ``q``, restricted to samples any report search field matches —
    an unmatched row carries the sample on one side only (the other side
    is null), so the "either side" search reduces to its own fields.
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
        .where(Sample.sample_type == "DMP", ~paired)
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
    """SQL COUNT of the unmatched DMP rows for the logic.

    Uses the exact ``~paired`` predicate that ``_report_unmatched_page``
    serves rows with, so the count equals the number of rows the endpoint
    would actually return (DMP samples with no latest pairing under the
    logic). Unlike ``count_unpaired``'s ``unpaired``, this never falls back
    to a total-minus-paired estimate, so ``total`` stays exact. With ``q``,
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
    stmt = select(func.count(Sample.id)).where(Sample.sample_type == "DMP", ~paired)
    if q is not None:
        stmt = stmt.where(or_(*_search_conds(Sample, q)))
    return db.scalar(stmt) or 0


def _paired_row(p: PairingTable) -> PairingReportRowRead:
    """Map a latest pairing row to a report row (both sides present)."""
    return PairingReportRowRead.model_validate(p)


def _unmatched_row(s: Sample, logic: PairingLogic) -> PairingReportRowRead:
    """Map an unmatched DMP sample to a report row.

    Mirrors the frontend ``appendUnmatchedDmpRows``: the sample goes on its
    natural side (``tumor`` if TUMOR, else ``normal``), the other side is
    null, ``mismatched_keys`` is an empty list, and no pairing identity is
    set.
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
):
    """One page of the merged Pair Report list for the logic.

    Returns ``{"rows": [...], "total": int}``. The merged list is the
    logic's latest paired rows (both sides present), followed by — DMP
    logics only — the unmatched DMP samples rendered as rows with the sample
    on its natural side (``tumor`` if TUMOR, otherwise ``normal``) and the
    other side null. ``total`` is the count of the FULL merged list, computed
    with a SQL COUNT (never materializing the whole unmatched set in memory),
    so the client can compute the page count.

    With ``q`` (free-text search), both sections are restricted to rows the
    query matches on either side — the server-side form of the former
    client-side ``pairMatchesQuery`` — and ``total`` becomes the count of the
    filtered list, so pages and counts reflect the search.

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

    return PairingReportRead(rows=rows, total=total)


# ---------------------------------------------------------------------------
# Pair Report server-side download/export (T6).
#
# The Pair Report table holds only one server page (T4), so the old
# client-side "download the loaded set" only exported the visible page.
# The export is computed server-side and STREAMED: the merged list (latest
# paired rows, then — DMP logics only — the unmatched DMP rows) is fetched
# in keyset batches (created_date desc, id desc), never materialized whole,
# and rendered to CSV/TSV one line at a time. Cell formatting mirrors the
# frontend table renderers (pairText / pairDate / dmpMetaText) so the file
# matches what the user sees.
# ---------------------------------------------------------------------------

# Rows fetched per SQL round-trip while streaming the export. Keyset
# pagination (not OFFSET) means this stays O(1) in memory regardless of the
# logic's row count (DMP: ~284k samples / ~111k pairs).
EXPORT_BATCH_SIZE = 500

# DMP columns whose value is not derivable from the imported sample — the
# frontend renders these with this placeholder (app.js DMP_PLACEHOLDER).
DMP_PLACEHOLDER = "—"


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


def _dmp_name_parts(primary_id: str) -> list:
    """Mirror the frontend ``dmpNameParts``: split a DMP primary ID on "-";
    return [second-to-last, last] (["",""] if fewer than two parts)."""
    parts = (primary_id or "").split("-")
    if len(parts) < 2:
        return ["", ""]
    return [parts[-2], parts[-1]]


def _dmp_meta(sample, key: str) -> str:
    """Mirror the frontend ``dmpMetaText``: read additional_metadata[key];
    null sample / non-dict metadata / missing key -> ""."""
    if sample is None:
        return ""
    meta = sample.additional_metadata
    if not isinstance(meta, dict):
        meta = {}
    v = meta.get(key)
    if v is None:
        return ""
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

# Python mirror of app.js DMP_COLUMNS: the DMP logic's report column model
# (legacy beagle DMP filemetadata layout), same order and text renderers.
DMP_EXPORT_COLUMNS = [
    {"key": "tumor_sample", "header": "tumor_sample", "text": lambda r: (r.tumor.primary_id or "") if r.tumor is not None else ""},
    {"key": "patient", "header": "patient", "text": lambda r: (r.tumor.patient__dmp or "") if r.tumor is not None else ""},
    {"key": "tumor_idx", "header": "tumor_idx", "text": lambda r: _dmp_name_parts(r.tumor.primary_id)[0] if r.tumor is not None else ""},
    {"key": "tumor_IM_ver", "header": "tumor_IM_ver", "text": lambda r: _dmp_name_parts(r.tumor.primary_id)[1] if r.tumor is not None else ""},
    {"key": "tumor_bamname", "header": "tumor_bamname", "text": lambda r: (r.tumor.anon_id or "") if r.tumor is not None else ""},
    {"key": "tumor_group", "header": "tumor_group", "text": lambda r: DMP_PLACEHOLDER},
    {"key": "tumor_matched", "header": "tumor_matched", "text": lambda r: ("Matched" if r.normal is not None else "Unmatched") if r.tumor is not None else ""},
    {"key": "tumor_bam_exists", "header": "tumor_bam_exists", "text": lambda r: DMP_PLACEHOLDER},
    {"key": "tumor_bam_coverage", "header": "tumor_bam_coverage", "text": lambda r: _dmp_meta(r.tumor, "coverage")},
    {"key": "normal_sample", "header": "normal_sample", "text": lambda r: (r.normal.primary_id or "") if r.normal is not None else ""},
    {"key": "normal_bamname", "header": "normal_bamname", "text": lambda r: (r.normal.anon_id or "") if r.normal is not None else ""},
    {"key": "normal_IM_ver", "header": "normal_IM_ver", "text": lambda r: _dmp_name_parts(r.normal.primary_id)[1] if r.normal is not None else ""},
    {"key": "pool_id", "header": "pool_id", "text": lambda r: _dmp_meta(r.tumor, "project_name")},
    {"key": "normal_bam_exists", "header": "normal_bam_exists", "text": lambda r: DMP_PLACEHOLDER},
    {"key": "normal_bam_coverage", "header": "normal_bam_coverage", "text": lambda r: _dmp_meta(r.normal, "coverage")},
]

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
    blank. Unknown keys are a 400."""
    keys = None
    if columns is not None:
        keys = [k.strip() for k in columns.split(",") if k.strip()]
    if not keys:
        keys = DMP_DEFAULT_EXPORT_KEYS if _is_dmp_strategy(logic) else PAIR_DEFAULT_EXPORT_KEYS
    defs = []
    for k in keys:
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
    """Stream every unmatched DMP row for the logic in report order via
    keyset pagination. Same predicate as _report_unmatched_page (DMP samples
    with no latest pairing under the logic, plus the q search)."""
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
    base = [Sample.sample_type == "DMP", ~paired]
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


def _sanitize_filename(name: str) -> str:
    """Mirror the frontend filename sanitize (/[^\\w.-]+/g -> "_")."""
    import re
    return re.sub(r"[^\w.-]+", "_", name or "")


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
    only — the unmatched DMP rows with the tumor/CMO columns empty), in the
    same cell format the table displays. Keyset pagination keeps memory flat
    for large logics.
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
            for row in _iter_paired_export_rows(db, logic, q):
                yield _csv_line([c["text"](row) for c in col_defs], sep)
            if is_dmp:
                for row in _iter_unmatched_export_rows(db, logic, q):
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
    return trigger_pairing_for_unpaired_tumors(db, logic_name=pairing_logic_name)


@router.post("/trigger-dmp", response_model=DmpTriggerRead)
def trigger_dmp_pairing_endpoint(db: Session = Depends(get_db)) -> dict:
    """Run automatic DMP pairing under the DMP pairing logic.

    Every unpaired DMP tumor is paired with its DMP normal (same
    ``patient__dmp`` and gene panel) via the DMP strategy. Existing
    pairings are never modified or replaced. DMP tumors without a
    ``patient__dmp`` value cannot be matched and are reported in
    ``missing_patient__dmp``.
    """
    return trigger_dmp_pairing(db)


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
