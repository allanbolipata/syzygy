from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import PairingLogic, PairingTable, Sample, utcnow
from bait_sets import resolve_bait_set_logic
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
        reasons = strategy.mismatch_reasons(tumor, normal, db)
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

    No-op for pooled normals: with a null patient__cmo no tumor matches, so
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


# ---------------------------------------------------------------------------
# Bulk backfill (POST /pairing/trigger-all)
# ---------------------------------------------------------------------------


def _is_dmp_sample(sample: Sample) -> bool:
    """Provenance flag for DMP-ingested samples."""
    return str(sample.sample_type or "").strip().upper() == "DMP"


def trigger_pairing_for_unpaired_tumors(db: Session) -> dict:
    """Backfill pairings for every tumor that has no latest pairing yet.

    Intended as an occasional bulk operation (POST /pairing/trigger-all),
    e.g. after a bulk ingest or to pick up tumors that were unpairable
    at ingest time. Each tumor's pairing logic is resolved from its
    assigned bait set (``bait_sets.resolve_bait_set_logic``), and:

    * DMP tumors are skipped (paired automatically under the DMP logic);
    * tumors whose bait set has no assigned logic, or whose logic is not
      active, are skipped (never paired under a wrong or disabled logic);
    * tumors that already have a latest pairing under their resolved
      logic are left untouched — ``trigger_pairing`` with ``automatic``
      short-circuits on it, so nothing existing is ever replaced.

    New pairings are recorded with ``trigger_type="automatic"``. Returns a
    summary dict: per-outcome counts (summing to ``scanned``) plus the
    newly created pairings in ``new_pairings``.
    """
    tumors = db.scalars(
        select(Sample).where(Sample.tumor_or_normal == TumorOrNormal.TUMOR.value)
    ).all()

    scanned = 0
    already_paired = 0
    paired = 0
    unpaired = 0
    skipped_dmp = 0
    skipped_no_logic = 0
    skipped_inactive_logic = 0
    new_pairings: list[PairingTable] = []

    for tumor in tumors:
        scanned += 1
        if _is_dmp_sample(tumor):
            skipped_dmp += 1
            continue
        logic = resolve_bait_set_logic(db, tumor.bait_set)
        if logic is None:
            skipped_no_logic += 1
            continue
        if not logic.active:
            skipped_inactive_logic += 1
            continue
        if _get_latest_pairing(db, tumor.id, logic.id) is not None:
            already_paired += 1
            continue
        pairing = trigger_pairing(db, tumor, logic, "automatic")
        if pairing is None:
            unpaired += 1
        else:
            paired += 1
            new_pairings.append(pairing)

    return {
        "scanned": scanned,
        "already_paired": already_paired,
        "paired": paired,
        "unpaired": unpaired,
        "skipped_dmp": skipped_dmp,
        "skipped_no_logic": skipped_no_logic,
        "skipped_inactive_logic": skipped_inactive_logic,
        "new_pairings": new_pairings,
    }


# ---------------------------------------------------------------------------
# Automatic DMP pairing (POST /pairing/trigger-dmp)
# ---------------------------------------------------------------------------

#: Name of the pairing logic under which DMP samples pair automatically.
#: The DMP strategy is strictly DMP-group scoped (DMP tumor <-> DMP normal,
#: same patient__dmp and gene panel), so running it in bulk can only create
#: DMP-DMP pairings and never touches other sample groups.
DMP_LOGIC_NAME = "DMP"


