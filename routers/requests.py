from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Sample
from schemas import RequestRead


router = APIRouter(prefix="/request", tags=["requests"])


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
    """
    stmt = select(Sample).where(Sample.request_id.is_not(None))
    if q is not None:
        stmt = stmt.where(Sample.request_id.ilike(f"%{q}%"))
    samples = db.scalars(stmt).all()

    by_request: dict[str, list[Sample]] = {}
    for s in samples:
        by_request.setdefault(s.request_id, []).append(s)

    requests: list[RequestRead] = []
    for request_id in sorted(by_request):
        rows = by_request[request_id]
        tumors = [s for s in rows if s.tumor_or_normal == "TUMOR"]
        normals = [s for s in rows if s.tumor_or_normal == "NORMAL"]
        requests.append(
            RequestRead(
                request_id=request_id,
                sample_count=len(rows),
                tumor_count=len(tumors),
                normal_count=len(normals),
                patients=sorted({s.patient_id for s in rows if s.patient_id}),
                sample_types=sorted({s.sample_type for s in rows if s.sample_type}),
                gene_panels=sorted({s.gene_panel for s in rows if s.gene_panel}),
            )
        )

    if offset is not None:
        requests = requests[offset:]
    if limit is not None:
        requests = requests[:limit]
    return requests
