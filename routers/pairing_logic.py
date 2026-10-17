from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import PairingLogic
from pairing_strategies import strategies
from schemas import PairingLogicCreate, PairingLogicRead


router = APIRouter(prefix="/pairing-logic", tags=["pairing-logic"])


@router.get("/", response_model=list[PairingLogicRead])
def list_pairing_logic(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    # Stable ordering so limit/offset pagination is deterministic.
    stmt = select(PairingLogic).order_by(PairingLogic.created_date.desc(), PairingLogic.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.post("/", response_model=PairingLogicRead, status_code=201)
def create_pairing_logic(payload: PairingLogicCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(PairingLogic).where(PairingLogic.name == payload.name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Pairing logic already registered")

    if payload.strategy_name not in strategies:
        raise HTTPException(status_code=400, detail="Unknown pairing strategy")

    logic = PairingLogic(
        name=payload.name,
        description=payload.description,
        active=payload.active,
        strategy_name=payload.strategy_name,
        config=payload.config,
    )
    db.add(logic)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Pairing logic already registered")
    db.refresh(logic)
    return logic