def trigger_dmp_pairing(db: Session) -> dict:
    """Bulk automatic DMP pairing under the DMP pairing logic.

    Every unpaired DMP tumor is paired with its best DMP normal (same
    ``patient__dmp`` and gene panel) via the DMP strategy. Existing pairings
    are never modified: a tumor that already has a latest pairing under the
    DMP logic is skipped, whatever that pairing's trigger type is.

    Returns ``{"pairing_logic", "scanned", "already_paired",
    "missing_patient__dmp", "paired", "unpaired", "new_pairings"}`` where
    the four outcome counts sum to ``scanned``. DMP tumors without
    ``patient__dmp`` cannot match (the strategy is patient-scoped) and are
    reported in ``missing_patient__dmp``.

    Raises ``HTTPException(400)`` when no active pairing logic named
    "DMP" exists: the trigger has nothing to run against.
    """
    logic = find_logic_by_name(db, DMP_LOGIC_NAME)
    if logic is None or not logic.active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No active pairing logic named '{DMP_LOGIC_NAME}': the DMP "
                "pairing trigger has nothing to run against."
            ),
        )

    # Tumors already carrying a latest pairing under this logic — one query
    # instead of a lookup per tumor.
    already_paired_ids = set(
        db.scalars(
            select(PairingTable.tumor_id).where(
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest.is_(True),
            )
        ).all()
    )

    tumors = db.scalars(
        select(Sample).where(
            Sample.sample_type == "DMP",
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
        )
    ).all()

    scanned = already_paired = missing_patient = paired = unpaired = 0
    new_pairings: list[PairingTable] = []
    for tumor in tumors:
        scanned += 1
        if tumor.id in already_paired_ids:
            already_paired += 1
            continue
        if not (tumor.patient__dmp or "").strip():
            missing_patient += 1
            continue
        pairing = trigger_pairing(db, tumor, logic, "automatic")
        if pairing is None:
            unpaired += 1
        else:
            paired += 1
            new_pairings.append(pairing)

    return {
        "pairing_logic": logic.name,
        "scanned": scanned,
        "already_paired": already_paired,
        "missing_patient__dmp": missing_patient,
        "paired": paired,
        "unpaired": unpaired,
        "new_pairings": new_pairings,
    }


# Default page size for the bounded ``other_matches`` slice. The strategy
# candidate scope is bounded by design, but patientless samples can still
# reach tens of thousands of candidates, so the endpoint returns a page
# by default and reports the full-scope totals alongside it.
DEFAULT_OTHER_MATCHES_LIMIT = 500
MAX_OTHER_MATCHES_LIMIT = 5000


