from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db, settings
from models import Sample
from pairing_service import resolve_pairing_logic, trigger_pairing, trigger_pairing_for_normal
from schemas import SampleCreate, SampleRead, TumorOrNormal


router = APIRouter(prefix="/sample", tags=["samples"])


@router.post("/", response_model=SampleRead, status_code=201)
def create_sample(payload: SampleCreate, db: Session = Depends(get_db)):
    pairing_logic = resolve_pairing_logic(
        db,
        payload.pairing_logic_id,
        payload.pairing_logic_name,
        settings.default_pairing_logic_name,
    )

    sample_data = payload.model_dump(exclude={"pairing_logic_id", "pairing_logic_name"})
    sample_data["tumor_or_normal"] = payload.tumor_or_normal.value
    sample = Sample(**sample_data)
    db.add(sample)
    db.commit()
    db.refresh(sample)

    if sample.tumor_or_normal == TumorOrNormal.TUMOR:
        trigger_pairing(db, sample, pairing_logic, "automatic")
    elif sample.tumor_or_normal == TumorOrNormal.NORMAL:
        trigger_pairing_for_normal(db, sample, pairing_logic)

    return sample


@router.get("/", response_model=list[SampleRead])
def list_samples(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    # Stable ordering so limit/offset pagination is deterministic.
    stmt = select(Sample).order_by(Sample.created_date.desc(), Sample.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()
