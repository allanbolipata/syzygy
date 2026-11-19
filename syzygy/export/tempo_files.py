"""Syzygy-side generation of beagle's TEMPO operator files (ticket E1).

Spec: Projects/Syzygy/Factory/TEMPO_EXPORT_TICKETS.md.

E1 covers the two pairing-side files:

- ``sample_pairing.txt``  -- beagle's pairing TSV: the extended 4-column
  header (``NORMAL_ID``, ``TUMOR_ID`` plus the two ``*_PRIMARY_ID``
  columns), rows from the latest pairing rows for the logic, ordered
  deterministically by tumor ``cmo_sample_name``. ``NORMAL_ID`` /
  ``TUMOR_ID`` are the cmo display labels; the primary columns carry the
  ``primary_id`` values. The output parses cleanly through
  ``scripts/import_tempo_pairs.py --dry-run`` (round-trip covered in
  ``tests/test_tempo_export.py``).
- ``sample_unpaired.txt`` -- beagle's unpaired file: the same 8 fields as
  the conflict file (``cmoSampleName``, ``cmoPatientId``, ``primaryId``,
  ``sampleClass``, ``runMode``, ``sampleType``, ``baitSet``, ``runDate``)
  plus ``Possible Reason?``. Rows are the logic-family tumors with no
  latest pairing row for the logic; the reason is derived from the
  logic's pairing-strategy diagnostics (candidate normals + failing keys,
  the same mechanism as the possible-matches / MULTI checks) and is never
  empty.

E2 (tracker CSV) and E3 (conflict TSV) extend this module with their own
builders.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from syzygy.bait_sets import logic_membership_condition
from syzygy.models import PairingLogic, PairingTable, Sample
from syzygy.pairing_import import EXTENDED_HEADER
from syzygy.pairing_strategies import PairingStrategy, get_pairing_strategy
from syzygy.schemas import TumorOrNormal

#: beagle's operator filenames (create_pairing_file / create_unpaired_txt_file).
PAIRING_FILENAME = "sample_pairing.txt"
UNPAIRED_FILENAME = "sample_unpaired.txt"

#: beagle's unpaired file: the 8 conflict-file fields + the reason column.
UNPAIRED_HEADER: tuple[str, ...] = (
    "cmoSampleName",
    "cmoPatientId",
    "primaryId",
    "sampleClass",
    "runMode",
    "sampleType",
    "baitSet",
    "runDate",
    "Possible Reason?",
)


def _resolve_logic(db: Session, logic_name: str) -> PairingLogic:
    """The ``PairingLogic`` row for ``logic_name``.

    Raises ``ValueError`` (mapped to a 404 by the router) when the logic
    does not exist.
    """
    logic = db.scalar(select(PairingLogic).where(PairingLogic.name == logic_name))
    if logic is None:
        raise ValueError(f"pairing logic {logic_name!r} not found")
    return logic


def _tsv(rows: Iterable[Iterable[str]]) -> str:
    return "\n".join("\t".join(r) for r in rows) + "\n"


def build_pairing_tsv(db: Session, logic_name: str) -> str:
    """beagle's ``sample_pairing.txt`` for ``logic_name``.

    The extended 4-column header (``NORMAL_ID``, ``TUMOR_ID``,
    ``NORMAL_PRIMARY_ID``, ``TUMOR_PRIMARY_ID``) followed by one row per
    *latest* pairing row for the logic. ``NORMAL_ID`` / ``TUMOR_ID`` are
    the cmo display labels; the primary columns carry the
    ``primary_id`` values. Rows are ordered by tumor ``cmo_sample_name``
    (ties broken by normal name, then pairing id) so the output is
    byte-stable for a given database state.
    """
    logic = _resolve_logic(db, logic_name)
    pairs = (
        p
        for p in db.scalars(
            select(PairingTable).where(
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest.is_(True),
            )
        ).all()
        if p.tumor is not None and p.normal is not None
    )
    # Order by the TUMOR's cmo name (the spec's deterministic ordering),
    # normal name second, pairing id as the final tiebreak. Sorted in
    # Python so the ordering is explicit and dialect-independent.
    ordered = sorted(
        pairs,
        key=lambda p: (
            p.tumor.cmo_sample_name,
            p.normal.cmo_sample_name,
            p.id,
        ),
    )
    return _tsv(
        [EXTENDED_HEADER]
        + [
            (
                p.normal.cmo_sample_name,
                p.tumor.cmo_sample_name,
                p.normal.primary_id,
                p.tumor.primary_id,
            )
            for p in ordered
        ]
    )


def _run_date(value) -> str:
    """beagle's ``runDate`` cell: the sequencer run date as ``YYYY-MM-DD``
    (blank when the sample has no ``sequencing_date``)."""
    return value.strftime("%Y-%m-%d") if value else ""


def _possible_reason(
    strategy: PairingStrategy, db: Session, logic: PairingLogic, tumor: Sample
) -> str:
    """The non-empty ``Possible Reason?`` for an unpaired tumor.

    Derived from the logic's strategy diagnostics (the same candidate
    scope + ``mismatch_reasons`` mechanism as the possible-matches and
    MULTI checks):

    - no candidate normal in scope (tempo: no same-patient normal) ->
      beagle's ``No normals for patient`` wording;
    - candidates exist, every one fails -> the failing keys aggregated
      the way the strategy's ``_select_in_candidates`` notes them;
    - a fully matching candidate exists but no pairing row was ever
      created -> say so explicitly (the pairing trigger has not run for
      this tumor under this logic).
    """
    prefix = "tumor redacted; " if tumor.redacted else ""
    candidates = strategy.candidate_scope(db, tumor, logic)
    if not candidates:
        return prefix + "No normals for patient"
    failures: dict[str, int] = {}
    matched: list[Sample] = []
    for candidate in candidates:
        reasons = strategy.mismatch_reasons(tumor, candidate, db)
        if reasons:
            for reason in reasons:
                failures[reason] = failures.get(reason, 0) + 1
        else:
            matched.append(candidate)
    if matched:
        names = ", ".join(sorted(m.cmo_sample_name for m in matched))
        return (
            prefix
            + f"{len(matched)} matching candidate normal(s) ({names}) "
            f"but no pairing row for {logic.name} yet"
        )
    note = f"{len(candidates)} candidate normal(s), 0 matched"
    if failures:
        note += "; " + ", ".join(
            f"{count} skipped ({reason})"
            for reason, count in sorted(failures.items())
        )
    return prefix + note


def build_unpaired_tsv(db: Session, logic_name: str) -> str:
    """beagle's ``sample_unpaired.txt`` for ``logic_name``.

    Header is the 8 conflict-file fields plus ``Possible Reason?``. Rows
    are the logic-family tumors (``logic_membership_condition`` -- the
    same "samples of a given pairing logic" scope as the listing
    filters) with no *latest* pairing row for the logic, ordered by
    ``cmo_sample_name`` (ties broken by id). ``sampleClass`` is ``T``
    (every row is a tumor). The ``Possible Reason?`` cell is never empty.
    """
    logic = _resolve_logic(db, logic_name)
    strategy = get_pairing_strategy(logic)
    paired_tumor_ids = set(
        db.scalars(
            select(PairingTable.tumor_id).where(
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest.is_(True),
            )
        ).all()
    )
    tumors = db.scalars(
        select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
            logic_membership_condition(logic, sample=Sample),
        )
    ).all()
    unpaired = sorted(
        (t for t in tumors if t.id not in paired_tumor_ids),
        key=lambda t: (t.cmo_sample_name, t.id),
    )
    rows = [UNPAIRED_HEADER]
    for tumor in unpaired:
        rows.append(
            (
                tumor.cmo_sample_name or "",
                tumor.patient__cmo or "",
                tumor.primary_id or "",
                "T",
                tumor.run_mode or "",
                tumor.sample_type or "",
                tumor.bait_set or "",
                _run_date(tumor.sequencing_date),
                _possible_reason(strategy, db, logic, tumor),
            )
        )
    return _tsv(rows)
