from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import PooledMachine
from schemas import PooledMachineRead, PooledMachineUpsert


router = APIRouter(prefix="/machines", tags=["machines"])


@router.get("/", response_model=list[PooledMachineRead])
def list_machines(db: Session = Depends(get_db)):
    # Case-insensitive stable ordering so the list is deterministic.
    stmt = select(PooledMachine).order_by(func.lower(PooledMachine.machine_name))
    return db.scalars(stmt).all()


@router.post("/", response_model=PooledMachineRead)
def upsert_machine(payload: PooledMachineUpsert, db: Session = Depends(get_db)):
    """Create or update a pooled machine row (upsert on machine_name)."""
    name = payload.machine_name.strip().lower()
    machine_class = payload.machine_class.strip().lower()
    if not name or not machine_class:
        raise HTTPException(
            status_code=400, detail="machine_name and machine_class are required"
        )
    existing = db.scalar(
        select(PooledMachine).where(func.lower(PooledMachine.machine_name) == name)
    )
    if existing is not None:
        existing.machine_class = machine_class
        existing.machine_type = payload.machine_type
        db.commit()
        db.refresh(existing)
        return existing
    machine = PooledMachine(
        machine_name=name,
        machine_class=machine_class,
        machine_type=payload.machine_type,
    )
    db.add(machine)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        machine = db.scalar(
            select(PooledMachine).where(func.lower(PooledMachine.machine_name) == name)
        )
        if machine is None:
            raise HTTPException(status_code=409, detail="Could not create machine")
    db.refresh(machine)
    return machine
