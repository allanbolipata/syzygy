from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import PairingLogic, PairingTable, Sample, utcnow
from pairing_strategies import get_pairing_strategy
from schemas import TumorOrNormal


def _ensure_active(logic: PairingLogic) -> None:
    if not logic.active:
        raise HTTPException(status_code=400, detail=f"Pairing logic '{logic.name}' is not active")


# Pairing-logic names that alias a canonical row. IMPACT is the assay the
# ARGOS pipeline sequences, so IMPACT data is paired by the ARGOS logic;
# CHRONOS data is paired by the TEMPO logic. An exact-name match always
# wins; the alias is only consulted when no row carries the given name.
PAIRING_LOGIC_ALIASES: dict[str, str] = {
    "IMPACT": "ARGOS",
    "CHRONOS": "TEMPO",
}


def find_logic_by_name(db: Session, name: str) -> Optional[PairingLogic]:
    """Exact pairing-logic name lookup with alias fallback."""
    logic = db.scalar(select(PairingLogic).where(PairingLogic.name == name))
    if logic is None and name in PAIRING_LOGIC_ALIASES:
        logic = db.scalar(
            select(PairingLogic).where(PairingLogic.name == PAIRING_LOGIC_ALIASES[name])
        )
    return logic


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
        logic = find_logic_by_name(db, pairing_logic_name)
        if logic is None:
            raise HTTPException(status_code=404, detail="Pairing logic not found")
    elif default_name is not None:
        logic = find_logic_by_name(db, default_name)
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


def possible_matches(db: Session, sample: Sample, logic: PairingLogic) -> dict:
    """The sample's current pairing and all potential matches.

    Candidates are every opposite-type sample the strategy's keys can
    reach: same-patient normals/tumors, plus pooled normals for
    strategies with a pooled tier (argos). Each candidate is evaluated
    with the strategy's ``mismatch_reasons``: ``matched`` is true when
    it would pass every matching key; otherwise ``mismatched_keys``
    lists the reasons it does not. For a tumor input,
    ``would_be_selected`` marks the candidate the strategy's cascade
    would pick. Used to support manual pairing (e.g. pinning a DMP
    normal via POST /pairing/trigger).
    """
    strategy = get_pairing_strategy(logic)
    is_tumor = sample.tumor_or_normal == TumorOrNormal.TUMOR.value

    # Current match: the latest pairing row with this sample on either
    # side.
    if is_tumor:
        row = _get_latest_pairing(db, sample.id, logic.id)
        counterpart = db.get(Sample, row.normal_id) if row else None
    else:
        row = db.scalar(
            select(PairingTable).where(
                PairingTable.normal_id == sample.id,
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest == True,
            )
        )
        counterpart = db.get(Sample, row.tumor_id) if row else None

    # Candidate scope: same-patient opposite-type samples, plus pooled
    # normals (pooled-tier strategies) for a tumor.
    if is_tumor:
        conds = [Sample.tumor_or_normal == TumorOrNormal.NORMAL.value]
        if sample.patient_id is not None:
            conds.append(Sample.patient_id == sample.patient_id)
        candidates = list(db.scalars(select(Sample).where(*conds)).all())
        if strategy.HAS_POOLED_TIER:
            candidates.extend(
                db.scalars(
                    select(Sample).where(
                        Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
                        Sample.patient_id.is_(None),
                        func.lower(Sample.sample_type).in_(strategy._pooled_types()),
                    )
                ).all()
            )
    else:
        if sample.patient_id is None and not strategy.HAS_POOLED_TIER:
            candidates = []
        else:
            conds = [Sample.tumor_or_normal == TumorOrNormal.TUMOR.value]
            if sample.patient_id is not None:
                conds.append(Sample.patient_id == sample.patient_id)
            candidates = list(db.scalars(select(Sample).where(*conds)).all())

    winner = (
        strategy.select_latest_normal(db, sample, logic).normal
        if is_tumor
        else None
    )

    tier_rank = {"same_request": 0, "other_request": 1, "pooled": 2}
    others: list[dict] = []
    for cand in candidates:
        if counterpart is not None and cand.id == counterpart.id:
            continue
        tumor, normal = (sample, cand) if is_tumor else (cand, sample)
        if strategy.HAS_POOLED_TIER and strategy._is_pooled(normal):
            tier = "pooled"
        elif normal.request_id is not None and normal.request_id == tumor.request_id:
            tier = "same_request"
        else:
            tier = "other_request"
        reasons = strategy.mismatch_reasons(tumor, normal)
        others.append(
            {
                "sample": cand,
                "tier": tier,
                "matched": not reasons,
                "mismatched_keys": reasons or None,
                "would_be_selected": winner is not None and cand.id == winner.id,
            }
        )
    others.sort(
        key=lambda c: (
            tier_rank[c["tier"]],
            0 if c["matched"] else 1,
            c["sample"].sequencing_date is None,
            c["sample"].sequencing_date or datetime.min,
            c["sample"].primary_id,
        )
    )

    current_match = None
    if row is not None and counterpart is not None:
        current_match = {
            "pairing_id": row.id,
            "trigger_type": row.trigger_type,
            "mismatched_keys": row.mismatched_keys,
            "notes": row.notes,
            "sample": counterpart,
        }

    return {
        "sample": sample,
        "pairing_logic": logic.name,
        "current_match": current_match,
        "other_matches": others,
    }
