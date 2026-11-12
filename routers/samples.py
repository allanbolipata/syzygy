from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bait_sets import logic_membership_condition, resolve_bait_set_logic
from database import get_db, settings
from models import PairingLogic, Sample
from pairing_service import (
    MAX_OTHER_MATCHES_LIMIT,
    possible_matches,
    resolve_filter_logic,
    resolve_pairing_logic,
    trigger_pairing,
    trigger_pairing_for_normal,
)
from schemas import PossibleMatchesRead, SampleCreate, SampleRead, TumorOrNormal


router = APIRouter(prefix="/sample", tags=["samples"])


@router.post("/", response_model=SampleRead, status_code=201)
def create_sample(payload: SampleCreate, db: Session = Depends(get_db)):
    pairing_logic = resolve_pairing_logic(
        db,
        payload.pairing_logic_id,
        payload.pairing_logic_name,
        settings.default_pairing_logic_name,
    )

    sample_data = payload.model_dump(exclude={"pairing_logic_id", "pairing_logic_name"})
    sample_data["tumor_or_normal"] = payload.tumor_or_normal.value
    sample = Sample(**sample_data)
    db.add(sample)
    db.commit()
    db.refresh(sample)

    if sample.tumor_or_normal == TumorOrNormal.TUMOR:
        trigger_pairing(db, sample, pairing_logic, "automatic")
    elif sample.tumor_or_normal == TumorOrNormal.NORMAL:
        trigger_pairing_for_normal(db, sample, pairing_logic)

    return sample


def _samples_filtered_query(
    *,
    q: Optional[str],
    primary_id: Optional[str],
    cmo_sample_name: Optional[str],
    patient__cmo: Optional[str],
    request_id: Optional[str],
    sample_type: Optional[str],
    gene_panel: Optional[str],
    bait_set: Optional[str],
    run_mode: Optional[str],
    file_group: Optional[str],
    tumor_or_normal: Optional[TumorOrNormal],
    no_patient: Optional[bool],
    logic: Optional[PairingLogic] = None,
):
    """Filtered ``SELECT`` on ``Sample`` — no ordering or pagination.

    Shared by the page query and the ``X-Total-Count`` query so the
    reported total always reflects the currently active filter set.
    """
    stmt = select(Sample)
    if q is not None:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Sample.primary_id.ilike(like),
                Sample.cmo_sample_name.ilike(like),
                Sample.patient__cmo.ilike(like),
                Sample.request_id.ilike(like),
            )
        )
    for field, value in {
        "primary_id": primary_id,
        "cmo_sample_name": cmo_sample_name,
        "patient__cmo": patient__cmo,
        "request_id": request_id,
        "sample_type": sample_type,
        "gene_panel": gene_panel,
        "bait_set": bait_set,
        "run_mode": run_mode,
        "file_group": file_group,
    }.items():
        if value is not None:
            stmt = stmt.where(getattr(Sample, field).ilike(f"%{value}%"))
    if no_patient:
        stmt = stmt.where(Sample.patient__cmo.is_(None))
    if tumor_or_normal is not None:
        stmt = stmt.where(Sample.tumor_or_normal == tumor_or_normal.value)
    if logic is not None:
        # pairing-logic family membership (SQL-side bait_sets join + the
        # DMP null-bait_set rule; see bait_sets.logic_membership_condition).
        stmt = stmt.where(logic_membership_condition(logic))
    return stmt


