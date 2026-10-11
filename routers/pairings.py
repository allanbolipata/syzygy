from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import PairingTable
from schemas import PairingRead


router = APIRouter(prefix="/pairing", tags=["pairings"])


@router.get("/", response_model=list[PairingRead])
def list_pairings(
    latest: bool = Query(False),
    db: Session = Depends(get_db),
):
    stmt = select(PairingTable).options(
        joinedload(PairingTable.tumor),
        joinedload(PairingTable.normal),
    )
    if latest:
        stmt = stmt.where(PairingTable.latest.is_(True))
    return db.scalars(stmt).all()
