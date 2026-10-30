"""Request-level metadata helpers (Option A: no request table).

Request-level data (lab heads, emails, bicAnalysis, isCmoRequest,
validation status, importDate...) lives on the SAMPLES of the request,
inside each sample's ``additional_metadata`` under dedicated keys:

- ``requestMetadata``   — the request-level blob as SMILE sends it
- ``requestStatus``     — the request-level status dict
  (e.g. ``{"validationStatus": ..., "validationReport": ...}``)
- ``requestImportDate`` — the request-level importDate as sent (string)

A request is considered to exist iff at least one ``Sample`` row carries
its ``igoRequestId`` as ``request_id`` — the same derivation
``GET /v1/request/`` uses.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Sample, utcnow


def request_samples(db: Session, request_id: str) -> list[Sample]:
    """All samples whose ``request_id`` equals ``request_id``."""
    return list(db.scalars(select(Sample).where(Sample.request_id == request_id)).all())


def request_exists(db: Session, request_id: str) -> bool:
    """True iff at least one sample belongs to the request."""
    return len(request_samples(db, request_id)) > 0


def update_request_metadata(
    db: Session,
    request_id: str,
    *,
    request_metadata: Optional[dict] = None,
    status: Optional[dict] = None,
    import_date: Optional[str] = None,
) -> int:
    """Stamp request-level metadata onto every sample of the request.

    Only the provided (non-None) keys are touched: ``requestMetadata`` is
    replaced wholesale (a fresh blob), ``requestStatus`` and
    ``requestImportDate`` are set to the given values. The sample's own
    ``additional_metadata`` keys (qcReports, importDate,
    sampleMetadataList, ...) are preserved. ``modified_date`` is
    refreshed on every touched sample. Commits once.

    Returns the number of samples updated (0 when the request has no
    samples — the caller is expected to log/report).
    """
    samples = request_samples(db, request_id)
    if not samples:
        return 0

    updates: dict[str, object] = {}
    if request_metadata is not None:
        updates["requestMetadata"] = request_metadata
    if status is not None:
        updates["requestStatus"] = status
    if import_date is not None:
        updates["requestImportDate"] = import_date

    for sample in samples:
        meta = dict(sample.additional_metadata or {})
        meta.update(updates)
        sample.additional_metadata = meta
        sample.modified_date = utcnow()
    db.commit()
    return len(samples)
