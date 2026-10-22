from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from database import get_db, settings
from models import Sample
from pairing_service import (
    possible_matches,
    resolve_pairing_logic,
    trigger_pairing,
    trigger_pairing_for_normal,
)
from schemas import PossibleMatchesRead, SampleCreate, SampleRead, TumorOrNormal


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
    q: Optional[str] = None,
    primary_id: Optional[str] = None,
    cmo_sample_name: Optional[str] = None,
    patient_id: Optional[str] = None,
    request_id: Optional[str] = None,
    sample_type: Optional[str] = None,
    gene_panel: Optional[str] = None,
    bait_set: Optional[str] = None,
    run_mode: Optional[str] = None,
    file_group: Optional[str] = None,
    tumor_or_normal: Optional[TumorOrNormal] = None,
    db: Session = Depends(get_db),
):
    """List samples, newest first, with optional field filters.

    ``q`` matches any of ``primary_id``, ``cmo_sample_name``,
    ``patient_id``, or ``request_id``. The per-field filters are
    case-insensitive substring matches; ``tumor_or_normal`` is exact.
    All filters combine with AND.
    """
    stmt = select(Sample).order_by(Sample.created_date.desc(), Sample.id.desc())
    if q is not None:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Sample.primary_id.ilike(like),
                Sample.cmo_sample_name.ilike(like),
                Sample.patient_id.ilike(like),
                Sample.request_id.ilike(like),
            )
        )
    for field, value in {
        "primary_id": primary_id,
        "cmo_sample_name": cmo_sample_name,
        "patient_id": patient_id,
        "request_id": request_id,
        "sample_type": sample_type,
        "gene_panel": gene_panel,
        "bait_set": bait_set,
        "run_mode": run_mode,
        "file_group": file_group,
    }.items():
        if value is not None:
            stmt = stmt.where(getattr(Sample, field).ilike(f"%{value}%"))
    if tumor_or_normal is not None:
        stmt = stmt.where(Sample.tumor_or_normal == tumor_or_normal.value)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.get("/{sample_id}/possible-matches", response_model=PossibleMatchesRead)
def possible_matches_endpoint(
    sample_id: UUID,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """The sample's current match and all potential matches.

    Candidates are every opposite-type sample the strategy's keys can
    reach (same-patient normals/tumors, plus pooled normals for
    strategies with a pooled tier). Each candidate reports its cascade
    tier, whether it passes every matching key (``matched``), and the
    reasons it does not (``mismatched_keys``). For a tumor input,
    ``would_be_selected`` marks the candidate the strategy's cascade
    would pick. Supports manual pairing (e.g. pinning a DMP normal via
    POST /pairing/trigger).
    """
    sample = db.get(Sample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    logic = resolve_pairing_logic(
        db,
        pairing_logic_id,
        pairing_logic_name,
        settings.default_pairing_logic_name,
    )
    if logic is None:
        raise HTTPException(status_code=400, detail="No active pairing logic found")
    return possible_matches(db, sample, logic)
