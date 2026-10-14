from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import PairingTable, Sample
from pairing_service import resolve_pairing_logic, trigger_pairing
from schemas import PairingRead, PairingTriggerCreate, TumorOrNormal


router = APIRouter(prefix="/pairing", tags=["pairings"])


@router.get("/", response_model=list[PairingRead])
def list_pairings(
    latest: bool = Query(False),
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

    trigger_type = "override" if payload.override or payload.trigger_type == "override" else "manual"
    pairing = trigger_pairing(db, tumor, pairing_logic, trigger_type, payload.normal_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="No valid pairing could be created")
    return pairing
