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

E2 (ticket E2) adds ``build_tracker_tsv``: beagle's
``sample_tracker.txt`` — the byte-equal PM header
(``TRACKER_HEADER``, 6 PM-formatted columns + the 17 raw metadata keys),
one row per sample in the logic's family (tumors and normals, paired or
unpaired — beagle's tracker covers whole patients). Raw-metadata columns
follow the spec's resolution order (sample column ->
``additional_metadata`` -> ``additional_metadata["requestMetadata"]`` ->
empty cell), never raising on a missing key; DMP samples put
``primary_id`` in the ``Collaborator_ID_(or_DMP_Sample_ID)`` column.

E3 (ticket E3) adds ``build_conflict_tsv``: beagle's
``sample_conflict.txt`` — the byte-equal conflict header
(``CONFLICT_HEADER``, the 8 sample fields + ``Conflict Reason``), one row
per sample whose ``errors`` is non-null and non-empty (ingest validation
failures — per the spec's DECIDED v1, Syzygy's closest concept to
beagle's cross-file metadata conflicts), logic-agnostic (the spec scopes
conflict to *all* samples with errors, unlike the pairing-logic-scoped
builders). ``Conflict Reason`` is the error strings joined with ``;``
(beagle's conflict-join convention), tabs/newlines flattened.
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

#: beagle's operator filenames (create_pairing_file /
#: create_unpaired_txt_file / create_tracker_file /
#: create_conflict_samples_txt_file).
PAIRING_FILENAME = "sample_pairing.txt"
UNPAIRED_FILENAME = "sample_unpaired.txt"
TRACKER_FILENAME = "sample_tracker.txt"
CONFLICT_FILENAME = "sample_conflict.txt"

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

#: beagle's conflict header — byte-equal to what
#: ``TempoMpgenOperator.create_conflict_samples_txt_file`` writes (beagle
#: ``runner/operator/tempo_mpgen_operator/tempo_mpgen_operator.py``): the
#: 8 sample fields followed by ``Conflict Reason``, tab-joined, no
#: trailing tab. The field names are beagle's ``settings.py`` metadata
#: keys (``CMO_SAMPLE_NAME_METADATA_KEY`` -> cmoSampleName,
#: ``PATIENT_ID_METADATA_KEY`` -> cmoPatientId,
#: ``SAMPLE_ID_METADATA_KEY`` -> primaryId,
#: ``SAMPLE_CLASS_METADATA_KEY`` -> sampleClass,
#: ``CMO_SAMPLE_CLASS_METADATA_KEY`` -> sampleType) plus the literal
#: ``runMode`` / ``baitSet`` / ``runDate`` — verified against the beagle
#: source 2026-11-19. The same 8 fields head the unpaired file
#: (``UNPAIRED_HEADER``).
CONFLICT_HEADER: tuple[str, ...] = (
    "cmoSampleName",
    "cmoPatientId",
    "primaryId",
    "sampleClass",
    "runMode",
    "sampleType",
    "baitSet",
    "runDate",
    "Conflict Reason",
)

#: beagle's tracker header — byte-equal to what
#: ``TempoMpgenOperator.create_tracker_file`` writes (beagle
#: ``runner/operator/tempo_mpgen_operator/tempo_mpgen_operator.py``):
#: the 6 PM-formatted columns followed by the 17 raw metadata keys used
#: verbatim as column names, tab-joined, no trailing tab. Verified
#: against the beagle source 2026-11-19; the raw metadata column names
#: are beagle's ``settings.py`` metadata-key values (``RECIPE_METADATA_KEY``
#: -> genePanel, ``SAMPLE_CLASS_METADATA_KEY`` -> sampleClass,
#: ``SAMPLE_ID_METADATA_KEY`` -> primaryId, ``PATIENT_ID_METADATA_KEY`` ->
#: the CMO patient-id key).
TRACKER_HEADER: tuple[str, ...] = (
    "CMO_Sample_ID",
    "Collaborator_ID_(or_DMP_Sample_ID)",
    "Historical_Investigator_ID_(for_CCS_use)",
    "Sample_Class_(T/N)",
    "Bait_set_(Agilent/_IDT/WGS)",
    "IGO_Request_ID_(Project_ID)",
    "tumorOrNormal",
    "species",
    "genePanel",
    "sampleClass",
    "primaryId",
    "cmoPatientId",
    "investigatorName",
    "investigatorEmail",
    "piEmail",
    "labHeadName",
    "labHeadEmail",
    "preservation",
    "dataAnalystName",
    "dataAnalystEmail",
    "projectManagerName",
    "investigatorSampleId",
    "cmoSampleName",
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


# ---------------------------------------------------------------------------
# build_tracker_tsv — beagle's sample_tracker.txt (ticket E2)
# ---------------------------------------------------------------------------


def _cell_str(value) -> str:
    """One TSV cell: ``None`` -> empty, lists joined with ``,``; tabs and
    line breaks sanitized to spaces so a value can never break the column
    grid."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = ",".join(str(item) for item in value if item is not None)
    else:
        text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _metadata_cell(sample: Sample, key: str) -> str:
    """A raw-metadata column by the spec's resolution order:
    ``additional_metadata`` -> ``additional_metadata["requestMetadata"]``
    -> empty cell. Never raises: a missing key, a ``None`` value, a
    non-dict blob, or an absent ``requestMetadata`` all resolve to an
    empty cell."""
    am = sample.additional_metadata
    if isinstance(am, dict):
        value = am.get(key)
        if isinstance(value, (str, int, float, bool)):
            return _cell_str(value)
        nested = am.get("requestMetadata")
        if isinstance(nested, dict):
            value = nested.get(key)
            if isinstance(value, (str, int, float, bool)):
                return _cell_str(value)
    return ""


def _resolved(sample: Sample, key: str, sample_value) -> str:
    """The spec's full column resolution order: the sample column (when
    one exists for the key) -> ``additional_metadata`` ->
    ``additional_metadata["requestMetadata"]`` -> empty cell."""
    if sample_value is None or sample_value == "":
        return _metadata_cell(sample, key)
    return _cell_str(sample_value)


def _is_dmp(sample: Sample) -> bool:
    """DMP-ingested sample (provenance marker ``sample_type == "DMP"``,
    per the model's column docs) — same convention as
    ``pairing_service._is_dmp_sample``."""
    return str(sample.sample_type or "").strip().upper() == "DMP"


def _class_cell(sample: Sample) -> str:
    """beagle's T/N class cell from the ``tumor_or_normal`` column."""
    return "T" if sample.tumor_or_normal == TumorOrNormal.TUMOR.value else "N"


def _tracker_row(sample: Sample) -> tuple[str, ...]:
    """One tracker row in ``TRACKER_HEADER`` order."""
    tnn = _class_cell(sample)
    if _is_dmp(sample):
        # Spec DECIDED: DMP samples put their DMP sample id (primary_id)
        # in the Collaborator slot.
        collaborator = _cell_str(sample.primary_id)
    else:
        # beagle fills the Collaborator slot from the
        # investigatorSampleId metadata for non-DMP samples.
        collaborator = _metadata_cell(sample, "investigatorSampleId")
    return (
        _cell_str(sample.cmo_sample_name),  # CMO_Sample_ID
        collaborator,  # Collaborator_ID_(or_DMP_Sample_ID)
        # Spec DECIDED: Historical_Investigator_ID <- investigatorSampleId
        # when present.
        _metadata_cell(sample, "investigatorSampleId"),
        tnn,  # Sample_Class_(T/N)
        _resolved(sample, "baitSet", sample.bait_set),
        _resolved(sample, "igoRequestId", sample.request_id),
        tnn,  # tumorOrNormal
        _metadata_cell(sample, "species"),
        _resolved(sample, "genePanel", sample.gene_panel),
        tnn,  # sampleClass
        _resolved(sample, "primaryId", sample.primary_id),
        _resolved(sample, "cmoPatientId", sample.patient__cmo),
        _metadata_cell(sample, "investigatorName"),
        _metadata_cell(sample, "investigatorEmail"),
        _metadata_cell(sample, "piEmail"),
        _metadata_cell(sample, "labHeadName"),
        _metadata_cell(sample, "labHeadEmail"),
        _resolved(sample, "preservation", sample.preservation),
        _metadata_cell(sample, "dataAnalystName"),
        _metadata_cell(sample, "dataAnalystEmail"),
        _metadata_cell(sample, "projectManagerName"),
        _metadata_cell(sample, "investigatorSampleId"),
        _cell_str(sample.cmo_sample_name),  # cmoSampleName
    )


def build_tracker_tsv(db: Session, logic_name: str) -> str:
    """beagle's ``sample_tracker.txt`` for ``logic_name``.

    Header is byte-equal to beagle's tracker header (``TRACKER_HEADER``).
    Rows are every sample in the logic's family
    (``logic_membership_condition`` — the same "samples of a given pairing
    logic" scope as the listing filters and the unpaired builder), both
    tumors and normals, paired or unpaired (beagle's tracker covers whole
    patients, including unpaired normals), ordered by
    ``cmo_sample_name`` (ties broken by id).

    Column resolution follows the spec's order: the sample column (where
    one exists for the key) -> ``additional_metadata`` ->
    ``additional_metadata["requestMetadata"]`` -> empty cell; a missing
    key never raises. DMP samples (``sample_type == "DMP"``) put
    ``primary_id`` in the ``Collaborator_ID_(or_DMP_Sample_ID)`` column;
    other samples take ``investigatorSampleId`` from the metadata there.
    """
    logic = _resolve_logic(db, logic_name)
    samples = db.scalars(
        select(Sample).where(logic_membership_condition(logic, sample=Sample))
    ).all()
    ordered = sorted(samples, key=lambda s: (s.cmo_sample_name, s.id))
    return _tsv([TRACKER_HEADER] + [_tracker_row(s) for s in ordered])


# ---------------------------------------------------------------------------
# build_conflict_tsv — beagle's sample_conflict.txt (ticket E3)
# ---------------------------------------------------------------------------


def _conflict_reason(sample: Sample) -> str:
    """The ``Conflict Reason`` cell: the sample's ingest validation
    errors (``Sample.errors``) joined with ``;`` — beagle's
    conflict-join convention (``";".join(conflicts)`` in
    ``tempo_patient.create_conflict_string``) — each part sanitized so
    tabs and newlines can never break the column grid."""
    parts = (_cell_str(error) for error in sample.errors)
    return ";".join(part for part in parts if part)


def build_conflict_tsv(db: Session) -> str:
    """beagle's ``sample_conflict.txt`` (ticket E3).

    Header is byte-equal to beagle's conflict header
    (``CONFLICT_HEADER``). Rows are every sample whose ``errors`` is
    non-null and non-empty — ingest validation failures, the spec's
    DECIDED v1 source for the conflict file (beagle's own semantics are
    cross-file metadata conflicts; Syzygy's closest concept is
    ``Sample.errors``). Scope is logic-agnostic (spec: conflict = all
    samples with errors, unlike the pairing-logic-scoped builders),
    ordered by ``cmo_sample_name`` (ties broken by id). ``Conflict
    Reason`` is the error strings joined with ``;`` with tabs/newlines
    flattened. Samples without errors (``errors`` null or empty) never
    appear; with no error rows the file is header-only.
    """
    samples = db.scalars(select(Sample)).all()
    ordered = sorted(
        (s for s in samples if s.errors),
        key=lambda s: (s.cmo_sample_name or "", s.id),
    )
    rows = [CONFLICT_HEADER]
    for sample in ordered:
        rows.append(
            (
                _cell_str(sample.cmo_sample_name),  # cmoSampleName
                _cell_str(sample.patient__cmo),  # cmoPatientId
                _cell_str(sample.primary_id),  # primaryId
                _class_cell(sample),  # sampleClass
                _cell_str(sample.run_mode),  # runMode
                _cell_str(sample.sample_type),  # sampleType
                _cell_str(sample.bait_set),  # baitSet
                _run_date(sample.sequencing_date),  # runDate
                _conflict_reason(sample),  # Conflict Reason
            )
        )
    return _tsv(rows)
