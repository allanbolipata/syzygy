"""Bait set -> primary pairing logic assignments (local DB only).

Explicit lists take precedence, then name rules: "ACCESS" in the name ->
ACCESS; "HEMEPACT" or "IMPACT" in the name -> ARGOS (case-insensitive).
"""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import BaitSet, PairingLogic

TEMPO_BAIT_SETS = (
    "Agilent_v4_51MB_Human_hg19_BAITS",
    "IDT_Exome_v1_FP_b37_baits",
    "IDT_Exome_v1_FP_BAITS",
    "IDT_Exome_v2_FP_b37_baits",
    "IDT_Exome_v2_GRCh38_BAITS",
    "SureSelect-All-Exon-V4-hg19",
    "IDT_Exome_v2_BAITS",
    "IDT_Exome_v2_FP_BAITS",
)
ACCESS_BAIT_SETS = (
    "MSK-ACCESS-v2_0-probesAllwFP",
    "MSK-ACCESS-v1_0-probesAllwFP",
    "CMO-CH",
)
SEED_BAIT_SETS: tuple = tuple((b, "TEMPO") for b in TEMPO_BAIT_SETS) + tuple(
    (b, "ACCESS") for b in ACCESS_BAIT_SETS
)


def classify_bait_set(name: str) -> Optional[str]:
    """Pairing logic name for a bait set, or None if no rule matches."""
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    for bait, logic in SEED_BAIT_SETS:
        if bait.lower() == low:
            return logic
    if "access" in low:
        return "ACCESS"
    if "hemepact" in low or "impact" in low:
        return "ARGOS"
    return None


def find_bait_set(db: Session, name: str) -> Optional[BaitSet]:
    return db.scalar(
        select(BaitSet).where(func.lower(BaitSet.bait_set) == name.strip().lower())
    )


def seed_bait_sets(db: Session) -> None:
    """Idempotently insert SEED_BAIT_SETS (existing rows left untouched)."""
    for name, logic_name in SEED_BAIT_SETS:
        if find_bait_set(db, name) is not None:
            continue
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == logic_name))
        if logic is not None:
            db.add(BaitSet(bait_set=name, pairing_logic_id=logic.id))
    db.commit()


def resolve_bait_set_logic(db: Session, name: Optional[str]) -> Optional[PairingLogic]:
    """Assigned logic for a bait set. Unregistered names matching a rule
    are registered on first sight; unmatched names return None."""
    if not name or not name.strip():
        return None
    row = find_bait_set(db, name)
    if row is not None:
        return row.pairing_logic_obj
    logic_name = classify_bait_set(name)
    if logic_name is None:
        return None
    logic = db.scalar(select(PairingLogic).where(PairingLogic.name == logic_name))
    if logic is None:
        return None
    db.add(BaitSet(bait_set=name.strip(), pairing_logic_id=logic.id))
    db.commit()
    return logic
