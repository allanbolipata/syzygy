from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Sample
from schemas import PatientRead


router = APIRouter(prefix="/patient", tags=["patients"])

# Patient-level fields carried in sample additional_metadata. Sample-level
# keys (runId, flowCellId, platform, ...) are deliberately excluded.
PATIENT_META_KEYS = ("species", "sex", "oncotreeCode", "diagnosisDate")


@router.get("/", response_model=list[PatientRead])
def list_patients(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Patients derived from samples (``patient_id`` non-null), sorted by
    patient_id. ``q`` is a case-insensitive partial match on patient_id.

    ``metadata`` holds patient-level fields derived from the patient's
    sample ``additional_metadata`` (first non-null value per key; tumor
    samples take priority).
    """
    stmt = select(Sample).where(Sample.patient_id.is_not(None))
    if q is not None:
        stmt = stmt.where(Sample.patient_id.ilike(f"%{q}%"))
    samples = db.scalars(stmt).all()

    by_patient: dict[str, list[Sample]] = {}
    for s in samples:
        by_patient.setdefault(s.patient_id, []).append(s)

    patients: list[PatientRead] = []
    for patient_id in sorted(by_patient):
        rows = by_patient[patient_id]
        tumors = [s for s in rows if s.tumor_or_normal == "TUMOR"]
        normals = [s for s in rows if s.tumor_or_normal == "NORMAL"]

        metadata: dict = {}
        for key in PATIENT_META_KEYS:
            for s in tumors + normals:
                value = (s.additional_metadata or {}).get(key)
                if value is not None:
                    metadata[key] = value
                    break

        patients.append(
            PatientRead(
                patient_id=patient_id,
                sample_count=len(rows),
                tumor_count=len(tumors),
                normal_count=len(normals),
                requests=sorted({s.request_id for s in rows if s.request_id}),
                metadata=metadata,
            )
        )

    if offset is not None:
        patients = patients[offset:]
    if limit is not None:
        patients = patients[:limit]
    return patients
