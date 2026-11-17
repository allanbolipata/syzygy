from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, case, cast, func, or_, select, String
from sqlalchemy.orm import Session

from syzygy.auth import (
    LOGIC_ACCESS_DENIED_DETAIL,
    get_current_user,
    gating_allowed_logics,
)
from syzygy.bait_sets import allowed_logics_condition, logic_membership_condition
from syzygy.database import get_db
from syzygy.models import DMP_LOGIC_NAME, PairingLogic, Sample
from syzygy.pairing_service import resolve_filter_logic
from syzygy.schemas import PatientRead


router = APIRouter(prefix="/patient", tags=["patients"])

# Patient-level fields carried in sample additional_metadata. Sample-level
# keys (runId, flowCellId, platform, ...) are deliberately excluded.
PATIENT_META_KEYS = ("species", "sex", "oncotreeCode", "diagnosisDate")

# Advanced-filter query param -> additional_metadata key it matches.
PATIENT_META_FILTERS = {
    "species": "species",
    "sex": "sex",
    "oncotree_code": "oncotreeCode",
    "diagnosis_date": "diagnosisDate",
}


def _meta_text(key: str):
    """SQL text of ``additional_metadata[key]`` (dialect-agnostic JSON key
    extraction: ``json_extract`` on SQLite, ``->`` + cast on Postgres).
    Compiles to SQL NULL when ``additional_metadata`` is null or the key is
    absent — so null-safe ``IS NOT NULL`` / ILIKE checks behave consistently."""
    return cast(Sample.additional_metadata[key], String)


def _any_sample_matching(cond):
    """Group-level (HAVING) condition: at least one sample in the group
    satisfies ``cond``. Used instead of a row WHERE clause so an advanced
    filter decides *which groups* appear while every group's counts still
    cover ALL of its samples (Allan's 2026-11-11 decision: no logic
    scoping of counts)."""
    return func.sum(case((cond, 1), else_=0)) > 0


def _metadata_match_having(key: str, value: str):
    """Group HAVING clause matching the patient's *derived* metadata value
    (first non-null per key; tumor samples take priority — see
    ``list_patients``).

    Tumor-priority semantics, evaluated per group:
    - matches if any TUMOR sample's value for ``key`` partial-matches, or
    - no TUMOR sample carries ``key`` at all and some (NORMAL) sample's
      value partial-matches.

    ``value`` is a case-insensitive partial match.
    """
    like = f"%{value}%"
    meta = _meta_text(key)
    tumor_has_key = func.sum(
        case((and_(Sample.tumor_or_normal == "TUMOR", meta.is_not(None)), 1), else_=0)
    )
    return or_(
        _any_sample_matching(
            and_(Sample.tumor_or_normal == "TUMOR", meta.ilike(like))
        ),
        and_(tumor_has_key == 0, _any_sample_matching(meta.ilike(like))),
    )


def _patient_advanced_having(
    *,
    species: Optional[str],
    sex: Optional[str],
    oncotree_code: Optional[str],
    diagnosis_date: Optional[str],
    request_id: Optional[str],
    logic: Optional[PairingLogic] = None,
    gating=None,
):
    """HAVING clauses for the advanced patient filters (AND-combined)."""
    having = []
    meta_values = {
        "species": species,
        "sex": sex,
        "oncotree_code": oncotree_code,
        "diagnosis_date": diagnosis_date,
    }
    for param, value in meta_values.items():
        if value is not None:
            having.append(_metadata_match_having(PATIENT_META_FILTERS[param], value))
    if request_id is not None:
        having.append(_any_sample_matching(Sample.request_id.ilike(f"%{request_id}%")))
    if logic is not None:
        # Group-level pairing-logic filter: the patient is included when
        # >=1 of its samples belongs to the logic family; the group's
        # counts still cover ALL of its samples (Allan's 2026-11-11
        # all-samples counting decision, reaffirmed 2026-11-12).
        having.append(_any_sample_matching(logic_membership_condition(logic)))
    if gating is not None:
        # Layer B entitlement filter (ticket C3): the patient is included
        # when >=1 of its samples belongs to one of the principal's
        # allowed logics — same group-membership semantics as the
        # pairing_logic filter; the group's counts still cover ALL of
        # its samples.
        having.append(_any_sample_matching(gating))
    return having


