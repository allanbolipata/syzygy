from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import PairingLogic
from schemas import PairingLogicCreate, PairingLogicRead


router = APIRouter(prefix="/pairing-logic", tags=["pairing-logic"])


@router.get("/", response_model=list[PairingLogicRead])
def list_pairing_logic(db: Session = Depends(get_db)):
    return db.scalars(select(PairingLogic)).all()


@router.post("/", response_model=PairingLogicRead, status_code=201)
def create_pairing_logic(payload: PairingLogicCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(PairingLogic).where(PairingLogic.name == payload.name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Pairing logic already registered")

    logic = PairingLogic(name=payload.name, description=payload.description, active=payload.active)
    db.add(logic)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Pairing logic already registered")
    db.refresh(logic)
    return logic
