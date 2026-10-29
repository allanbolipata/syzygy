from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from database import get_db
from models import Sample
from schemas import PatientRead


router = APIRouter(prefix="/patient", tags=["patients"])

# Patient-level fields carried in sample additional_metadata. Sample-level
# keys (runId, flowCellId, platform, ...) are deliberately excluded.
PATIENT_META_KEYS = ("species", "sex", "oncotreeCode", "diagnosisDate")


def _patient_page(db: Session, limit, offset, q):
    """One row per patient: id + sample/tumor/normal counts.

    Aggregation happens in SQL (GROUP BY) with limit/offset applied at the
    database level, so a paginated call never loads more than ``limit``
    patients' worth of group rows. Dialect-agnostic (Postgres + SQLite).
    """
    stmt = (
        select(
            Sample.patient__cmo,
            func.count().label("sample_count"),
            func.sum(case((Sample.tumor_or_normal == "TUMOR", 1), else_=0)).label(
                "tumor_count"
            ),
            func.sum(case((Sample.tumor_or_normal == "NORMAL", 1), else_=0)).label(
                "normal_count"
            ),
        )
        .where(Sample.patient__cmo.is_not(None))
        .group_by(Sample.patient__cmo)
    )
    if q is not None:
        stmt = stmt.where(Sample.patient__cmo.ilike(f"%{q}%"))
    stmt = stmt.order_by(Sample.patient__cmo)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).all()


@router.get("/", response_model=list[PatientRead])
def list_patients(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Patients derived from samples (``patient__cmo`` non-null), sorted by
    patient__cmo. ``q`` is a case-insensitive partial match on patient__cmo.

    ``metadata`` holds patient-level fields derived from the patient's
    sample ``additional_metadata`` (first non-null value per key; tumor
    samples take priority).

    Counts come from a SQL GROUP BY with DB-level limit/offset; the
    derived fields are then computed in Python over only the current
    page's samples.
    """
    rows = _patient_page(db, limit, offset, q)
    if not rows:
        return []

    patient__cmos = [row.patient__cmo for row in rows]
    samples = db.scalars(
        select(Sample).where(Sample.patient__cmo.in_(patient__cmos))
    ).all()

    by_patient: dict[str, list[Sample]] = {}
    for s in samples:
        by_patient.setdefault(s.patient__cmo, []).append(s)

    patients: list[PatientRead] = []
    for row in rows:
        rows_for_patient = by_patient.get(row.patient__cmo, [])
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
                patient__cmo=row.patient__cmo,
                sample_count=row.sample_count,
                tumor_count=row.tumor_count,
                normal_count=row.normal_count,
                requests=sorted({s.request_id for s in rows_for_patient if s.request_id}),
                metadata=metadata,
            )
        )
    return patients
