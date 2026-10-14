from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import PairingLogic, PairingTable, Sample
from pairing_service import resolve_pairing_logic, trigger_pairing
from schemas import PairingRead, PairingTriggerCreate, TumorOrNormal


router = APIRouter(prefix="/pairing", tags=["pairings"])


@router.get("/", response_model=list[PairingRead])
def list_pairings(
    latest: bool = False,
    sample_id: Optional[UUID] = None,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
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
    if pairing_logic_id is not None:
        if db.get(PairingLogic, pairing_logic_id) is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == pairing_logic_id)
    elif pairing_logic_name is not None:
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == pairing_logic_name))
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == logic.id)
    # Stable ordering so limit/offset pagination is deterministic.
    stmt = stmt.order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.post("/trigger", response_model=PairingRead)
def trigger_pairing_endpoint(payload: PairingTriggerCreate, db: Session = Depends(get_db)) -> PairingTable:
    tumor = db.get(Sample, payload.tumor_id)
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        raise HTTPException(status_code=404, detail="Tumor sample not found")

    pairing_logic = resolve_pairing_logic(db, payload.pairing_logic_id, payload.pairing_logic_name)
    if pairing_logic is None:
        raise HTTPException(status_code=400, detail="pairing_logic_id or pairing_logic_name is required")

    pairing = trigger_pairing(db, tumor, pairing_logic, payload.trigger_type, payload.normal_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="No valid pairing could be created")
    return pairing
