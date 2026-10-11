from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import PairingLogic, PairingTable, Sample, utcnow
from schemas import TumorOrNormal


def resolve_pairing_logic(
    db: Session,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
) -> Optional[PairingLogic]:
    if pairing_logic_id is not None:
        logic = db.scalar(select(PairingLogic).where(PairingLogic.id == pairing_logic_id))
        if logic is not None:
            return logic

    if pairing_logic_name is not None:
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == pairing_logic_name))
        if logic is not None:
            return logic

    if pairing_logic_id is not None or pairing_logic_name is not None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")

    return None


def _matches(tumor: Sample, normal: Sample) -> bool:
    return (
        tumor.patient_id == normal.patient_id
        and tumor.sample_type == normal.sample_type
        and tumor.gene_panel == normal.gene_panel
        and tumor.bait_set == normal.bait_set
    )


def _select_latest_normal(db: Session, tumor: Sample) -> Optional[Sample]:
    stmt = select(Sample).where(
        Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
        Sample.patient_id == tumor.patient_id,
        Sample.sample_type == tumor.sample_type,
        Sample.gene_panel == tumor.gene_panel,
        Sample.bait_set == tumor.bait_set,
    ).order_by(
        Sample.sequencing_date.desc().nulls_last(),
        Sample.created_date.desc(),
        Sample.id.desc(),
    )
    return db.scalar(stmt)


def _select_matching_tumors(db: Session, normal: Sample) -> list[Sample]:
    stmt = select(Sample).where(
        Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
        Sample.patient_id == normal.patient_id,
        Sample.sample_type == normal.sample_type,
        Sample.gene_panel == normal.gene_panel,
        Sample.bait_set == normal.bait_set,
    )
    return db.scalars(stmt).all()


def _get_latest_pairing(db: Session, tumor_id: UUID, pairing_logic_id: UUID) -> Optional[PairingTable]:
    return db.scalar(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_id,
            PairingTable.pairing_logic_id == pairing_logic_id,
            PairingTable.latest.is_(True),
        )
    )


def _create_pairing(db: Session, tumor: Sample, normal: Sample, pairing_logic: PairingLogic) -> PairingTable:
    pairing = PairingTable(
        tumor_id=tumor.id,
        normal_id=normal.id,
        pairing_logic_id=pairing_logic.id,
        latest=True,
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


def trigger_pairing(
    db: Session,
    tumor: Sample,
    pairing_logic: Optional[PairingLogic],
    trigger_type: str,
    normal_id: Optional[UUID] = None,
) -> Optional[PairingTable]:
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        return None
    if pairing_logic is None:
        return None

    existing = _get_latest_pairing(db, tumor.id, pairing_logic.id)

    if trigger_type == "automatic":
        if existing is not None:
            return existing
        normal = _select_latest_normal(db, tumor)
        if normal is None:
            return None
        return _create_pairing(db, tumor, normal, pairing_logic)

    if normal_id is not None:
        normal = db.get(Sample, normal_id)
        if normal is None or normal.tumor_or_normal != TumorOrNormal.NORMAL.value:
            raise HTTPException(status_code=400, detail="Normal sample not found")
        if not _matches(tumor, normal):
            raise HTTPException(status_code=400, detail="Normal sample does not match tumor")
    else:
        normal = _select_latest_normal(db, tumor)
        if normal is None:
            return None

    if existing is not None and existing.normal_id == normal.id:
        return existing

    if existing is not None:
        existing.latest = False
        existing.modified_date = utcnow()

    return _create_pairing(db, tumor, normal, pairing_logic)


def trigger_pairing_for_normal(
    db: Session,
    normal: Sample,
    pairing_logic: Optional[PairingLogic],
) -> None:
    if normal is None or normal.tumor_or_normal != TumorOrNormal.NORMAL.value:
        return
    if pairing_logic is None:
        return

    tumors = _select_matching_tumors(db, normal)
    for tumor in tumors:
        existing = _get_latest_pairing(db, tumor.id, pairing_logic.id)
        if existing is not None:
            continue
        selected_normal = _select_latest_normal(db, tumor)
        if selected_normal is None:
            continue
        _create_pairing(db, tumor, selected_normal, pairing_logic)
