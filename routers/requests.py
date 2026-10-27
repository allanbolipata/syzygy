from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from database import get_db
from models import Sample
from schemas import RequestRead


router = APIRouter(prefix="/request", tags=["requests"])


def _request_page(db: Session, limit, offset, q):
    """One row per request: id + sample/tumor/normal counts.

    Aggregation happens in SQL (GROUP BY) with limit/offset applied at the
    database level, so a paginated call never loads more than ``limit``
    requests' worth of group rows. Dialect-agnostic (Postgres + SQLite).
    """
    stmt = (
        select(
            Sample.request_id,
            func.count().label("sample_count"),
            func.sum(case((Sample.tumor_or_normal == "TUMOR", 1), else_=0)).label(
                "tumor_count"
            ),
            func.sum(case((Sample.tumor_or_normal == "NORMAL", 1), else_=0)).label(
                "normal_count"
            ),
        )
        .where(Sample.request_id.is_not(None))
        .group_by(Sample.request_id)
    )
    if q is not None:
        stmt = stmt.where(Sample.request_id.ilike(f"%{q}%"))
    stmt = stmt.order_by(Sample.request_id)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).all()


@router.get("/", response_model=list[RequestRead])
def list_requests(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Requests derived from samples (``request_id`` non-null), sorted by
    request_id. ``q`` is a case-insensitive partial match on request_id.

    ``patients`` lists the distinct patient_ids across the request's
    samples (non-null); ``sample_types`` and ``gene_panels`` are the
    distinct values carried by the request's samples.

    Counts come from a SQL GROUP BY with DB-level limit/offset; the
    distinct lists are then computed in Python over only the current
    page's samples.
    """
    rows = _request_page(db, limit, offset, q)
    if not rows:
        return []

    request_ids = [row.request_id for row in rows]
    samples = db.scalars(
        select(Sample).where(Sample.request_id.in_(request_ids))
    ).all()

    by_request: dict[str, list[Sample]] = {}
    for s in samples:
        by_request.setdefault(s.request_id, []).append(s)

    requests: list[RequestRead] = []
    for row in rows:
        rows_for_request = by_request.get(row.request_id, [])
        requests.append(
            RequestRead(
                request_id=row.request_id,
                sample_count=row.sample_count,
                tumor_count=row.tumor_count,
                normal_count=row.normal_count,
                patients=sorted({s.patient_id for s in rows_for_request if s.patient_id}),
                sample_types=sorted({s.sample_type for s in rows_for_request if s.sample_type}),
                gene_panels=sorted({s.gene_panel for s in rows_for_request if s.gene_panel}),
            )
        )
    return requests
