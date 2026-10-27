from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bait_sets import find_bait_set, resolve_bait_set_logic
from database import get_db
from models import BaitSet
from pairing_service import find_logic_by_name
from schemas import BaitSetRead, BaitSetUpsert


router = APIRouter(prefix="/bait-sets", tags=["bait-sets"])


@router.get("/", response_model=list[BaitSetRead])
def list_bait_sets(db: Session = Depends(get_db)):
    return db.scalars(select(BaitSet).order_by(func.lower(BaitSet.bait_set))).all()


@router.get("/lookup", response_model=Optional[BaitSetRead])
def lookup_bait_set(name: str, db: Session = Depends(get_db)):
    """Assigned row for a bait set; unregistered names matching a rule
    (ACCESS / HEMEPACT / IMPACT) are registered on first lookup. Returns
    null when no assignment exists."""
    resolve_bait_set_logic(db, name)
    return find_bait_set(db, name)


@router.post("/", response_model=BaitSetRead)
def upsert_bait_set(payload: BaitSetUpsert, db: Session = Depends(get_db)):
    """Create or reassign a bait set's primary pairing logic."""
    name = payload.bait_set.strip()
    if not name:
        raise HTTPException(status_code=400, detail="bait_set is required")
    logic = find_logic_by_name(db, payload.pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    row = find_bait_set(db, name)
    if row is None:
        row = BaitSet(bait_set=name, pairing_logic_id=logic.id)
        db.add(row)
    else:
        row.pairing_logic_id = logic.id
    db.commit()
    db.refresh(row)
    return row
