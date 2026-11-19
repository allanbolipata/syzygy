"""Bait set -> primary pairing logic assignments (local DB only).

Explicit lists take precedence, then name rules: "ACCESS" in the name ->
ACCESS; "HEMEPACT" or "IMPACT" in the name -> ARGOS (case-insensitive).
"""
from typing import Iterable, Optional

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session

from syzygy import audit
from syzygy.models import DMP_LOGIC_NAME, BaitSet, PairingLogic, Sample

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
            # AL1: bait_set.assigned (bootstrap seed — system actor;
            # ids/names only).
            audit.record(
                db,
                audit.SYSTEM_ACTOR,
                "bait_set.assigned",
                entity_type="bait_set",
                entity_id=name,
                pairing_logic=logic.name,
                payload={"bait_set": name, "pairing_logic": logic.name},
            )
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
    # AL1: bait_set.assigned (lazy registration on first sight — system
    # actor; ids/names only).
    audit.record(
        db,
        audit.SYSTEM_ACTOR,
        "bait_set.assigned",
        entity_type="bait_set",
        entity_id=name.strip(),
        pairing_logic=logic.name,
        payload={"bait_set": name.strip(), "pairing_logic": logic.name},
    )
    db.commit()
    return logic


def logic_membership_condition(logic: PairingLogic, sample=None):
    """SQL boolean condition on a sample row: the sample belongs to
    ``logic``'s family (listing-filter semantics, pure SQL — no
    per-row Python resolution).

    Membership rules (mirroring ``resolve_bait_set_logic``'s *registered*
    side, which is what a SQL-side join can express):

    - the sample's ``bait_set`` matches, case-insensitively, a
      ``bait_sets`` row whose ``pairing_logic_id`` is ``logic.id``;
    - the DMP logic additionally owns samples with ``bait_set`` NULL and
      ``sample_type`` ``'DMP'`` — DMP samples carry no bait-set key.

    ``sample`` is the mapped class (or alias) whose row the condition is
    evaluated against; it defaults to ``Sample``. Pass an ``aliased``
    class when the condition must bind to a specific join side (e.g. the
    tumor/normal aliases of a pairing listing).

    Known wrinkle (flagged per card): samples whose ``bait_set`` is NOT
    registered in ``bait_sets`` belong to no logic filter — they are only
    visible when the filter is omitted/ALL — even when ``classify_bait_set``
    would match a name rule for them. ``resolve_bait_set_logic`` registers
    such names lazily (on ingest/pairing), so imported data is normally
    registered; a never-resolved bait set stays invisible to logic filters.
    """
    if sample is None:
        sample = Sample
    registered = (
        select(BaitSet.id)
        .where(
            BaitSet.pairing_logic_id == logic.id,
            sample.bait_set.is_not(None),
            func.lower(BaitSet.bait_set) == func.lower(sample.bait_set),
        )
        .correlate(sample)
        .exists()
    )
    if logic.name.strip().upper() == DMP_LOGIC_NAME:
        return or_(
            registered,
            and_(
                sample.bait_set.is_(None),
                sample.sample_type == DMP_LOGIC_NAME,
            ),
        )
    return registered


def allowed_logics_condition(db: Session, allowed_logics: Iterable[str], sample=None):
    """SQL boolean condition on a sample row: the row belongs to ANY of
    ``allowed_logics`` — the Layer B listing filter (ticket C3).

    An OR across ``logic_membership_condition`` for each allowed logic
    (the DMP null-bait rule comes free from the helper). ``sample`` is
    passed through (see ``logic_membership_condition``).

    Fail-closed (design point 3): an empty set — or a set with no
    resolvable logic rows — yields a FALSY condition, so a principal
    with no logic entitlements sees no logic-scoped data at all.
    """
    names = {n for n in (allowed_logics or ()) if isinstance(n, str) and n}
    if not names:
        return false()
    logics = db.scalars(select(PairingLogic).where(PairingLogic.name.in_(names))).all()
    if not logics:
        return false()
    conds = [logic_membership_condition(logic, sample=sample) for logic in logics]
    return conds[0] if len(conds) == 1 else or_(*conds)