def possible_matches(
    db: Session,
    sample: Sample,
    logic: PairingLogic,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict:
    """The sample's current matches and all potential matches.

    ``current_matches`` is the list of every latest pairing for the
    selected logic on the sample's side: a tumor has at most one, but a
    normal matches 1-to-many and can list several tumors. (``current_match``
    mirrors ``current_matches[0]`` for backward compatibility.)

    Candidates are exactly the opposite-type samples this strategy's
    cascade can reach (``strategy.candidate_scope``): its hard keys,
    compared case-insensitively, plus the strategy's extra tiers (e.g.
    argos's pooled-normal tier and same-patient DMP samples). This
    mirrors pairing, so the list stays bounded instead of returning the
    whole opposite-type table. DMP candidates (tier "dmp") under the
    argos strategy are always mismatches: DMP samples are
    manual-pairing only and are never auto-selected, so they are listed
    purely as override targets. Under the dmp strategy the candidate
    scope is DMP-group only; matching DMP candidates are ``matched`` and
    the winner carries ``would_be_selected``.

    ``other_matches`` is a bounded page of that scope (``limit``/``offset``,
    default ``DEFAULT_OTHER_MATCHES_LIMIT``); ``other_matches_total``
    counts the whole scope, and ``other_matches_matched`` /
    ``other_matches_tiers`` / ``other_matches_breakdown`` summarize it
    without depending on the page. Each candidate that is not already a
    current match is evaluated with the strategy's ``mismatch_reasons``:
    ``matched`` is true when it would pass every matching key; otherwise
    ``mismatched_keys`` lists the reasons it does not. For a tumor input,
    ``would_be_selected`` marks the candidate the strategy's cascade would
    pick. Used to support manual pairing (e.g. pinning a DMP normal via
    POST /pairing/trigger).
    """
    strategy = get_pairing_strategy(logic)
    is_tumor = sample.tumor_or_normal == TumorOrNormal.TUMOR.value

    # Current matches: every latest pairing row with this sample on its
    # side. A tumor has at most one (unique per tumor + logic); a normal
    # matches 1-to-many, so it can carry several latest rows — one per
    # distinct tumor currently paired to it.
    if is_tumor:
        row = _get_latest_pairing(db, sample.id, logic.id)
        current_rows = [row] if row is not None else []
    else:
        current_rows = list(
            db.scalars(
                select(PairingTable)
                .where(
                    PairingTable.normal_id == sample.id,
                    PairingTable.pairing_logic_id == logic.id,
                    PairingTable.latest == True,  # noqa: E712
                )
                .order_by(PairingTable.created_date.desc(), PairingTable.id.desc())
            ).all()
        )

    current_matches: list[dict] = []
    for row in current_rows:
        counterpart = (
            db.get(Sample, row.normal_id) if is_tumor else db.get(Sample, row.tumor_id)
        )
        if counterpart is None:
            continue
        current_matches.append(
            {
                "pairing_id": row.id,
                "trigger_type": row.trigger_type,
                "mismatched_keys": row.mismatched_keys,
                "notes": row.notes,
                "sample": counterpart,
            }
        )
    current_ids = {m["sample"].id for m in current_matches}

    # Candidate scope: every opposite-type sample this strategy's cascade
    # can actually reach (its hard keys, case-insensitive, plus extra
    # tiers such as argos's pooled normals) — bounded by design.
    candidates = strategy.candidate_scope(db, sample, logic)

    winner = (
        strategy.select_latest_normal(db, sample, logic).normal
        if is_tumor
        else None
    )

    tier_rank = {"same_request": 0, "other_request": 1, "dmp": 2, "pooled": 3}
    others: list[dict] = []
    for cand in candidates:
        if cand.id in current_ids:
            continue
        tumor, normal = (sample, cand) if is_tumor else (cand, sample)
        if strategy.HAS_POOLED_TIER and strategy._is_pooled(normal):
            tier = "pooled"
        elif strategy._is_dmp(cand):
            # The candidate itself is a DMP sample: under argos it is
            # the manual-pairing tier (never auto-selected); under the
            # dmp strategy it is the DMP-group cascade tier. Non-DMP
            # candidates keep their natural tier.
            tier = "dmp"
        elif normal.request_id is not None and normal.request_id == tumor.request_id:
            tier = "same_request"
        else:
            tier = "other_request"
        reasons = strategy.mismatch_reasons(tumor, normal, db)
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

    # Whole-scope summary (independent of the page) + the bounded slice.
    total = len(others)
    matched_count = sum(1 for c in others if c["matched"])
    tiers: dict[str, int] = {}
    for c in others:
        tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
    # Mismatch profile: how many candidates fail on exactly which set of
    # keys (key labels only, so e.g. "bait_set" groups regardless of which
    # bait it differs on).
    groups: dict[tuple, int] = {}
    for c in others:
        if c["matched"]:
            continue
        keys = tuple(sorted(r.split(" (")[0] for r in c["mismatched_keys"]))
        groups[keys] = groups.get(keys, 0) + 1
    breakdown = [
        {"mismatched_keys": list(keys), "count": count}
        for keys, count in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    page_limit = (
        DEFAULT_OTHER_MATCHES_LIMIT
        if limit is None
        else max(1, min(int(limit), MAX_OTHER_MATCHES_LIMIT))
    )
    page_offset = 0 if offset is None else max(0, int(offset))

    # ``current_match`` is kept for backward compatibility as the first
    # current match; ``current_matches`` is the authoritative full list.
    current_match = current_matches[0] if current_matches else None

    return {
        "sample": sample,
        "pairing_logic": logic.name,
        "current_match": current_match,
        "current_matches": current_matches,
        "other_matches": others[page_offset:page_offset + page_limit],
        "other_matches_total": total,
        "other_matches_matched": matched_count,
        "other_matches_tiers": tiers,
        "other_matches_breakdown": breakdown,
    }
