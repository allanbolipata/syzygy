from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from syzygy import audit
from syzygy.auth import get_current_user, require_logic_admin
from syzygy.database import get_db, settings
from syzygy.models import PairingLogic
from syzygy.pairing_strategies import strategies
from syzygy.schemas import PairingLogicCreate, PairingLogicRead


router = APIRouter(prefix="/pairing-logic", tags=["pairing-logic"])


class PairingLogicDefault(BaseModel):
    name: Optional[str] = None


@router.get("/default", response_model=PairingLogicDefault)
def pairing_logic_default():
    """The configured default pairing-logic name (settings.default_pairing_logic_name).

    Serves the topbar View selector's first-visit preselection (card
    t_3e1444f0): the client preselects this name only when it resolves to an
    ACTIVE logic; otherwise the View starts unselected and the pairing tabs
    stay gated. Passed through verbatim — no activation check server-side.
    """
    return PairingLogicDefault(name=settings.default_pairing_logic_name)


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
def create_pairing_logic(
    payload: PairingLogicCreate,
    db: Session = Depends(get_db),
    current_user: Optional[dict] = Depends(get_current_user),
    logic_admin: None = Depends(require_logic_admin),
):
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
    db.flush()
    # AL1: logic.created — ids/names only.
    audit.record(
        db,
        audit.actor_from_user(current_user),
        "logic.created",
        entity_type="pairing_logic",
        entity_id=payload.name,
        pairing_logic=payload.name,
        payload={
            "name": payload.name,
            "strategy_name": payload.strategy_name,
            "active": payload.active,
        },
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Pairing logic already registered")
    db.refresh(logic)
    return logic
