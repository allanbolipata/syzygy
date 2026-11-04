"""Pairing endpoints.

DMP tumors pair with their own DMP normals (same ``patient__dmp`` and gene
panel) automatically via ``POST /pairing/trigger-dmp`` under the DMP
pairing logic. DMP samples are also pairable manually, but only under the
other logics (e.g. ARGOS) — ``POST /pairing/trigger`` refuses the DMP
strategy, which is reserved for the automatic trigger.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from database import get_db, settings
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
