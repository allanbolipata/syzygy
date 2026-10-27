from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from database import get_db, settings
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    find_logic_by_name,
    resolve_pairing_logic,
    trigger_pairing,
    trigger_pairing_for_unpaired_tumors,
)
from pairing_strategies import get_pairing_strategy
from schemas import PairingRead, PairingTriggerCreate, TumorOrNormal, TriggerAllRead


router = APIRouter(prefix="/pairing", tags=["pairings"])


# Sample attributes that can be filtered on either side of a pairing.
SAMPLE_FILTER_FIELDS = (
    "primary_id",
    "cmo_sample_name",
    "patient_id",
    "request_id",
    "sample_type",
    "gene_panel",
    "bait_set",
    "run_mode",
)


@router.get("/", response_model=list[PairingRead])
def list_pairings(
    latest: bool = False,
    sample_id: Optional[UUID] = None,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
    primary_id: Optional[str] = None,
    cmo_sample_name: Optional[str] = None,
    patient_id: Optional[str] = None,
    request_id: Optional[str] = None,
    sample_type: Optional[str] = None,
    gene_panel: Optional[str] = None,
    bait_set: Optional[str] = None,
    run_mode: Optional[str] = None,
    tumor_or_normal: Optional[TumorOrNormal] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
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
    # Sample attribute filters: all specified fields must match the same
    # sample, which may sit on either the tumor or the normal side.
    sample_filters = [
        (field, value)
        for field, value in (
            ("primary_id", primary_id),
            ("cmo_sample_name", cmo_sample_name),
            ("patient_id", patient_id),
            ("request_id", request_id),
            ("sample_type", sample_type),
            ("gene_panel", gene_panel),
            ("bait_set", bait_set),
            ("run_mode", run_mode),
            ("tumor_or_normal", tumor_or_normal.value if tumor_or_normal is not None else None),
        )
        if value is not None
    ]
    if sample_filters:
        tumor_alias = aliased(Sample)
        normal_alias = aliased(Sample)
        stmt = stmt.join(tumor_alias, PairingTable.tumor_id == tumor_alias.id)
        stmt = stmt.join(normal_alias, PairingTable.normal_id == normal_alias.id)
        tumor_conds = [getattr(tumor_alias, field) == value for field, value in sample_filters]
        normal_conds = [getattr(normal_alias, field) == value for field, value in sample_filters]
        stmt = stmt.where(or_(and_(*tumor_conds), and_(*normal_conds)))
    if pairing_logic_id is not None:
        if db.get(PairingLogic, pairing_logic_id) is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == pairing_logic_id)
    elif pairing_logic_name is not None:
        logic = find_logic_by_name(db, pairing_logic_name)
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
        stmt = stmt.where(PairingTable.pairing_logic_id == logic.id)
    if created_after is not None:
        stmt = stmt.where(PairingTable.created_date >= created_after)
    if created_before is not None:
        stmt = stmt.where(PairingTable.created_date <= created_before)
    # Stable ordering so limit/offset pagination is deterministic.
    stmt = stmt.order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.post("/trigger", response_model=PairingRead)
def trigger_pairing_endpoint(
    payload: PairingTriggerCreate,
    db: Session = Depends(get_db),
) -> PairingTable:
    tumor = db.get(Sample, payload.tumor_id)
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        raise HTTPException(status_code=404, detail="Tumor sample not found")

    pairing_logic = resolve_pairing_logic(
        db,
        payload.pairing_logic_id,
        payload.pairing_logic_name,
        settings.default_pairing_logic_name,
    )
    if pairing_logic is None:
        raise HTTPException(
            status_code=400,
            detail="pairing_logic_id or pairing_logic_name is required",
        )

    pairing = trigger_pairing(
        db,
        tumor,
        pairing_logic,
        payload.trigger_type,
        payload.normal_id,
        strict=payload.strict,
    )
    if pairing is None:
        detail = "No valid pairing could be created"
        if payload.normal_id is None:
            # Re-run the (read-only) selection to explain why, with tier notes.
            selection = get_pairing_strategy(pairing_logic).select_latest_normal(db, tumor, pairing_logic)
            if selection.notes:
                detail += ": " + " | ".join(selection.notes)
        raise HTTPException(status_code=404, detail=detail)
    return pairing


@router.post("/trigger-all", response_model=TriggerAllRead)
def trigger_all_pairings_endpoint(db: Session = Depends(get_db)) -> dict:
    """Backfill pairings for every tumor that has no latest pairing yet.

    Occasional bulk operation (e.g. after a bulk ingest): each tumor's
    pairing logic is resolved from its assigned bait set, DMP tumors
    (manual pairing only) and tumors with no assigned or inactive logic
    are skipped and counted, and existing pairings are never touched.
    New pairings are recorded with ``trigger_type="automatic"``.
    """
    return trigger_pairing_for_unpaired_tumors(db)
