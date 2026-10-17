"""Pluggable sample pairing strategies.

A strategy decides which normal sample pairs with a tumor. Strategies are
registered under a name and referenced from a PairingLogic row via
``strategy_name``, so per-logic behavior can differ without new endpoints.
A logic's ``config`` dict customizes the strategy at use time (see
``get_pairing_strategy``).

Registered strategies:
  * ``default`` — patient-scoped matching on (patient_id, sample_type,
    gene_panel, bait_set) with a request-first cascade: normals ingested
    under the same ``request_id`` are preferred, then all other normals
    for the patient.
  * ``argos`` — WES strategy aligned with beagle's argos pairing logic
    (v2_3.0). Matches on (patient_id, gene_panel, bait_set) with
    run-mode (sequencer) bucket matching, a request-first cascade, and a
    pooled normal tier (shared across patients). Beagle's v2_3.0 logic
    does not key on specimen/sample type and does not gate on
    preservation, so neither is a matching key here (both are still
    stored on the sample).
  * ``tempo`` — TEMPO_MPGEN / CHRONOS strategy aligned with beagle's
    tempo_patient.py: a historical-pairing tier (beagle's pre_pairing /
    pairing.tsv) retained unconditionally, then patient-wide matching on
    bait_set (case-insensitive) and run-mode bucket. Gene panel and
    sample_type are not keys; there is no request cascade or pooled tier.
  * ``IMPACT`` / ``ACCESS`` — aliases for ``default``.
  * ``TEMPO`` / ``CHRONOS`` — aliases for ``tempo``.

Every selection returns a ``NormalSelection`` whose ``notes`` record which
tiers were consulted and why candidates were skipped; the service stores
them on the pairing row and surfaces them in API errors.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol, Sequence

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import PairingLogic, PairingTable, Sample
from schemas import TumorOrNormal

# Sentinel used to sort rows without a sequencing_date to the end. It is
# never compared against a real (timezone-aware) timestamp: tuple
# comparison short-circuits on the first element when a date is missing.
_NULL_DATE = datetime.min

# Default mapping from raw run-mode / sequencer substrings to buckets,
# checked in order (most specific first). Unknown non-empty values map to
# themselves, so they only match an identical unknown value. Configurable
# per pairing logic via the ``run_mode_map`` config key (same shape).
DEFAULT_RUN_MODE_MAP: Dict[str, str] = {
    "novaseq x": "novaseq_x",
    "novaseq": "novaseq",
    "hiseq": "hiseq",
}

# beagle's TEMPO/CHRONOS run-mode buckets (tempo_sample._map_run_modes):
# any value containing "novaseq" -> NovaSeq, containing "hiseq" -> HiSeq,
# otherwise the raw value; compared case-insensitively. Note the simpler
# bucketing than DEFAULT_RUN_MODE_MAP: "NovaSeq X 1000" and "NovaSeq 6000"
# both land in "novaseq" here.
TEMPO_RUN_MODE_MAP: Dict[str, str] = {
    "novaseq": "novaseq",
    "hiseq": "hiseq",
}

# sample_type values that mark a shared (pooled) normal. Pooled normals
# belong to no patient (patient_id is null) and are offered as a last
# resort by the argos strategy. Configurable per pairing logic via the
# ``pooled_sample_types`` config key.
DEFAULT_POOLED_SAMPLE_TYPES: tuple = ("Pooled Normal",)


def _as_text(value) -> str:
    """Normalize a possibly list-valued metadata field to a single string.

    preservation / sample_origin are JSON columns that may hold a list
    (mirroring beagle's list-valued metadata) or a scalar; lists use
    their first entry.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip().lower()


def normalize_run_mode(value: Optional[str], mapping: Optional[Dict[str, str]] = None) -> str:
    """Map a raw run-mode string (e.g. "NovaSeq X 1000") to a bucket.

    Empty/None map to "". Known substrings map to their bucket; unknown
    non-empty values map to themselves (lower-cased, stripped).
    """
    if not value:
        return ""
    v = str(value).strip().lower()
    for key, bucket in (mapping or DEFAULT_RUN_MODE_MAP).items():
        if key in v:
            return bucket
    return v


def resolve_preservation(preservation, sample_origin) -> str:
    """Resolve (preservation, sample_origin) to "ffpe", "frozen", or "".

    Ported from beagle's get_preservation_type (argos_operator v2_2_0):
    both sides agree on FFPE -> "ffpe"; both consistent with fresh/frozen
    -> "frozen"; anything else (missing or unrecognizable included) -> ""
    (unresolvable).
    """
    p = _as_text(preservation)
    o = _as_text(sample_origin)
    if p and o:
        if p == "ffpe" and o == "ffpe":
            return "ffpe"
        if "fresh or frozen" in o and p != "ffpe":
            return "frozen"
        return ""
    if p:
        return "ffpe" if p == "ffpe" else "frozen"
    if o:
        if o == "ffpe":
            return "ffpe"
        if "fresh or frozen" in o:
            return "frozen"
        return ""
    return ""


@dataclass
class NormalSelection:
    """Result of a strategy's normal selection.

    ``normal`` is the chosen normal (None when no candidate qualified);
    ``notes`` carries human-readable tier notes, including why candidates
    were skipped.
    """

    normal: Optional[Sample] = None
    notes: List[str] = field(default_factory=list)


class PairingStrategy(Protocol):
    def matches(self, tumor: Sample, normal: Sample) -> bool:
        ...

    def mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        ...

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        ...

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        ...

    def with_config(self, config: dict) -> "PairingStrategy":
        ...


strategies: Dict[str, PairingStrategy] = {}


def register_pairing_strategy(name: str):
    def decorator(cls):
        strategies[name] = cls()
        return cls
    return decorator


@register_pairing_strategy("default")
class DefaultPairingStrategy:
    """Reference pairing strategy (used by IMPACT, ACCESS).

    Base keys: see ``HARD_KEYS`` (patient_id, sample_type, gene_panel,
    bait_set). Cascade:
    prefer normals ingested under the same request_id (when the tumor has
    one), then all other normals for the patient.
    """

    # Sample attributes that must match on both sides of a pairing.
    # Overridden by strategies that align with other operators (argos).
    HARD_KEYS: tuple = ("patient_id", "sample_type", "gene_panel", "bait_set")

    def __init__(self, config: Optional[dict] = None):
        self.config = dict(config or {})

    def with_config(self, config: dict) -> "PairingStrategy":
        return type(self)(config=dict(config or {}))

    # -- matching -------------------------------------------------------

    def _base_mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        reasons: List[str] = []
        for label in self.HARD_KEYS:
            t_val = getattr(tumor, label)
            n_val = getattr(normal, label)
            if t_val != n_val:
                reasons.append(f"{label} mismatch ({t_val} vs {n_val})")
        return reasons

    def mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        return self._base_mismatch_reasons(tumor, normal)

    def matches(self, tumor: Sample, normal: Sample) -> bool:
        return not self.mismatch_reasons(tumor, normal)

    # -- candidate selection ---------------------------------------------

    def _candidate_stmt(
        self,
        tumor: Sample,
        request_id: Optional[str] = None,
        exclude_request_id: Optional[str] = None,
    ):
        conds = [Sample.tumor_or_normal == TumorOrNormal.NORMAL.value]
        conds.extend(
            getattr(Sample, key) == getattr(tumor, key) for key in self.HARD_KEYS
        )
        stmt = select(Sample).where(*conds)
        if request_id is not None:
            stmt = stmt.where(Sample.request_id == request_id)
        if exclude_request_id is not None:
            stmt = stmt.where(Sample.request_id != exclude_request_id)
        return stmt

    def _soft_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        """Extra gates beyond the base keys. Overridden by argos."""
        return []

    @staticmethod
    def _latest(candidates: List[Sample]) -> Sample:
        return max(
            candidates,
            key=lambda s: (
                s.sequencing_date is not None,
                s.sequencing_date or _NULL_DATE,
                s.created_date,
                s.id,
            ),
        )

    def _select_in_candidates(
        self,
        tumor: Sample,
        candidates: Sequence[Sample],
        tier_label: str,
        reasons_fn,
    ):
        """Pick the latest candidate passing ``reasons_fn``; note the rest."""
        if not candidates:
            return None, [f"tier={tier_label}: no candidate normals"]
        ok: List[Sample] = []
        failures: Dict[str, int] = {}
        for candidate in candidates:
            reasons = reasons_fn(tumor, candidate)
            if reasons:
                for reason in reasons:
                    failures[reason] = failures.get(reason, 0) + 1
            else:
                ok.append(candidate)
        if ok:
            note = f"tier={tier_label}: {len(candidates)} candidate(s), {len(ok)} matched"
            if failures:
                note += "; " + ", ".join(f"{n} skipped ({r})" for r, n in sorted(failures.items()))
            return self._latest(ok), [note]
        note = f"tier={tier_label}: {len(candidates)} candidate(s), 0 matched"
        if failures:
            note += "; " + ", ".join(f"{n} skipped ({r})" for r, n in sorted(failures.items()))
        return None, [note]

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        notes: List[str] = []
        if tumor.request_id:
            candidates = db.scalars(
                self._candidate_stmt(tumor, request_id=tumor.request_id)
            ).all()
            picked, tier_notes = self._select_in_candidates(
                tumor, candidates, "same_request", self._soft_reasons
            )
            notes.extend(tier_notes)
            if picked is not None:
                return NormalSelection(picked, notes)
        tier_label = "other_requests" if tumor.request_id else "patient-wide"
        candidates = db.scalars(
            self._candidate_stmt(tumor, exclude_request_id=tumor.request_id or None)
        ).all()
        picked, tier_notes = self._select_in_candidates(
            tumor, candidates, tier_label, self._soft_reasons
        )
        notes.extend(tier_notes)
        return NormalSelection(picked, notes)

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        conds = [Sample.tumor_or_normal == TumorOrNormal.TUMOR.value]
        conds.extend(
            getattr(Sample, key) == getattr(normal, key) for key in self.HARD_KEYS
        )
        stmt = select(Sample).where(*conds)
        return list(db.scalars(stmt).all())


for name in ("IMPACT", "ACCESS"):
    strategies[name] = strategies["default"]


@register_pairing_strategy("argos")
class ArgosPairingStrategy(DefaultPairingStrategy):
    """ARGOS (WES) pairing strategy, aligned with beagle's argos pairing
    logic (v2_3.0).

    Matching keys (hard): patient_id, gene_panel, bait_set. Beagle's
    v2_3.0 logic never keys on specimen/sample type and does not gate on
    preservation, so ``sample_type`` is dropped from the hard keys and
    the preservation gate is removed; both attributes remain stored on
    the sample and are surfaced in pairing notes/reads.

    Extends the default strategy with:
      * run-mode (sequencer) matching — the raw ``run_mode`` strings are
        normalized to buckets and must be equal;
      * a request-first cascade (inherited) — normals ingested under the
        same ``request_id`` are preferred, then all other normals for the
        patient, with the latest ``sequencing_date`` winning within a
        tier (mirrors beagle's compare_dates "most recent" selection);
      * a pooled-normal tier — shared pooled normals (``sample_type`` in
        ``pooled_sample_types``, patient_id null), matched across patients
        on gene_panel + bait_set. Run mode is enforced only when the
        pooled sample carries one (run/machine data is not tracked).

    Per-logic config keys:
      * ``run_mode_map`` — dict of raw substring -> bucket, checked in
        order (default: DEFAULT_RUN_MODE_MAP);
      * ``pooled_sample_types`` — list of sample_type values marking a
        pooled normal (default: ["Pooled Normal"]).
    """

    HARD_KEYS: tuple = ("patient_id", "gene_panel", "bait_set")

    def _pooled_types(self) -> List[str]:
        raw = self.config.get("pooled_sample_types") or DEFAULT_POOLED_SAMPLE_TYPES
        return [str(t).lower() for t in raw]

    def _run_mode_map(self) -> Dict[str, str]:
        return self.config.get("run_mode_map") or DEFAULT_RUN_MODE_MAP

    def _is_pooled(self, sample: Sample) -> bool:
        return str(sample.sample_type or "").lower() in self._pooled_types()

    @staticmethod
    def _resolved_preservation(sample: Sample) -> str:
        return resolve_preservation(sample.preservation, sample.sample_origin)

    def _soft_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        t_bucket = normalize_run_mode(tumor.run_mode, self._run_mode_map())
        n_bucket = normalize_run_mode(candidate.run_mode, self._run_mode_map())
        if t_bucket != n_bucket:
            return [
                f"run_mode mismatch ({t_bucket or 'unknown'} vs {n_bucket or 'unknown'})"
            ]
        return []

    def _pooled_reasons(self, tumor: Sample, pooled: Sample) -> List[str]:
        reasons: List[str] = []
        if tumor.gene_panel != pooled.gene_panel:
            reasons.append(f"gene_panel mismatch ({tumor.gene_panel} vs {pooled.gene_panel})")
        if tumor.bait_set != pooled.bait_set:
            reasons.append(f"bait_set mismatch ({tumor.bait_set} vs {pooled.bait_set})")
        if pooled.run_mode:
            t_bucket = normalize_run_mode(tumor.run_mode, self._run_mode_map())
            p_bucket = normalize_run_mode(pooled.run_mode, self._run_mode_map())
            if t_bucket != p_bucket:
                reasons.append(
                    f"run_mode mismatch ({t_bucket or 'unknown'} vs {p_bucket or 'unknown'})"
                )
        return reasons

    def mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        if self._is_pooled(normal):
            return self._pooled_reasons(tumor, normal)
        reasons = self._base_mismatch_reasons(tumor, normal)
        reasons.extend(self._soft_reasons(tumor, normal))
        return reasons

    def matches(self, tumor: Sample, normal: Sample) -> bool:
        return not self.mismatch_reasons(tumor, normal)

    def _pooled_candidate_stmt(self, tumor: Sample):
        return select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
            func.lower(Sample.sample_type).in_(self._pooled_types()),
            Sample.gene_panel == tumor.gene_panel,
            Sample.bait_set == tumor.bait_set,
        )

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        selection = super().select_latest_normal(db, tumor, logic)
        if selection.normal is not None:
            return selection
        notes = list(selection.notes)
        candidates = db.scalars(self._pooled_candidate_stmt(tumor)).all()
        picked, tier_notes = self._select_in_candidates(
            tumor, candidates, "pooled", self._pooled_reasons
        )
        notes.extend(tier_notes)
        return NormalSelection(picked, notes)


