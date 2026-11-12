from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from bait_sets import logic_membership_condition
from database import get_db
from models import PairingLogic, Sample
from pairing_service import resolve_filter_logic
from schemas import RequestRead


router = APIRouter(prefix="/request", tags=["requests"])


def _any_sample_matching(cond):
    """Group-level (HAVING) condition: at least one sample in the group
    satisfies ``cond``. Used instead of a row WHERE clause so an advanced
    filter decides *which groups* appear while every group's counts still
    cover ALL of its samples (Allan's 2026-11-11 decision: no logic
    scoping of counts)."""
    return func.sum(case((cond, 1), else_=0)) > 0


def _request_advanced_having(
    *,
    gene_panel: Optional[str],
    sample_type: Optional[str],
    bait_set: Optional[str],
    patient__cmo: Optional[str],
    logic: Optional[PairingLogic] = None,
):
    """HAVING clauses for the advanced request filters (AND-combined):
    the request matches when at least one of its samples carries a
    case-insensitive partial match on each given field."""
    having = []
    for field, value in {
        "gene_panel": gene_panel,
        "sample_type": sample_type,
        "bait_set": bait_set,
        "patient__cmo": patient__cmo,
    }.items():
        if value is not None:
            having.append(
                _any_sample_matching(getattr(Sample, field).ilike(f"%{value}%"))
            )
    if logic is not None:
        # Group-level pairing-logic filter: the request is included when
        # >=1 of its samples belongs to the logic family; the group's
        # counts still cover ALL of its samples (Allan's 2026-11-11
        # all-samples counting decision, reaffirmed 2026-11-12).
        having.append(_any_sample_matching(logic_membership_condition(logic)))
    return having


def _apply_request_filters(
    stmt,
    q,
    *,
    gene_panel: Optional[str] = None,
    sample_type: Optional[str] = None,
    bait_set: Optional[str] = None,
    patient__cmo: Optional[str] = None,
    logic: Optional[PairingLogic] = None,
):
    """Filter clauses shared by the page query and the total query, so
    the reported total always reflects the currently active filter set.

    ``q`` restricts rows (request_id is group-level: every sample of a
    request shares it, so restricting rows never shrinks a surviving
    group's counts). The advanced filters are group-level HAVING clauses
    on the unfiltered group rows — the request is included when at least
    one of its samples matches, but its aggregate counts still cover ALL
    of its samples."""
    stmt = stmt.where(Sample.request_id.is_not(None))
    if q is not None:
        stmt = stmt.where(Sample.request_id.ilike(f"%{q}%"))
    having = _request_advanced_having(
        gene_panel=gene_panel,
        sample_type=sample_type,
        bait_set=bait_set,
        patient__cmo=patient__cmo,
        logic=logic,
    )
    if having:
        stmt = stmt.having(and_(*having))
    return stmt


def _request_page(db: Session, limit, offset, *, q, **advanced):
    """One row per request: id + sample/tumor/normal counts.

    Aggregation happens in SQL (GROUP BY) with limit/offset applied at the
    database level, so a paginated call never loads more than ``limit``
    requests' worth of group rows. Dialect-agnostic (Postgres + SQLite).
    """
    stmt = _apply_request_filters(
        select(
            Sample.request_id,
            func.count().label("sample_count"),
            func.sum(case((Sample.tumor_or_normal == "TUMOR", 1), else_=0)).label(
                "tumor_count"
            ),
            func.sum(case((Sample.tumor_or_normal == "NORMAL", 1), else_=0)).label(
                "normal_count"
            ),
        ),
        q,
        **advanced,
    ).group_by(Sample.request_id).order_by(Sample.request_id)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).all()


def _request_total(db: Session, *, q, **advanced) -> int:
    """Pre-pagination number of request groups under the current filters
    (COUNT over the same filtered+HAVING GROUP BY that produces the page)."""
    groups = _apply_request_filters(select(Sample.request_id), q, **advanced).group_by(
        Sample.request_id
    )
    return db.scalar(select(func.count()).select_from(groups.subquery()))


@router.get("/", response_model=list[RequestRead])
def list_requests(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    gene_panel: Optional[str] = None,
    sample_type: Optional[str] = None,
    bait_set: Optional[str] = None,
    patient__cmo: Optional[str] = None,
    pairing_logic: Optional[str] = None,
    db: Session = Depends(get_db),
    response: Response = None,
):
    """Requests derived from samples (``request_id`` non-null), sorted by
    request_id. ``q`` is a case-insensitive partial match on request_id.

    ``patients`` lists the distinct patient__cmo values across the request's
    samples (non-null); ``sample_types`` and ``gene_panels`` are the
    distinct values carried by the request's samples.

    Advanced filters select which request groups appear; they never scope
    the groups' counts (every row's counts cover ALL of the request's
    samples): ``gene_panel``, ``sample_type``, ``bait_set`` and
    ``patient__cmo`` are case-insensitive partial matches — a request
    matches when at least one of its samples matches each given field.
    ``pairing_logic`` (e.g. ``ARGOS``/``TEMPO``/``ACCESS``/``DMP``)
    matches requests having at least one sample of that logic family
    (sample membership = registered bait_sets assignment; the DMP logic
    also owns bait_set-NULL ``sample_type`` ``'DMP'`` samples). Omitted /
    blank / ``ALL`` = no restriction; unknown name -> 400.
    All filters combine with AND.

    Counts come from a SQL GROUP BY with DB-level limit/offset; the
    distinct lists are then computed in Python over only the current
    page's samples.

    The ``X-Total-Count`` response header reports the total number of
    request groups matching the current filters, independent of
    ``limit``/``offset`` (pre-pagination). ``response`` defaults to
    ``None`` so direct-call tests (repo convention) can pass a plain
    ``Response()`` to capture the header.
    """
    advanced = {
        "gene_panel": gene_panel,
        "sample_type": sample_type,
        "bait_set": bait_set,
        "patient__cmo": patient__cmo,
        "logic": resolve_filter_logic(db, pairing_logic),
    }
    rows = _request_page(db, limit, offset, q=q, **advanced)

    by_request: dict[str, list[Sample]] = {}
    if rows:
        request_ids = [row.request_id for row in rows]
        samples = db.scalars(
            select(Sample).where(Sample.request_id.in_(request_ids))
        ).all()
        for s in samples:
            by_request.setdefault(s.request_id, []).append(s)

    # Totals issued last: a bounded scalar COUNT (exactly 1 row) over the
    # same filtered GROUP BY — never the full group set.
    total = _request_total(db, q=q, **advanced)
    if response is not None:
        response.headers["X-Total-Count"] = str(total)

    if not rows:
        return []

    requests: list[RequestRead] = []
    for row in rows:
        rows_for_request = by_request.get(row.request_id, [])
        requests.append(
            RequestRead(
                request_id=row.request_id,
                sample_count=row.sample_count,
                tumor_count=row.tumor_count,
                normal_count=row.normal_count,
                patients=sorted({s.patient__cmo for s in rows_for_request if s.patient__cmo}),
                sample_types=sorted({s.sample_type for s in rows_for_request if s.sample_type}),
                gene_panels=sorted({s.gene_panel for s in rows_for_request if s.gene_panel}),
            )
        )
    return requests
