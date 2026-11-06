"""Pairing endpoints.

DMP tumors pair with their own DMP normals (same ``patient__dmp`` and gene
panel) automatically via ``POST /pairing/trigger-dmp`` under the DMP
pairing logic. DMP samples are also pairable manually, but only under the
other logics (e.g. ARGOS) — ``POST /pairing/trigger`` refuses the DMP
strategy, which is reserved for the automatic trigger.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from database import get_db, settings
from models import PairingLogic, PairingTable, Sample
from pairing_service import (
    HARMONY_LOGIC_NAME,
    count_unpaired,
    evaluate_logic,
    find_logic_by_name,
    resolve_pairing_logic,
    trigger_dmp_pairing,
    trigger_harmony_pairing,
    trigger_pairing,
    trigger_pairing_for_unpaired_tumors,
)
from pairing_strategies import get_pairing_strategy, strategies
from schemas import (
    DmpTriggerRead,
    EvaluateRead,
    HarmonyRow,
    HarmonyTriggerRead,
    PairingRead,
    PairingTriggerCreate,
    SampleRead,
    TumorOrNormal,
    TriggerAllRead,
    UnmatchedCountRead,
)


router = APIRouter(prefix="/pairing", tags=["pairings"])


# Sample attributes that can be filtered on either side of a pairing.
SAMPLE_FILTER_FIELDS = (
    "primary_id",
    "cmo_sample_name",
    "patient__cmo",
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
    patient__cmo: Optional[str] = None,
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
            ("patient__cmo", patient__cmo),
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


@router.get("/unmatched/", response_model=list[SampleRead])
def list_unmatched_dmp_samples(
    pairing_logic_name: str,
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    """DMP samples that have no latest pairing under the given logic.

    The DMP report lists every DMP sample; unpaired ones are shown with
    the opposite side's columns empty. The Pairs UI used to download the
    whole DMP sample population and diff it client-side against the
    pairings (hundreds of sequential requests against a ~280k-row table
    per load); the set difference is computed here in one query instead.
    Same per-logic, latest-only semantics as the pairing list itself.
    """
    logic = find_logic_by_name(db, pairing_logic_name)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    stmt = (
        select(Sample)
        .where(Sample.sample_type == "DMP", ~paired)
        .order_by(Sample.created_date.desc(), Sample.id.desc())
    )
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
    if strategies.get(pairing_logic.strategy_name) is strategies["dmp"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "The DMP strategy is automatic: DMP tumors pair with their "
                "own DMP normals via POST /pairing/trigger-dmp. Pair this "
                "sample under another logic (e.g. ARGOS) instead."
            ),
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
def trigger_all_pairings_endpoint(
    pairing_logic_name: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict:
    """Backfill pairings for every tumor that has no latest pairing yet.

    Occasional bulk operation (e.g. after a bulk ingest): each tumor's
    pairing logic is resolved from its assigned bait set, DMP tumors
    (paired automatically under the DMP logic) and tumors with no
    assigned or inactive logic are skipped and counted, and existing
    pairings are never touched.
    New pairings are recorded with ``trigger_type="automatic"``.

    With ``pairing_logic_name`` the backfill is scoped to that one
    non-DMP logic (counts cover that scope only); without the param the
    all-logics behavior is unchanged. DMP still runs via ``POST
    /trigger-dmp``.
    """
    return trigger_pairing_for_unpaired_tumors(db, logic_name=pairing_logic_name)


@router.post("/trigger-dmp", response_model=DmpTriggerRead)
def trigger_dmp_pairing_endpoint(db: Session = Depends(get_db)) -> dict:
    """Run automatic DMP pairing under the DMP pairing logic.

    Every unpaired DMP tumor is paired with its DMP normal (same
    ``patient__dmp`` and gene panel) via the DMP strategy. Existing
    pairings are never modified or replaced. DMP tumors without a
    ``patient__dmp`` value cannot be matched and are reported in
    ``missing_patient__dmp``.
    """
    return trigger_dmp_pairing(db)


# ---------------------------------------------------------------------------
# HARMONY: its own pairing space + 8-column normal-oriented export
# ---------------------------------------------------------------------------

#: The HARMONY output columns — exact headers, exact order (user-confirmed).
#: One row per (normal, tumor) pair; a normal repeats across rows (1-to-many).
HARMONY_COLUMNS = (
    "normal_sample",
    "tumor_sample",
    "normal_bamname",
    "normal_bam_path",
    "tumor_bamname",
    "tumor_bam_path",
    "patient",
    "consent",
)

#: Placeholder for the bam-path columns: syzygy stores no bam paths (future
#: DMP-payload field), so the column is emitted as an em dash.
HARMONY_BAM_PATH_PLACEHOLDER = "\u2014"


def _harmony_present_value(tumor: Optional[Sample], normal: Optional[Sample], field: str):
    """A field value from the present side, tumor preferred.

    On a paired row both sides are present, so the tumor's value wins; on a
    one-sided (unmatched) row the only present side supplies it. Falls
    through to the other side when the preferred side's value is null.
    """
    for side in (tumor, normal):
        if side is not None:
            value = getattr(side, field)
            if value is not None:
                return value
    return None


def _harmony_row(tumor: Optional[Sample], normal: Optional[Sample]) -> dict:
    """Build one 8-column HARMONY row from a (possibly one-sided) pair.

    ``normal_sample``/``tumor_sample`` <- ``primary_id``; ``normal_bamname``/
    ``tumor_bamname`` <- ``anon_id``; the ``*_bam_path`` columns are the em-dash
    placeholder for any present side and null for an absent side; ``patient``
    and ``consent`` come from the present side (tumor preferred).
    """
    return {
        "normal_sample": normal.primary_id if normal is not None else None,
        "tumor_sample": tumor.primary_id if tumor is not None else None,
        "normal_bamname": normal.anon_id if normal is not None else None,
        "normal_bam_path": HARMONY_BAM_PATH_PLACEHOLDER if normal is not None else None,
        "tumor_bamname": tumor.anon_id if tumor is not None else None,
        "tumor_bam_path": HARMONY_BAM_PATH_PLACEHOLDER if tumor is not None else None,
        "patient": _harmony_present_value(tumor, normal, "patient__dmp"),
        "consent": _harmony_present_value(tumor, normal, "consent"),
    }


def _harmony_csv_field(value) -> str:
    """Render one cell for the CSV export (nulls -> empty string)."""
    return "" if value is None else str(value)


def _harmony_csv_consent(consent) -> str:
    """CSV rendering of consent: None -> empty, False -> "false", True -> "true"."""
    if consent is None:
        return ""
    return "true" if consent is True else "false"


def _harmony_rows(db: Session, logic: PairingLogic) -> list[dict]:
    """All HARMONY rows: latest HARMONY pairings plus unmatched DMP samples.

    Paired rows: every latest ``pairing_table`` row under the HARMONY logic
    (tumor + normal embedded). Unmatched rows: DMP samples in the HARMONY
    scope (``sample_type == "DMP"``) with no latest HARMONY pairing — each
    becomes a one-sided row with the opposite side's columns null, mirroring
    the DMP unmatched-visibility convention. Ordered by normal ``primary_id``
    then tumor ``primary_id`` (absent sides sort first).
    """
    pairings = db.scalars(
        select(PairingTable)
        .options(
            joinedload(PairingTable.tumor),
            joinedload(PairingTable.normal),
        )
        .where(
            PairingTable.pairing_logic_id == logic.id,
            PairingTable.latest.is_(True),
        )
    ).all()
    rows = [_harmony_row(p.tumor, p.normal) for p in pairings]

    paired = exists(
        select(PairingTable.id).where(
            or_(
                PairingTable.tumor_id == Sample.id,
                PairingTable.normal_id == Sample.id,
            ),
            PairingTable.latest.is_(True),
            PairingTable.pairing_logic_id == logic.id,
        )
    )
    unmatched = db.scalars(
        select(Sample).where(Sample.sample_type == "DMP", ~paired)
    ).all()
    for sample in unmatched:
        if sample.tumor_or_normal == TumorOrNormal.NORMAL.value:
            rows.append(_harmony_row(None, sample))
        else:
            rows.append(_harmony_row(sample, None))

    rows.sort(key=lambda r: (r["normal_sample"] or "", r["tumor_sample"] or ""))
    return rows


def _harmony_csv_body(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HARMONY_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                _harmony_csv_field(r["normal_sample"]),
                _harmony_csv_field(r["tumor_sample"]),
                _harmony_csv_field(r["normal_bamname"]),
                _harmony_csv_field(r["normal_bam_path"]),
                _harmony_csv_field(r["tumor_bamname"]),
                _harmony_csv_field(r["tumor_bam_path"]),
                _harmony_csv_field(r["patient"]),
                _harmony_csv_consent(r["consent"]),
            ]
        )
    return buf.getvalue()


@router.get("/harmony/", response_model=list[HarmonyRow])
def get_harmony_pairings(
    format: Optional[str] = Query(
        None, pattern="^(json|csv)$",
        description='Return "json" (default: a list of rows) or "csv" '
        '(an 8-column, normal-oriented download).',
    ),
    db: Session = Depends(get_db),
):
    """The HARMONY pairing output, normal-oriented.

    Rows are the latest HARMONY-logic pairings (one row per normal/tumor
    pair) plus unmatched DMP samples in the HARMONY scope (no HARMONY pair
    yet), shown one-sided with the opposite side's columns null.

    ``?format=csv`` returns a ``text/csv`` attachment whose header is exactly
    the 8 HARMONY columns (``normal_sample, tumor_sample, normal_bamname,
    normal_bam_path, tumor_bamname, tumor_bam_path, patient, consent``), one
    row per (normal, tumor) pair, ordered by normal ``primary_id`` then tumor
    ``primary_id``. ``normal_bam_path``/``tumor_bam_path`` are em-dash
    placeholders (no bam paths in syzygy yet). ``consent`` renders as
    lowercase ``true``/``false`` (empty when unknown).
    """
    logic = find_logic_by_name(db, HARMONY_LOGIC_NAME)
    if logic is None:
        raise HTTPException(status_code=404, detail="Pairing logic not found")
    rows = _harmony_rows(db, logic)
    if format == "csv":
        return Response(
            content=_harmony_csv_body(rows),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="harmony_pairs.csv"'
            },
        )
    return rows


@router.post("/trigger-harmony", response_model=HarmonyTriggerRead)
def trigger_harmony_pairing_endpoint(db: Session = Depends(get_db)) -> dict:
    """Run automatic HARMONY pairing under the HARMONY pairing logic.

    HARMONY is its own pairing logic and pairing space, separate from the
    DMP logic (which may drift independently). For now it reuses the DMP
    strategy (DMP-group only, same ``patient__dmp`` and gene panel): every
    unpaired DMP tumor is paired with its best DMP normal, recorded under
    the HARMONY logic id. Existing pairings are never modified or replaced.
    DMP tumors without a ``patient__dmp`` value cannot be matched and are
    reported in ``missing_patient__dmp``.
    """
    return trigger_harmony_pairing(db)


@router.get("/unmatched-count/", response_model=UnmatchedCountRead)
def unmatched_count_endpoint(
    pairing_logic_name: str,
    db: Session = Depends(get_db),
) -> dict:
    """Per-logic unpaired count for the Pairing overview summary strip.

    DMP: mirrors ``GET /unmatched/`` (every unpaired DMP sample). Any
    other logic: the logic's unpaired tumor population — the same scope
    a backfill for that logic would consider. Read-only.
    """
    return count_unpaired(db, pairing_logic_name)


@router.get("/evaluate/", response_model=EvaluateRead)
def evaluate_pairing_endpoint(
    pairing_logic_name: str,
    db: Session = Depends(get_db),
) -> dict:
    """Dry-run: how many of this logic's unpaired tumors would pair.

    Strictly read-only — nothing is written. Over the logic's backfill
    scope, ``already_paired + would_pair + would_stay_unpaired ==
    scope_total``; ``missing_patient__dmp`` (DMP only) is a subset of
    ``would_stay_unpaired``.
    """
    return evaluate_logic(db, pairing_logic_name)