@router.get("/", response_model=list[SampleRead])
def list_samples(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    primary_id: Optional[str] = None,
    cmo_sample_name: Optional[str] = None,
    patient__cmo: Optional[str] = None,
    request_id: Optional[str] = None,
    sample_type: Optional[str] = None,
    gene_panel: Optional[str] = None,
    bait_set: Optional[str] = None,
    run_mode: Optional[str] = None,
    file_group: Optional[str] = None,
    tumor_or_normal: Optional[TumorOrNormal] = None,
    no_patient: Optional[bool] = None,
    pairing_logic: Optional[str] = None,
    db: Session = Depends(get_db),
    response: Response = None,
):
    """List samples, newest first, with optional field filters.

    ``q`` matches any of ``primary_id``, ``cmo_sample_name``,
    ``patient__cmo``, or ``request_id``. The per-field filters are
    case-insensitive substring matches; ``tumor_or_normal`` is exact;
    ``no_patient`` restricts to samples with a null patient__cmo (e.g.
    pooled normals). All filters combine with AND.

    ``pairing_logic`` restricts to samples belonging to one pairing-logic
    family (e.g. ``ARGOS``): the sample's ``bait_set`` must match a
    registered ``bait_sets`` assignment for that logic (case-insensitive),
    and the ``DMP`` logic additionally owns ``bait_set``-NULL samples of
    ``sample_type`` ``'DMP'``. Samples whose bait set carries no
    registered assignment belong to no family and only appear when the
    filter is omitted. Omitted / blank / ``ALL`` = no restriction. An
    unknown logic name is a 400. Listing filters never affect pairing
    candidate scope (``possible-matches`` / pairing triggers stay
    logic-agnostic).

    The ``X-Total-Count`` response header reports the total number of
    rows matching the current filters, independent of ``limit``/
    ``offset`` (pre-pagination). ``response`` defaults to ``None`` so
    direct-call tests (repo convention) can pass a plain ``Response()``
    to capture the header.
    """
    logic = resolve_filter_logic(db, pairing_logic)
    base = _samples_filtered_query(
        q=q,
        primary_id=primary_id,
        cmo_sample_name=cmo_sample_name,
        patient__cmo=patient__cmo,
        request_id=request_id,
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=bait_set,
        run_mode=run_mode,
        file_group=file_group,
        tumor_or_normal=tumor_or_normal,
        no_patient=no_patient,
        logic=logic,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    if response is not None:
        response.headers["X-Total-Count"] = str(total)
    stmt = base.order_by(Sample.created_date.desc(), Sample.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.get("/lookup", response_model=list[SampleRead])
def lookup_samples_endpoint(
    q: str = Query(..., min_length=1),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Case-insensitive partial-match lookup on ``primary_id`` and
    ``cmo_sample_name`` only — the two sample identifiers used in manual
    pairing workflows (the "Try Pairing" tab). Newest first."""
    if limit is not None and (limit < 1 or limit > 10000):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 10000")
    if offset is not None and offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    like = f"%{q.strip()}%"
    if not like.strip("%"):
        raise HTTPException(status_code=400, detail="q must not be blank")
    stmt = (
        select(Sample)
        .where(or_(Sample.primary_id.ilike(like), Sample.cmo_sample_name.ilike(like)))
        .order_by(Sample.created_date.desc(), Sample.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


@router.get("/{sample_id}/possible-matches", response_model=PossibleMatchesRead)
def possible_matches_endpoint(
    sample_id: UUID,
    pairing_logic_id: Optional[UUID] = None,
    pairing_logic_name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """The sample's current match and all potential matches.

    Candidates are exactly the opposite-type samples this strategy's
    cascade can reach (its hard keys, compared case-insensitively, plus
    extra tiers such as argos's pooled normals). Each candidate reports
    its cascade tier, whether it passes every matching key (``matched``),
    and the reasons it does not (``mismatched_keys``). For a tumor input,
    ``would_be_selected`` marks the candidate the strategy's cascade
    would pick.

    ``other_matches`` is a bounded page (``limit``/``offset``; default
    ``limit`` is 500, max 5000) of that candidate scope; the response's
    ``other_matches_total`` / ``other_matches_matched`` /
    ``other_matches_tiers`` / ``other_matches_breakdown`` fields describe
    the full scope regardless of the page. Supports manual pairing (e.g.
    pinning a DMP normal via POST /pairing/trigger).
    """
    sample = db.get(Sample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    logic = None
    if pairing_logic_id is None and pairing_logic_name is None:
        # No explicit logic: use the bait set's assigned primary logic.
        logic = resolve_bait_set_logic(db, sample.bait_set)
    if logic is None:
        logic = resolve_pairing_logic(
            db,
            pairing_logic_id,
            pairing_logic_name,
            settings.default_pairing_logic_name,
        )
    if logic is None:
        raise HTTPException(status_code=400, detail="No active pairing logic found")
    if limit is not None and (limit < 1 or limit > MAX_OTHER_MATCHES_LIMIT):
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {MAX_OTHER_MATCHES_LIMIT}",
        )
    if offset is not None and offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    return possible_matches(db, sample, logic, limit=limit, offset=offset)