def _apply_patient_filters(
    stmt,
    q,
    *,
    group_key,
    species: Optional[str] = None,
    sex: Optional[str] = None,
    oncotree_code: Optional[str] = None,
    diagnosis_date: Optional[str] = None,
    request_id: Optional[str] = None,
    logic: Optional[PairingLogic] = None,
    gating=None,
):
    """Filter clauses shared by the page query and the total query, so
    the reported total always reflects the currently active filter set.

    ``group_key`` is the column the groups key on — ``Sample.patient__cmo``
    for the CMO view, ``Sample.patient__dmp`` for the DMP-only view
    (ticket C4). ``q`` restricts rows on the group key (either column is
    group-level: every sample of a patient shares it, so restricting rows
    never shrinks a surviving group's counts). The advanced filters are
    group-level HAVING clauses on the unfiltered group rows — the group is
    included when at least one of its samples matches, but its aggregate
    counts still cover ALL of its samples.
    """
    if group_key is Sample.patient__cmo:
        stmt = stmt.where(Sample.patient__cmo.is_not(None))
        if q is not None:
            stmt = stmt.where(Sample.patient__cmo.ilike(f"%{q}%"))
    else:
        # DMP-only view: the PURE-DMP patients — patient__cmo IS NULL (the
        # patients invisible to the CMO view) and patient__dmp set (a
        # group with neither id is not a patient at all).
        stmt = stmt.where(
            and_(Sample.patient__cmo.is_(None), Sample.patient__dmp.is_not(None))
        )
        if q is not None:
            stmt = stmt.where(Sample.patient__dmp.ilike(f"%{q}%"))
    having = _patient_advanced_having(
        species=species,
        sex=sex,
        oncotree_code=oncotree_code,
        diagnosis_date=diagnosis_date,
        request_id=request_id,
        logic=logic,
        gating=gating,
    )
    if having:
        stmt = stmt.having(and_(*having))
    return stmt


def _patient_page(db: Session, limit, offset, *, q, group_key, **advanced):
    """One row per patient group: group id + sample/tumor/normal counts.

    Aggregation happens in SQL (GROUP BY) with limit/offset applied at the
    database level, so a paginated call never loads more than ``limit``
    patients' worth of group rows. Dialect-agnostic (Postgres + SQLite).
    """
    stmt = _apply_patient_filters(
        select(
            group_key,
            func.count().label("sample_count"),
            func.sum(case((Sample.tumor_or_normal == "TUMOR", 1), else_=0)).label(
                "tumor_count"
            ),
            func.sum(case((Sample.tumor_or_normal == "NORMAL", 1), else_=0)).label(
                "normal_count"
            ),
        ),
        q,
        group_key=group_key,
        **advanced,
    ).group_by(group_key).order_by(group_key)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).all()


def _patient_total(db: Session, *, q, group_key, **advanced) -> int:
    """Pre-pagination number of patient groups under the current filters
    (COUNT over the same filtered+HAVING GROUP BY that produces the page)."""
    groups = _apply_patient_filters(
        select(group_key), q, group_key=group_key, **advanced
    ).group_by(group_key)
    return db.scalar(select(func.count()).select_from(groups.subquery()))