@register_pairing_strategy("tempo")
class TempoPairingStrategy(DefaultPairingStrategy):
    """TEMPO_MPGEN / CHRONOS pairing strategy, aligned with beagle's
    tempo_patient.py (identical logic in both operators).

    Per patient, per tumor, beagle's rules are:
      1. Historical pairing (``pre_pairing`` — loaded from pairing.tsv,
         the previous run's output, optionally overridden). When a tumor
         has a historical normal, that normal is returned by identity and
         all other checks (bait, run mode, panel) are bypassed. There is
         no fallback: if the historical normal is gone the tumor stays
         unpaired.
      2. Otherwise patient-wide matching: the normal must be for the same
         patient, and match the tumor's bait_set (case-insensitive) and
         run mode (beagle buckets "novaseq"/"hiseq" substrings and
         compares case-insensitively). The most recent run date wins.

    Syzygy mapping:
      * The historical tier reads the tumor's current latest pairing row
        for this pairing logic (syzygy's equivalent of pairing.tsv).
      * Run dates come from ``sequencing_date``.
    Gene panel and sample_type are not keys (beagle uses sample_class
    only to classify tumor vs normal). There is no request cascade or
    pooled tier: beagle's TEMPO/CHRONOS pairing is patient-wide.

    Per-logic config keys:
      * ``run_mode_map`` — dict of raw substring -> bucket, checked in
        order (default: TEMPO_RUN_MODE_MAP).
    """

    HARD_KEYS: tuple = ("patient_id",)

    def _run_mode_map(self) -> Dict[str, str]:
        return self.config.get("run_mode_map") or TEMPO_RUN_MODE_MAP

    def _bait_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        t = str(tumor.bait_set or "").strip().lower()
        n = str(candidate.bait_set or "").strip().lower()
        if t != n:
            return [f"bait_set mismatch ({t or 'unknown'} vs {n or 'unknown'})"]
        return []

    def _soft_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        reasons = self._bait_reasons(tumor, candidate)
        t_bucket = normalize_run_mode(tumor.run_mode, self._run_mode_map())
        n_bucket = normalize_run_mode(candidate.run_mode, self._run_mode_map())
        if t_bucket != n_bucket:
            reasons.append(
                f"run_mode mismatch ({t_bucket or 'unknown'} vs {n_bucket or 'unknown'})"
            )
        return reasons

    def mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        # patient_id is the only hard key; bait_set + run_mode are soft.
        reasons = self._base_mismatch_reasons(tumor, normal)
        reasons.extend(self._soft_reasons(tumor, normal))
        return reasons

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        # 1. Historical pairing tier (beagle's pre_pairing / pairing.tsv).
        if logic is not None:
            hist = db.scalar(
                select(PairingTable).where(
                    PairingTable.tumor_id == tumor.id,
                    PairingTable.pairing_logic_id == logic.id,
                    PairingTable.latest == True,  # noqa: E712
                )
            )
            if hist is not None:
                normal = db.get(Sample, hist.normal_id)
                if normal is not None and normal.tumor_or_normal == TumorOrNormal.NORMAL.value:
                    note = "historical pairing retained (beagle pre_pairing)"
                    reasons = self.mismatch_reasons(tumor, normal)
                    if reasons:
                        note += "; bait/run-mode checks bypassed: " + "; ".join(reasons)
                    return NormalSelection(normal, [note])
                # beagle: pre_pairing points at a missing normal -> the tumor
                # stays unpaired (no patient-wide fallback).
                return NormalSelection(
                    None,
                    [
                        "historical pairing present but its normal sample is missing; "
                        "beagle has no fallback, so the tumor stays unpaired"
                    ],
                )
        # 2. Patient-wide tier (no request cascade, no pooled tier).
        if tumor.patient_id is None:
            return NormalSelection(
                None, ["patient_id missing: tempo pairing is patient-scoped"]
            )
        candidates = db.scalars(
            select(Sample).where(
                Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
                Sample.patient_id == tumor.patient_id,
            )
        ).all()
        picked, tier_notes = self._select_in_candidates(
            tumor, candidates, "patient-wide", self._soft_reasons
        )
        return NormalSelection(picked, tier_notes)

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        if normal.patient_id is None:
            return []
        tumors = db.scalars(
            select(Sample).where(
                Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
                Sample.patient_id == normal.patient_id,
            )
        ).all()
        return [t for t in tumors if not self._soft_reasons(t, normal)]


for name in ("TEMPO", "CHRONOS"):
    strategies[name] = strategies["tempo"]


def get_pairing_strategy(logic: PairingLogic) -> PairingStrategy:
    strategy = strategies.get(logic.strategy_name)
    if strategy is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pairing strategy: {logic.strategy_name}",
        )
    if logic.config:
        strategy = strategy.with_config(logic.config)
    return strategy
