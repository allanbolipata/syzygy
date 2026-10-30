"""Cohort sample classification (G7) — the core of the legacy validator
replacement.

Categorizes every tumor in a validated cohort against live Syzygy data:
``tumorNotFound`` / ``unpairedReason`` / ``conflictReason`` / valid-
pairable. Per sample (ids normalized before comparing — an id-space
mismatch between the request and the ``samples`` table would mean 100%
``tumorNotFound``):

* no ``Sample`` row for the tumor -> ``tumorNotFound`` finding;
* the tumor's pairing logic is resolved FIRST (bait-set based) and every
  latest-row lookup is scoped to that logic — the ``latest``
  ``PairingTable`` row is unique per (tumor, logic), so an unscoped lookup
  would misclassify a tumor paired under a different logic;
* latest row with non-empty ``mismatched_keys`` -> ``conflictReason``
  (a forced/override pair that violates matching keys);
* latest row whose normal differs from the requested normal (if one is
  present) -> ``conflictReason``; an assigned current match otherwise
  WINS — no conflict even when other eligible candidates exist;
* no latest row and no (active) pairing logic for the tumor's bait set ->
  ``unpairedReason`` (NOT retryable). DMP samples (``bait_set`` is None)
  have no pairing logic at all and are manual-pairing only, so they
  always land here and the cohort cannot pass until they are paired by
  hand;
* no latest row and 0 eligible candidates under the resolved logic ->
  ``unpairedReason`` (retryable — a later ingest may supply the normal);
* no latest row and >= 2 eligible candidates with no current match ->
  ``conflictReason`` (ambiguity);
* no latest row and exactly 1 eligible candidate -> the pair is created
  via ``pairing_service.trigger_pairing`` (automatic) and the sample is
  valid.

``classify_cohort`` returns ``(passes, findings, resolved_pairs)``:
``passes`` is all-or-nothing (``True`` iff there are no findings),
``findings`` holds ONLY problem samples, each with exactly the fields the
validator schema expects (``primaryId`` + ``cmoId`` + one of
``conflictReason`` / ``unpairedReason`` / ``tumorNotFound``; the reason
strings carry "(retryable)" / "(not retryable)" for the unpaired cases),
and ``resolved_pairs`` is the list of (tumor, normal) cmoId tuples
(both normalized) for the valid samples — T8 builds CRF rows from it.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from bait_sets import resolve_bait_set_logic
from cohort_crf import normalize_id
from models import PairingTable, Sample
from pairing_service import possible_matches, trigger_pairing
from schemas import TumorOrNormal


def _find_sample(db: Session, primary_id: Optional[str], cmo_id: Optional[str]) -> Optional[Sample]:
    """Look up a Sample by normalized ``primary_id``, falling back to the
    normalized ``cmo_sample_name``. Both request and DB sides are
    normalized before comparing so id-space differences (``s-``/``IGO-``
    prefixes, ``_`` vs ``-``) do not cause false ``tumorNotFound``."""
    if primary_id:
        row = db.scalars(select(Sample).where(Sample.primary_id == primary_id)).first()
        if row is not None:
            return row
    if cmo_id:
        row = db.scalars(
            select(Sample).where(Sample.cmo_sample_name == cmo_id)
        ).first()
        if row is not None:
            return row
    return None


def _latest_pairing(db: Session, tumor_id, logic_id) -> Optional[PairingTable]:
    """The tumor's current pairing, scoped to the resolved logic: the
    ``latest`` row is unique per (tumor, logic), so the lookup must always
    carry the logic id."""
    return db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_id,
            PairingTable.pairing_logic_id == logic_id,
            PairingTable.latest == True,  # noqa: E712
        )
    ).first()


def _finding(sample: dict, **reason: dict) -> dict:
    """A finding with exactly the validator schema's fields: the sample's
    raw (as-sent) ``primaryId``/``cmoId`` plus exactly one reason field."""
    finding = {"primaryId": sample.get("primaryId"), "cmoId": sample.get("cmoId")}
    finding.update(reason)
    return finding


def _classify_sample(db: Session, sample: dict) -> tuple[Optional[dict], Optional[tuple[str, str]]]:
    """Classify one cohort sample. Returns (finding, resolved_pair):
    ``finding`` is set for problem samples, ``resolved_pair`` is the
    normalized (tumor, normal) cmoId tuple for valid ones."""
    n_primary = normalize_id(sample.get("primaryId"))
    n_cmo = normalize_id(sample.get("cmoId"))

    tumor = _find_sample(db, n_primary, n_cmo)
    if tumor is None or tumor.tumor_or_normal != TumorOrNormal.TUMOR.value:
        return _finding(sample, tumorNotFound=True), None

    logic = resolve_bait_set_logic(db, tumor.bait_set)
    latest = _latest_pairing(db, tumor.id, logic.id) if logic is not None else None

    if latest is not None:
        if latest.mismatched_keys:
            return (
                _finding(
                    sample,
                    conflictReason=(
                        f"Current match violates matching keys: "
                        f"{'; '.join(latest.mismatched_keys)}"
                    ),
                ),
                None,
            )
        if latest.normal is None:
            return (
                _finding(sample, conflictReason="Current pairing row has no normal sample"),
                None,
            )
        requested = _find_sample(
            db,
            normalize_id(sample.get("normalPrimaryId")),
            normalize_id(sample.get("normalCmoId")),
        )
        if requested is not None and requested.id != latest.normal_id:
            return (
                _finding(
                    sample,
                    conflictReason=(
                        f"Requested normal '{sample.get('normalCmoId') or sample.get('normalPrimaryId')}' "
                        f"differs from the tumor's current match "
                        f"'{latest.normal.cmo_sample_name}'"
                    ),
                ),
                None,
            )
        # Current match wins: valid regardless of other eligible candidates.
        return None, (
            normalize_id(tumor.cmo_sample_name),
            normalize_id(latest.normal.cmo_sample_name),
        )

    if logic is None:
        if tumor.bait_set is None or not str(tumor.bait_set).strip():
            reason = (
                "DMP sample (no bait set): no pairing logic exists; "
                "manual pairing required (not retryable)"
            )
        else:
            reason = (
                f"No pairing logic assigned to bait set '{tumor.bait_set}' (not retryable)"
            )
        return _finding(sample, unpairedReason=reason), None
    if not logic.active:
        return (
            _finding(
                sample,
                unpairedReason=f"Pairing logic '{logic.name}' is not active (not retryable)",
            ),
            None,
        )

    # No latest row: count eligible candidates across the strategy's full
    # candidate scope (``other_matches_matched`` is the whole-scope count).
    matches = possible_matches(db, tumor, logic)
    eligible = matches["other_matches_matched"]
    if eligible == 0:
        return (
            _finding(
                sample,
                unpairedReason=(
                    f"No eligible normal candidates under pairing logic "
                    f"'{logic.name}' (retryable)"
                ),
            ),
            None,
        )
    if eligible >= 2:
        return (
            _finding(
                sample,
                conflictReason=(
                    f"{eligible} eligible normal candidates and no current "
                    f"match assigned (ambiguity)"
                ),
            ),
            None,
        )
    pairing = trigger_pairing(db, tumor, logic, "automatic")
    if pairing is None or pairing.normal_id is None:
        # Defensive: the cascade found nothing even though exactly one
        # eligible candidate was counted.
        return (
            _finding(
                sample,
                unpairedReason=(
                    f"No eligible normal candidates under pairing logic "
                    f"'{logic.name}' (retryable)"
                ),
            ),
            None,
        )
    normal = db.get(Sample, pairing.normal_id)
    if normal is None:
        return (
            _finding(sample, conflictReason="Created pairing row has no normal sample"),
            None,
        )
    return None, (
        normalize_id(tumor.cmo_sample_name),
        normalize_id(normal.cmo_sample_name),
    )


def classify_cohort(db: Session, cohort: dict) -> tuple[bool, list, list]:
    """Categorize every tumor in a validated cohort against live data.

    Returns ``(passes, findings, resolved_pairs)`` — see the module
    docstring for the per-branch semantics. ``passes`` is all-or-nothing.
    """
    findings: list[dict] = []
    resolved_pairs: list[tuple[str, str]] = []
    for sample in cohort.get("samples") or []:
        finding, pair = _classify_sample(db, sample)
        if finding is not None:
            findings.append(finding)
        if pair is not None:
            resolved_pairs.append(pair)
    return len(findings) == 0, findings, resolved_pairs
