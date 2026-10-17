from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import PairingLogic, PairingTable, Sample, utcnow
from pairing_strategies import get_pairing_strategy
from schemas import TumorOrNormal


def _ensure_active(logic: PairingLogic) -> None:
    if not logic.active:
        raise HTTPException(status_code=400, detail=f"Pairing logic '{logic.name}' is not active")


def resolve_pairing_logic(
    db: Session,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
    default_name: Optional[str] = None,
) -> Optional[PairingLogic]:
    logic: Optional[PairingLogic] = None
    if pairing_logic_id is not None:
        logic = db.get(PairingLogic, pairing_logic_id)
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
    elif pairing_logic_name is not None:
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == pairing_logic_name))
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
    elif default_name is not None:
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == default_name))
        if logic is None or not logic.active:
            return None

    if logic is not None:
        _ensure_active(logic)
    return logic


def _get_latest_pairing(
    db: Session, tumor_id: UUID, pairing_logic_id: UUID
) -> Optional[PairingTable]:
    return db.scalar(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_id,
            PairingTable.pairing_logic_id == pairing_logic_id,
            PairingTable.latest == True,
        )
    )


def _create_pairing(
    db: Session,
    tumor: Sample,
    normal: Sample,
    pairing_logic: PairingLogic,
    trigger_type: Optional[str] = None,
    mismatched_keys: Optional[list] = None,
    notes: Optional[list] = None,
) -> PairingTable:
    pairing = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=pairing_logic.id,
        latest=True,
        trigger_type=trigger_type,
        mismatched_keys=mismatched_keys,
        notes=notes,
    )
    db.add(pairing)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _get_latest_pairing(db, tumor.id, pairing_logic.id)
        if existing is not None:
            return existing
        raise
    db.refresh(pairing)
    return pairing


def _mark_stale(existing: PairingTable) -> None:
    existing.latest = False
    existing.modified_date = utcnow()


def trigger_pairing(
    db: Session,
    tumor: Sample,
    pairing_logic: Optional[PairingLogic],
    trigger_type: str,
    normal_id: Optional[UUID] = None,
    strict: bool = False,
) -> Optional[PairingTable]:
    """Create (or return) the current pairing for a tumor.

    * ``normal_id`` given — override semantics: the pinned normal is paired
      even when matching keys differ, and the mismatches are recorded in
      the pairing's ``mismatched_keys``. With ``strict=True`` any mismatch
      is rejected with 400 instead.
    * ``automatic`` without ``normal_id`` — return the existing pairing if
      one exists, otherwise run the strategy cascade.
    * ``manual`` without ``normal_id`` — always re-run the strategy
      cascade; a different winner replaces the current pairing.

    Returns None when the cascade finds no matching normal.
    """
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        return None
    if pairing_logic is None:
        return None

    strategy = get_pairing_strategy(pairing_logic)
    existing = _get_latest_pairing(db, tumor.id, pairing_logic.id)

    if normal_id is not None:
        normal = db.get(Sample, normal_id)
        if normal is None or normal.tumor_or_normal != TumorOrNormal.NORMAL.value:
            raise HTTPException(status_code=404, detail="Normal sample not found")
        reasons = strategy.mismatch_reasons(tumor, normal)
        if strict and reasons:
            raise HTTPException(
                status_code=400,
                detail="Normal sample does not match tumor: " + "; ".join(reasons),
            )
        if existing is not None and existing.normal_id == normal.id:
            return existing
        if existing is not None:
            _mark_stale(existing)
        result = _create_pairing(
            db,
            tumor,
            normal,
            pairing_logic,
            trigger_type=trigger_type,
            mismatched_keys=reasons or None,
        )
        # Race guard: a concurrent commit may have won the "latest" slot.
        if result.normal_id != normal.id:
            raise HTTPException(
                status_code=409,
                detail="Pairing conflict: a different pairing was created concurrently",
            )
        return result

    if trigger_type == "automatic" and existing is not None:
        return existing

    selection = strategy.select_latest_normal(db, tumor, pairing_logic)
    if selection.normal is None:
        return None
    if existing is not None and existing.normal_id == selection.normal.id:
        return existing
    if existing is not None:
        _mark_stale(existing)
    return _create_pairing(
        db,
        tumor,
        selection.normal,
        pairing_logic,
        trigger_type=trigger_type,
        notes=selection.notes or None,
    )


def trigger_pairing_for_normal(
    db: Session,
    normal: Sample,
    pairing_logic: Optional[PairingLogic],
) -> None:
    """Auto-pair a normal sample against tumors that have no pairing yet.

    No-op for pooled normals: with a null patient_id no tumor matches, so
    pooled normals enter pairing only when a tumor's cascade reaches the
    pooled tier.
    """
    if normal is None or normal.tumor_or_normal != TumorOrNormal.NORMAL.value:
        return
    if pairing_logic is None:
        return

    strategy = get_pairing_strategy(pairing_logic)

    tumors = strategy.select_matching_tumors(db, normal, pairing_logic)
    for tumor in tumors:
        existing = _get_latest_pairing(db, tumor.id, pairing_logic.id)
        if existing is not None:
            continue
        selection = strategy.select_latest_normal(db, tumor, pairing_logic)
        if selection.normal is None:
            continue
        _create_pairing(
            db,
            tumor,
            selection.normal,
            pairing_logic,
            trigger_type="automatic",
            notes=selection.notes or None,
        )
