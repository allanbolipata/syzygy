"""Pluggable sample pairing strategies.

A strategy decides which normal sample pairs with a tumor. Strategies are
registered under a name and referenced from a PairingLogic row via
``strategy_name``, so per-logic behavior can differ without new endpoints.
A logic's ``config`` dict customizes the strategy at use time (see
``get_pairing_strategy``).

Registered strategies:
  * ``default`` — patient-scoped matching on (patient__cmo, gene_panel,
    bait_set) with a request-first cascade: normals ingested under the
    same ``request_id`` are preferred, then all other normals for the
    patient.
  * ``argos`` — WES strategy aligned with beagle's argos pairing logic
    (v2_3.0). Matches on (patient__cmo, gene_panel, bait_set) with
    run-mode (sequencer) bucket matching, a request-first cascade, and a
    pooled normal tier (shared across patients). Beagle's v2.3.0 logic
    does not key on specimen/sample type, so it is not a matching key
    here (still stored on the sample). Preservation is a gate on every
    tier, pooled included: each side's (preservation, sample_origin)
    resolves to a category ("ffpe"/"frozen") or is not considered when
    both fields are empty (see ``resolve_preservation``); two considered
    sides must agree, a not-considered side never blocks, and an
    unresolvable side (a within-side conflict, or a junk origin when
    preservation is empty) blocks the pair.
    The pooled tier also resolves run mode through the pooled_machines
    table (pool machine name -> machine class; tumor run_mode ->
    novaseq/hiseq family). DMP samples (``sample_type`` == "DMP") are
    manual-pairing only: the automatic cascade never selects them, and
    any pair involving a DMP sample always carries a "DMP: manual
    pairing only" mismatch reason, so they surface in the Sample
    Pairing view as overridable candidates. The candidate scope lists
    them by patient only: a non-DMP sample lists every same-patient
    opposite-type DMP sample, and a DMP sample lists the same-patient
    opposite-type samples keyed on ``patient__dmp`` (the P- id) when
    available, or ``patient__cmo`` for legacy rows.
  * ``tempo`` — TEMPO_MPGEN / CHRONOS strategy aligned with beagle's
    tempo_patient.py: a historical-pairing tier (beagle's pre_pairing /
    pairing.tsv) retained unconditionally, then patient-wide matching on
    bait_set (case-insensitive) and run-mode bucket. Gene panel and
    sample_type are not keys; there is no request cascade or pooled tier.
  * ``dmp`` — DMP-group-only pairing (Syzygy-only; beagle has no DMP
    pairing logic): matches a DMP tumor to a DMP normal on
    (patient__dmp, gene_panel); never crosses into LIMS or pooled
    samples.
  * ``ACCESS`` — alias for ``default``.
  * ``IMPACT`` — alias for ``argos`` (IMPACT is the assay the ARGOS
    pipeline sequences; see ``PAIRING_LOGIC_ALIASES`` in
    ``pairing_service`` for the logic-name side of the same grouping).
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

from models import PairingLogic, PairingTable, PooledMachine, Sample
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
# belong to no patient (patient__cmo is null) and are offered as a last
# resort by the argos strategy. Configurable per pairing logic via the
# ``pooled_sample_types`` config key.
DEFAULT_POOLED_SAMPLE_TYPES: tuple = ("Pooled Normal",)

# Default machine reference rows for the pooled_machines table (used by
# seed_pooled_machines at startup and by tests). machine_class is the
# family used for pooled run-mode matching; machine_type is display-only.
DEFAULT_POOLED_MACHINES: tuple = (
    ("diana", "novaseq", "NovaSeq 6000"),
    ("jax", "hiseq", "HiSeq 4000"),
    ("kim", "hiseq", "HiSeq 2500"),
    ("liz", "hiseq", "HiSeq 2500"),
    ("michelle", "novaseq", "NovaSeq 6000"),
    ("momo", "hiseq", "HiSeq 2500"),
    ("pitt", "hiseq", "HiSeq 4000"),
    ("ruth", "novaseq", "NovaSeq 6000"),
)


def seed_pooled_machines(db: Session) -> None:
    """Idempotently insert DEFAULT_POOLED_MACHINES (missing rows only).

    Called from app startup so a fresh database gets the machine table;
    safe to call again (existing rows are left untouched).
    """
    for name, machine_class, machine_type in DEFAULT_POOLED_MACHINES:
        existing = db.scalar(
            select(PooledMachine).where(
                func.lower(PooledMachine.machine_name) == name.lower()
            )
        )
        if existing is None:
            db.add(
                PooledMachine(
                    machine_name=name,
                    machine_class=machine_class,
                    machine_type=machine_type,
                )
            )
    db.commit()



# Sample-origin entries (lower-cased) that resolve to the "frozen"
# category. "ffpe" resolves to "ffpe"; any other sample-origin entry is
# unresolvable.
_FROZEN_ORIGIN_VALUES = frozenset({"fresh or frozen", "frozen"})


def _value_set(value) -> set:
    """Lower-cased, stripped, non-empty entries of a JSON metadata field.

    preservation / sample_origin are JSON columns that may hold a list
    (mirroring beagle's list-valued metadata) or a scalar; every entry
    is considered, so any entry matching a category token qualifies.
    """
    if value is None:
        return set()
    items = value if isinstance(value, (list, tuple)) else (value,)
    return {str(v).strip().lower() for v in items if str(v).strip()}


def _origin_category(origin: set) -> Optional[str]:
    """Category of a sample-origin value set: "ffpe", "frozen", or None.

    Case-insensitive exact token match: "ffpe" -> "ffpe"; "fresh or
    frozen" or "frozen" -> "frozen". Any other value — or a set mixing
    both categories — is unresolvable (None).
    """
    cats = set()
    if "ffpe" in origin:
        cats.add("ffpe")
    if _FROZEN_ORIGIN_VALUES & origin:
        cats.add("frozen")
    if len(cats) != 1:
        return None
    return cats.pop()


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


def resolve_preservation(preservation, sample_origin) -> Optional[str]:
    """Resolve (preservation, sample_origin) for the ARGOS preservation
    gate.

    Returns one of:
      * ``"ffpe"`` / ``"frozen"`` — the side's resolved category;
      * ``""`` (empty string) — unresolvable: the fields conflict
        (preservation says ffpe but the origin names frozen, or
        vice versa), or preservation is empty and the origin alone
        does not resolve to a category. An unresolvable side blocks
        the pair;
      * ``None`` — not considered: both fields empty. The gate does
        not apply to that side and it never blocks.

    Fuzzy, case-insensitive resolution:
      * preservation non-empty -> "ffpe" if any entry is "ffpe",
        otherwise "frozen" (anything else). sample_origin is then free
        (junk origins like "Tissue" no longer block) unless it names
        the OPPOSITE category: an ffpe-side with origin "frozen" /
        "fresh or frozen", or a frozen-side with origin "ffpe", is
        unresolvable (within-side conflict);
      * preservation empty, sample_origin non-empty -> the origin's
        category: "ffpe" for "ffpe"; "frozen" for "fresh or frozen" or
        "frozen"; any other origin value is unresolvable;
      * both empty -> not considered (None).
    """
    p = _value_set(preservation)
    o = _value_set(sample_origin)
    if not p and not o:
        return None
    p_cat = "ffpe" if "ffpe" in p else "frozen" if p else None
    if p_cat is None:
        # Preservation empty: the origin alone decides.
        return _origin_category(o) or ""
    if (p_cat == "ffpe" and _FROZEN_ORIGIN_VALUES & o) or (
        p_cat == "frozen" and "ffpe" in o
    ):
        # Preservation present, but the origin names the opposite
        # category: within-side conflict -> unresolvable.
        return ""
    return p_cat


@dataclass
class NormalSelection:
    """Result of a strategy's normal selection.

    ``normal`` is the chosen normal (None when no candidate qualified);
    ``notes`` carries human-readable tier notes, including why candidates
    were skipped.
    """

    normal: Optional[Sample] = None
    notes: List[str] = field(default_factory=list)


def _norm_key(value) -> Optional[str]:
    """Case-insensitive comparison form of a key value (None stays None)."""
    return None if value is None else str(value).lower()


def _key_condition(column, value):
    """Case-insensitive hard-key equality for candidate queries.

    ``None`` values match only NULL columns: SQLAlchemy renders
    ``column == None`` as ``IS NULL``, which is the pre-existing
    behavior for null keys.
    """
    if value is None:
        return column.is_(None)
    return func.lower(column) == str(value).lower()


class PairingStrategy(Protocol):
    # Whether the strategy offers a pooled-normal tier (argos does).
    HAS_POOLED_TIER: bool

    def matches(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> bool:
        ...

    def mismatch_reasons(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> List[str]:
        ...

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        ...

    def candidate_scope(
        self,
        db: Session,
        sample: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        """Every opposite-type sample this strategy's cascade can reach.

        Powers the Matches & Overrides view: the candidate list mirrors
        what the strategy can actually select, so it stays bounded
        instead of returning the whole opposite-type table.
        """
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

    def _pooled_types(self) -> List[str]:
        ...

    def _is_pooled(self, sample: Sample) -> bool:
        ...

    def _is_dmp(self, sample: Sample) -> bool:
        ...


strategies: Dict[str, PairingStrategy] = {}


def register_pairing_strategy(name: str):
    def decorator(cls):
        strategies[name] = cls()
        return cls
    return decorator


@register_pairing_strategy("default")
class DefaultPairingStrategy:
    """Reference pairing strategy (used by ACCESS).

    Base keys: see ``HARD_KEYS`` (patient__cmo, gene_panel, bait_set).
    Cascade:
    prefer normals ingested under the same request_id (when the tumor has
    one), then all other normals for the patient.
    """

    # Sample attributes that must match on both sides of a pairing.
    # Overridden by strategies that align with other operators (argos).
    HARD_KEYS: tuple = ("patient__cmo", "gene_panel", "bait_set")
    # Whether the strategy offers a pooled-normal tier (argos does).
    HAS_POOLED_TIER: bool = False

    def __init__(self, config: Optional[dict] = None):
        self.config = dict(config or {})

    def with_config(self, config: dict) -> "PairingStrategy":
        return type(self)(config=dict(config or {}))

    def _pooled_types(self) -> List[str]:
        raw = self.config.get("pooled_sample_types") or DEFAULT_POOLED_SAMPLE_TYPES
        return [str(t).lower() for t in raw]

    def _is_pooled(self, sample: Sample) -> bool:
        return False

    @staticmethod
    def _is_dmp(sample: Sample) -> bool:
        # DMP is an argos-strategy concept; other strategies treat
        # sample_type "DMP" as an ordinary sample type.
        return False

    # -- matching -------------------------------------------------------

    def _base_mismatch_reasons(self, tumor: Sample, normal: Sample) -> List[str]:
        # Hard keys compare case-insensitively; None matches only NULL.
        reasons: List[str] = []
        for label in self.HARD_KEYS:
            t_val = getattr(tumor, label)
            n_val = getattr(normal, label)
            if _norm_key(t_val) != _norm_key(n_val):
                reasons.append(f"{label} mismatch ({t_val} vs {n_val})")
        return reasons

    def mismatch_reasons(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> List[str]:
        # ``db`` is accepted for protocol compatibility; the default
        # strategy has no pooled tier, so it never needs it.
        return self._base_mismatch_reasons(tumor, normal)

    def matches(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> bool:
        return not self.mismatch_reasons(tumor, normal, db)

    # -- candidate selection ---------------------------------------------

    def _candidate_stmt(
        self,
        tumor: Sample,
        request_id: Optional[str] = None,
        exclude_request_id: Optional[str] = None,
    ):
        conds = [Sample.tumor_or_normal == TumorOrNormal.NORMAL.value]
        conds.extend(
            _key_condition(getattr(Sample, key), getattr(tumor, key))
            for key in self.HARD_KEYS
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

    def candidate_scope(
        self,
        db: Session,
        sample: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        role = (
            TumorOrNormal.NORMAL.value
            if sample.tumor_or_normal == TumorOrNormal.TUMOR.value
            else TumorOrNormal.TUMOR.value
        )
        conds = [Sample.tumor_or_normal == role]
        conds.extend(
            _key_condition(getattr(Sample, key), getattr(sample, key))
            for key in self.HARD_KEYS
        )
        return list(db.scalars(select(Sample).where(*conds)).all())

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        conds = [Sample.tumor_or_normal == TumorOrNormal.TUMOR.value]
        conds.extend(
            _key_condition(getattr(Sample, key), getattr(normal, key))
            for key in self.HARD_KEYS
        )
        stmt = select(Sample).where(*conds)
        return list(db.scalars(stmt).all())


for name in ("ACCESS",):
    strategies[name] = strategies["default"]


@register_pairing_strategy("argos")
class ArgosPairingStrategy(DefaultPairingStrategy):
    """ARGOS (WES) pairing strategy, aligned with beagle's argos pairing
    logic (v2_3.0).

    Matching keys (hard): patient__cmo, gene_panel, bait_set. Beagle's
    v2.3.0 logic never keys on specimen/sample type, so ``sample_type``
    is dropped from the hard keys; it remains stored on the sample and
    is surfaced in pairing notes/reads.

    Preservation is a soft gate on every tier, pooled included. Each
    side's (preservation, sample_origin) resolves via
    ``resolve_preservation``: a present preservation decides the
    category (any "ffpe" entry -> "ffpe", anything else -> "frozen"),
    with the sample origin free unless it names the opposite category
    (a within-side conflict -> unresolvable); a missing preservation
    lets a resolvable origin decide; both fields empty means the side
    is not considered. When both sides are considered their categories
    must agree; a not-considered side never blocks; an unresolvable
    side (conflict, or a junk origin-only value) blocks the pair.

    Extends the default strategy with:
      * run-mode (sequencer) matching — the raw ``run_mode`` strings are
        normalized to buckets and must be equal;
      * a request-first cascade (inherited) — normals ingested under the
        same ``request_id`` are preferred, then all other normals for the
        patient, with the latest ``sequencing_date`` winning within a
        tier (mirrors beagle's compare_dates "most recent" selection);
      * a pooled-normal tier — shared pooled normals (``sample_type`` in
        ``pooled_sample_types``, patient__cmo null), matched across patients
        on gene_panel + bait_set. Run mode is resolved through the
        pooled_machines table: the pool's machine name (its raw run_mode)
        maps to a machine class, the tumor's run_mode maps to a
        novaseq/hiseq family, and the pair matches only when both resolve
        to the same family (unresolvable on either side blocks it). The
        preservation gate still applies;
      * DMP samples (``sample_type`` == "DMP") are manual-pairing only:
        the automatic cascade never selects them (``select_latest_normal``
        returns no selection for a DMP tumor, and ``select_matching_tumors``
        returns nothing for a DMP normal), and any pair involving a DMP
        sample always carries the "DMP: manual pairing only" mismatch
        reason, so the Sample Pairing view shows them as overridable
        candidates. The candidate scope lists them by patient only: a
        non-DMP sample lists every same-patient opposite-type DMP sample,
        and a DMP sample lists the same-patient opposite-type samples
        keyed on ``patient__dmp`` (the P- id) when the row carries one,
        or on ``patient__cmo`` for legacy rows. The run-mode and
        preservation gates are skipped when either side is DMP (the
        manual-only reason stands in for them).

    Per-logic config keys:
      * ``run_mode_map`` — dict of raw substring -> bucket, checked in
        order (default: DEFAULT_RUN_MODE_MAP);
      * ``pooled_sample_types`` — list of sample_type values marking a
        pooled normal (default: ["Pooled Normal"]).
    """

    HARD_KEYS: tuple = ("patient__cmo", "gene_panel", "bait_set")
    HAS_POOLED_TIER: bool = True

    def _run_mode_map(self) -> Dict[str, str]:
        return self.config.get("run_mode_map") or DEFAULT_RUN_MODE_MAP

    def _is_pooled(self, sample: Sample) -> bool:
        return str(sample.sample_type or "").lower() in self._pooled_types()

    @staticmethod
    def _resolved_preservation(sample: Sample) -> Optional[str]:
        return resolve_preservation(sample.preservation, sample.sample_origin)

    @staticmethod
    def _is_dmp(sample: Sample) -> bool:
        return str(sample.sample_type or "").strip().upper() == "DMP"

    def _soft_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        # DMP samples are manual-pairing only: they are never
        # auto-selected, and any pair involving a DMP sample carries the
        # manual-only mismatch reason (so the Sample Pairing view shows
        # it as an overridable candidate). DMP records carry no
        # run_mode/preservation/sample_origin, so those gates do not
        # apply when either side is DMP; the manual-only reason stands
        # in for them.
        if self._is_dmp(tumor) or self._is_dmp(candidate):
            return ["DMP: manual pairing only"]
        reasons: List[str] = []
        t_bucket = normalize_run_mode(tumor.run_mode, self._run_mode_map())
        n_bucket = normalize_run_mode(candidate.run_mode, self._run_mode_map())
        if t_bucket != n_bucket:
            reasons.append(
                f"run_mode mismatch ({t_bucket or 'unknown'} vs {n_bucket or 'unknown'})"
            )
        reasons.extend(self._preservation_reasons(tumor, candidate))
        return reasons

    def _preservation_reasons(self, tumor: Sample, candidate: Sample) -> List[str]:
        """Preservation gate.

        Each side resolves via ``resolve_preservation`` to "ffpe",
        "frozen", "" (unresolvable), or None (not considered: both
        fields empty). An unresolvable side blocks the pair (even
        against a not-considered partner); a not-considered side never
        blocks; two considered sides must agree on category.
        """
        t_res = self._resolved_preservation(tumor)
        n_res = self._resolved_preservation(candidate)
        reasons: List[str] = []
        if t_res == "":
            reasons.append("preservation unresolvable on tumor")
        if n_res == "":
            reasons.append("preservation unresolvable on normal")
        if reasons:
            return reasons
        if t_res is None or n_res is None:
            return []
        if t_res != n_res:
            return [f"preservation mismatch ({t_res} vs {n_res})"]
        return []

    @staticmethod
    def _tumor_machine_family(run_mode: Optional[str]) -> str:
        """Machine family of a tumor's raw run_mode string,
        case-insensitive: 'novaseq', 'hiseq', or '' when missing or
        unresolvable (e.g. 'NextSeq')."""
        s = (run_mode or "").strip().lower()
        if "novaseq" in s:
            return "novaseq"
        if "hiseq" in s:
            return "hiseq"
        return ""

    def _pooled_machine_class(self, db: Optional[Session], pooled: Sample) -> str:
        """Machine class for a pooled normal, looked up in the
        pooled_machines table by its machine name (the pooled normal's
        raw run_mode value): 'novaseq'/'hiseq', or '' when the pooled
        normal has no run_mode or its machine is not in the table."""
        name = (pooled.run_mode or "").strip().lower()
        if not name or db is None:
            return ""
        row = db.scalar(
            select(PooledMachine).where(
                func.lower(PooledMachine.machine_name) == name
            )
        )
        return row.machine_class if row is not None else ""

    def _pooled_reasons(
        self, db: Optional[Session], tumor: Sample, pooled: Sample
    ) -> List[str]:
        # Pooled run-mode check: the pool's machine name resolves to a
        # class via the pooled_machines table; the tumor's run_mode
        # resolves to a novaseq/hiseq family. Both must resolve and
        # agree. The preservation gate still applies too.
        reasons: List[str] = []
        if _norm_key(tumor.gene_panel) != _norm_key(pooled.gene_panel):
            reasons.append(f"gene_panel mismatch ({tumor.gene_panel} vs {pooled.gene_panel})")
        if _norm_key(tumor.bait_set) != _norm_key(pooled.bait_set):
            reasons.append(f"bait_set mismatch ({tumor.bait_set} vs {pooled.bait_set})")
        t_family = self._tumor_machine_family(tumor.run_mode)
        p_class = self._pooled_machine_class(db, pooled)
        if not t_family:
            reasons.append("tumor run_mode unresolvable")
        elif not p_class:
            reasons.append(
                "pooled machine not in table "
                f"({(pooled.run_mode or '').strip() or 'missing'})"
            )
        elif t_family != p_class:
            reasons.append(f"run_mode mismatch ({t_family} vs {p_class})")
        reasons.extend(self._preservation_reasons(tumor, pooled))
        return reasons

    def mismatch_reasons(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> List[str]:
        if self._is_pooled(normal):
            # Pooled tier has its own relaxed key set (the machine-table
            # run-mode check needs the session).
            reasons = list(self._pooled_reasons(db, tumor, normal))
            # The pooled tier does not go through _soft_reasons, so the
            # DMP manual-only reason is added here (the non-pooled path
            # picks it up from _soft_reasons).
            if self._is_dmp(tumor) or self._is_dmp(normal):
                reasons.append("DMP: manual pairing only")
            return reasons
        reasons = self._base_mismatch_reasons(tumor, normal)
        reasons.extend(self._soft_reasons(tumor, normal))
        return reasons

    def matches(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> bool:
        return not self.mismatch_reasons(tumor, normal, db)

    def _pooled_candidate_stmt(self, tumor: Sample):
        return select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
            func.lower(Sample.sample_type).in_(self._pooled_types()),
            _key_condition(Sample.gene_panel, tumor.gene_panel),
            _key_condition(Sample.bait_set, tumor.bait_set),
        )

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        # DMP normals are manual-pairing only: ingesting one never
        # auto-pairs it to a tumor.
        if self._is_dmp(normal):
            return []
        return super().select_matching_tumors(db, normal, logic)

    @staticmethod
    def _patient_key_condition(sample: Sample):
        """Same-patient condition for candidate listing.

        Keys on ``patient__dmp`` (the P- id) when the sample carries
        one, and falls back to ``patient__cmo`` for legacy rows (LIMS
        samples never carry ``patient__dmp``). Returns None when the
        sample carries neither.
        """
        if sample.patient__dmp is not None:
            return _key_condition(Sample.patient__dmp, sample.patient__dmp)
        if sample.patient__cmo is not None:
            return _key_condition(Sample.patient__cmo, sample.patient__cmo)
        return None

    def _dmp_candidates(self, db: Session, sample: Sample) -> List[Sample]:
        """Every opposite-type DMP sample for the sample's patient.

        "Same patient" keys on ``patient__dmp`` (the P- id) when the
        sample carries one, falling back to ``patient__cmo`` for legacy
        rows and LIMS samples.
        """
        patient_condition = self._patient_key_condition(sample)
        if patient_condition is None:
            # A patient-less sample has no "same patient" to share.
            return []
        role = (
            TumorOrNormal.NORMAL.value
            if sample.tumor_or_normal == TumorOrNormal.TUMOR.value
            else TumorOrNormal.TUMOR.value
        )
        return list(
            db.scalars(
                select(Sample).where(
                    Sample.tumor_or_normal == role,
                    patient_condition,
                    func.trim(func.upper(Sample.sample_type)) == "DMP",
                )
            ).all()
        )

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        # DMP tumors are manual-pairing only: no automatic selection.
        # (The default cascade below also skips DMP normals via
        # _soft_reasons.)
        if self._is_dmp(tumor):
            return NormalSelection(
                None,
                ["tumor is DMP: manual pairing only (no automatic selection)"],
            )
        selection = super().select_latest_normal(db, tumor, logic)
        if selection.normal is not None:
            return selection
        notes = list(selection.notes)
        candidates = db.scalars(self._pooled_candidate_stmt(tumor)).all()
        picked, tier_notes = self._select_in_candidates(
            tumor,
            candidates,
            "pooled",
            lambda t, c: self._pooled_reasons(db, t, c),
        )
        notes.extend(tier_notes)
        return NormalSelection(picked, notes)

    def candidate_scope(
        self,
        db: Session,
        sample: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        role = (
            TumorOrNormal.NORMAL.value
            if sample.tumor_or_normal == TumorOrNormal.TUMOR.value
            else TumorOrNormal.TUMOR.value
        )
        if self._is_dmp(sample):
            # A DMP sample: the same-patient opposite-type samples are
            # potential override candidates, keyed on ``patient__dmp``
            # (the P- id) when the row carries one, or on
            # ``patient__cmo`` for legacy rows without it.
            patient_condition = self._patient_key_condition(sample)
            if patient_condition is not None:
                candidates = list(
                    db.scalars(
                        select(Sample).where(
                            Sample.tumor_or_normal == role,
                            patient_condition,
                        )
                    ).all()
                )
            else:
                candidates = []
            if role == TumorOrNormal.NORMAL.value:
                # The pooled tier still applies for a DMP tumor.
                pooled = list(db.scalars(self._pooled_candidate_stmt(sample)).all())
                known = {c.id for c in candidates}
                candidates.extend(p for p in pooled if p.id not in known)
            return candidates
        if sample.tumor_or_normal == TumorOrNormal.TUMOR.value:
            candidates = super().candidate_scope(db, sample, logic)
            if sample.patient__cmo is None:
                # Patient-null hard-key scope already includes every
                # matching pooled normal; nothing extra to add.
                return candidates
            pooled = list(db.scalars(self._pooled_candidate_stmt(sample)).all())
            dmp = self._dmp_candidates(db, sample)
            known = {c.id for c in candidates}
            candidates.extend(c for c in pooled + dmp if c.id not in known)
            return candidates
        if self._is_pooled(sample):
            # Only the pooled tier can pick a pooled normal: any tumor
            # sharing its gene_panel + bait_set, regardless of patient.
            conds = [
                Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
                _key_condition(Sample.gene_panel, sample.gene_panel),
                _key_condition(Sample.bait_set, sample.bait_set),
            ]
            return list(db.scalars(select(Sample).where(*conds)).all())
        # Non-pooled normal: base scope plus same-patient DMP tumors.
        candidates = super().candidate_scope(db, sample, logic)
        if sample.patient__cmo is not None:
            dmp = self._dmp_candidates(db, sample)
            known = {c.id for c in candidates}
            candidates.extend(c for c in dmp if c.id not in known)
        return candidates


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

    HARD_KEYS: tuple = ("patient__cmo",)

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

    def mismatch_reasons(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> List[str]:
        # patient__cmo is the only hard key; bait_set + run_mode are soft.
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
        if tumor.patient__cmo is None:
            return NormalSelection(
                None, ["patient__cmo missing: tempo pairing is patient-scoped"]
            )
        candidates = db.scalars(
            select(Sample).where(
                Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
                _key_condition(Sample.patient__cmo, tumor.patient__cmo),
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
        if normal.patient__cmo is None:
            return []
        tumors = db.scalars(
            select(Sample).where(
                Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
                _key_condition(Sample.patient__cmo, normal.patient__cmo),
            )
        ).all()
        return [t for t in tumors if not self._soft_reasons(t, normal)]

    def candidate_scope(
        self,
        db: Session,
        sample: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        # Tempo is patient-scoped (no pooled tier): everything the
        # patient-wide tier can reach is one same-patient query away.
        if sample.patient__cmo is None:
            return []
        role = (
            TumorOrNormal.NORMAL.value
            if sample.tumor_or_normal == TumorOrNormal.TUMOR.value
            else TumorOrNormal.TUMOR.value
        )
        conds = [
            Sample.tumor_or_normal == role,
            _key_condition(Sample.patient__cmo, sample.patient__cmo),
        ]
        return list(db.scalars(select(Sample).where(*conds)).all())


@register_pairing_strategy("dmp")
class DmpPairingStrategy(DefaultPairingStrategy):
    """DMP pairing strategy (Syzygy-only — beagle has no DMP
    tumor->normal pairing logic).

    Automatic DMP pairing is strictly within the DMP samples group
    (``sample_type == "DMP"``): it never touches LIMS samples, pooled
    normals, or cross-group DMP<->LIMS pairs (those stay manual under
    the argos strategy).

    Matching keys (hard, compared case-insensitively):
      * ``patient__dmp`` (the "P-" patient id) — must be non-null and
        equal on both sides; this is the patient key for DMP pairing;
      * ``gene_panel`` — one P- patient can carry both IMPACT and
        ACCESS DMP samples, so cross-assay T/N pairing is invalid.

    Tumor vs. normal is enforced by the service exactly like every
    other strategy (``trigger_pairing`` rejects non-TUMOR input and the
    cascade queries only opposite-type candidates), so the checks here
    do not re-test it.

    Gates: preservation — DMP samples carry no preservation data
    (``preservation`` and ``sample_origin`` are both None), so the
    both-empty rule (Rule C) marks the gate "not considered" and it
    always passes; it is deliberately not implemented. run_mode — N/A
    for DMP, not gated.

    Multiple candidates: when a DMP tumor has several matching DMP
    normals, the winner is deterministic — the first in ``primary_id``
    (string) order. This reuses the argos/default selection mechanism
    (``_select_in_candidates`` picking via ``_latest``) with the
    ordering key changed from sequencing date to primary_id.

    Per-logic config keys: none.
    """

    HARD_KEYS: tuple = ("patient__dmp", "gene_panel")
    HAS_POOLED_TIER: bool = False

    @staticmethod
    def _is_dmp(sample: Sample) -> bool:
        return str(sample.sample_type or "").strip().upper() == "DMP"

    def mismatch_reasons(
        self, tumor: Sample, normal: Sample, db: Optional[Session] = None
    ) -> List[str]:
        # The dmp cascade is DMP-group only: a pair with either side
        # outside the group (LIMS, pooled normal) can never be selected
        # under this logic and stays manual under argos.
        if not (self._is_dmp(tumor) and self._is_dmp(normal)):
            return ["not a DMP sample"]
        return self._base_mismatch_reasons(tumor, normal)

    def _candidate_conditions(self, sample: Sample) -> List:
        """Opposite-type, same-patient, same-panel DMP-group candidates
        for one side of a pair."""
        role = (
            TumorOrNormal.NORMAL.value
            if sample.tumor_or_normal == TumorOrNormal.TUMOR.value
            else TumorOrNormal.TUMOR.value
        )
        return [
            Sample.tumor_or_normal == role,
            func.trim(func.upper(Sample.sample_type)) == "DMP",
            _key_condition(Sample.patient__dmp, sample.patient__dmp),
            _key_condition(Sample.gene_panel, sample.gene_panel),
        ]

    @staticmethod
    def _latest(candidates: Sequence[Sample]) -> Sample:
        # Deterministic winner: first candidate in primary_id (string)
        # order.
        return min(candidates, key=lambda s: str(s.primary_id))

    def select_latest_normal(
        self,
        db: Session,
        tumor: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> NormalSelection:
        # DMP-group only: a non-DMP tumor has no DMP counterpart, and a
        # DMP tumor without a P- id cannot be patient__dmp-scoped.
        if not self._is_dmp(tumor):
            return NormalSelection(
                None,
                ["tumor is not a DMP sample: dmp pairing is DMP-group only"],
            )
        if tumor.patient__dmp is None:
            return NormalSelection(
                None,
                [
                    "patient__dmp missing: "
                    "dmp pairing is patient__dmp-scoped"
                ],
            )
        candidates = db.scalars(
            select(Sample).where(*self._candidate_conditions(tumor))
        ).all()
        picked, tier_notes = self._select_in_candidates(
            tumor, candidates, "dmp", self.mismatch_reasons
        )
        return NormalSelection(picked, tier_notes)

    def select_matching_tumors(
        self,
        db: Session,
        normal: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        # A non-DMP normal (LIMS) has no DMP-group counterpart, and a
        # DMP normal without a P- id cannot be patient__dmp-scoped.
        if not self._is_dmp(normal) or normal.patient__dmp is None:
            return []
        tumors = db.scalars(
            select(Sample).where(*self._candidate_conditions(normal))
        ).all()
        return [t for t in tumors if not self.mismatch_reasons(t, normal)]

    def candidate_scope(
        self,
        db: Session,
        sample: Sample,
        logic: Optional[PairingLogic] = None,
    ) -> List[Sample]:
        # The dmp cascade is strictly DMP-group only: a non-DMP sample
        # (or a DMP sample without a P- id) has no cascade candidates
        # here — cross-group DMP<->LIMS pairing stays manual under
        # argos, not here.
        if not self._is_dmp(sample) or sample.patient__dmp is None:
            return []
        return list(
            db.scalars(
                select(Sample).where(*self._candidate_conditions(sample))
            ).all()
        )


# Strategy-name aliases: a PairingLogic row may reference a strategy by
# its pipeline name rather than its canonical name. IMPACT is the assay
# the ARGOS pipeline sequences, so it pairs with the argos strategy.
strategies["IMPACT"] = strategies["argos"]
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