@router.get("/", response_model=list[PatientRead])
def list_patients(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    dmp_only: bool = False,
    species: Optional[str] = None,
    sex: Optional[str] = None,
    oncotree_code: Optional[str] = None,
    diagnosis_date: Optional[str] = None,
    request_id: Optional[str] = None,
    pairing_logic: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    response: Response = None,
):
    """Patient groups derived from samples, sorted by the group id.

    Two views over the SAME row shape (``PatientRead``: group id +
    sample/tumor/normal counts + requests + derived metadata):

    - default (``dmp_only=false``): groups by ``patient__cmo`` (non-null),
      i.e. the patients visible today;
    - ``dmp_only=true`` (ticket C4): groups by ``patient__dmp`` where
      ``patient__cmo IS NULL`` — the pure-DMP patients that are invisible
      to the CMO view.

    ``q`` is a case-insensitive partial match on the view's group id
    (patient__cmo / patient__dmp respectively).

    ``metadata`` holds patient-level fields derived from the patient's
    sample ``additional_metadata`` (first non-null value per key; tumor
    samples take priority).

    Advanced filters select which patient groups appear; they never scope
    the groups' counts (every row's counts cover ALL of the patient's
    samples) — in both views:
    - ``species`` / ``sex`` / ``oncotree_code`` / ``diagnosis_date`` are
      case-insensitive partial matches on the *derived* metadata value
      (same key set as ``metadata``; tumor priority means a tumor value
      matching wins, and a NORMAL value only matches when no tumor sample
      carries the key at all).
    - ``request_id`` matches patients having at least one sample whose
      request_id partial-matches.
    - ``pairing_logic`` (e.g. ``ARGOS``/``TEMPO``/``ACCESS``/``DMP``)
      matches patients having at least one sample of that logic family
      (sample membership = registered bait_sets assignment; the DMP logic
      also owns bait_set-NULL ``sample_type`` ``'DMP'`` samples). Omitted
      / blank / ``ALL`` = no restriction; unknown name -> 400. Counts and
      ``requests`` stay all-samples under this filter too (2026-11-11
      counting decision).
    All filters combine with AND.

    Counts come from a SQL GROUP BY with DB-level limit/offset; the
    derived fields are then computed in Python over only the current
    page's samples.

    The ``X-Total-Count`` response header reports the total number of
    patient groups matching the current filters, independent of
    ``limit``/``offset`` (pre-pagination). ``response`` defaults to
    ``None`` so direct-call tests (repo convention) can pass a plain
    ``Response()`` to capture the header.

    Layer B entitlement filter (ticket C3): while ``logic_gating_enabled``
    is set, only patients with >=1 sample in one of the principal's
    ``allowed_logics`` are listed (group counts stay all-samples); with
    the flag off the listing is exactly today's.

    DMP-only view gate (ticket C4): while ``logic_gating_enabled`` is set,
    ``dmp_only=true`` is scoped to the DMP logic — a principal whose
    ``allowed_logics`` does not include ``DMP`` gets 403 with the Layer A
    stable ``LOGIC_ACCESS_DENIED_DETAIL`` (same as a logic-scoped read
    they may not see). With the flag off the view is open to everyone
    (dark-launch passthrough).
    """
    allowed = gating_allowed_logics(current_user)
    if dmp_only and allowed is not None and DMP_LOGIC_NAME not in allowed:
        raise HTTPException(status_code=403, detail=LOGIC_ACCESS_DENIED_DETAIL)
    gating = allowed_logics_condition(db, allowed) if allowed is not None else None
    group_key = Sample.patient__dmp if dmp_only else Sample.patient__cmo
    advanced = {
        "species": species,
        "sex": sex,
        "oncotree_code": oncotree_code,
        "diagnosis_date": diagnosis_date,
        "request_id": request_id,
        "logic": resolve_filter_logic(db, pairing_logic),
        "gating": gating,
    }
    rows = _patient_page(db, limit, offset, q=q, group_key=group_key, **advanced)

    by_group: dict[str, list[Sample]] = {}
    if rows:
        group_ids = [getattr(row, "patient__dmp" if dmp_only else "patient__cmo") for row in rows]
        samples = db.scalars(
            select(Sample).where(
                (Sample.patient__dmp if dmp_only else Sample.patient__cmo).in_(group_ids)
            )
        ).all()
        for s in samples:
            group_id = s.patient__dmp if dmp_only else s.patient__cmo
            by_group.setdefault(group_id, []).append(s)

    # Totals issued last: a bounded scalar COUNT (exactly 1 row) over the
    # same filtered GROUP BY — never the full group set.
    total = _patient_total(db, q=q, group_key=group_key, **advanced)
    if response is not None:
        response.headers["X-Total-Count"] = str(total)

    if not rows:
        return []

    patients: list[PatientRead] = []
    for row in rows:
        group_id = row.patient__dmp if dmp_only else row.patient__cmo
        rows_for_patient = by_group.get(group_id, [])
        tumors = [s for s in rows_for_patient if s.tumor_or_normal == "TUMOR"]
        normals = [s for s in rows_for_patient if s.tumor_or_normal == "NORMAL"]

        metadata: dict = {}
        for key in PATIENT_META_KEYS:
            for s in tumors + normals:
                value = (s.additional_metadata or {}).get(key)
                if value is not None:
                    metadata[key] = value
                    break

        patients.append(
            PatientRead(
                patient__cmo=None if dmp_only else row.patient__cmo,
                patient__dmp=row.patient__dmp if dmp_only else None,
                sample_count=row.sample_count,
                tumor_count=row.tumor_count,
                normal_count=row.normal_count,
                requests=sorted({s.request_id for s in rows_for_patient if s.request_id}),
                metadata=metadata,
            )
        )
    return patients
