"""Internal publish-outbox endpoints for the smile-listener's flusher.

Not part of the public API surface: no Keycloak auth — a shared-secret
X-NATS-Token header, enforced fail-closed exactly like the NATS ingest
endpoint (routers/nats_ingest.py); the flusher sends the same token.
The listener acks every inbound NATS message unconditionally, so a
publish that fails on the listener side is recorded as a row in the
``pending_nats_publishes`` outbox (models.PendingNatsPublish). The
flusher drains it through this endpoint: GET /pending lists unpublished
rows oldest-first, and POST /mark-published stamps a row once its
republish lands.
"""
import hmac
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db, settings
from models import PendingNatsPublish, utcnow

logger = logging.getLogger(__name__)

# Fail-closed: a missing token means the endpoint is disabled, not open.
if not settings.nats_ingest_token:
    logger.warning(
        "Publish-outbox endpoint is DISABLED: NATS_INGEST_TOKEN is not set; "
        "every request to /v1/publish-outbox will be rejected (403)"
    )

router = APIRouter(prefix="/publish-outbox", tags=["publish-outbox"])


def _check_token(x_nats_token: Optional[str]) -> None:
    """Fail-closed token check, mirroring routers/nats_ingest.py."""
    if not settings.nats_ingest_token or not hmac.compare_digest(
        x_nats_token or "", settings.nats_ingest_token
    ):
        raise HTTPException(status_code=403, detail="invalid X-NATS-Token")


class MarkPublishedIn(BaseModel):
    id: uuid.UUID


@router.get("/pending")
def list_pending(
    db: Session = Depends(get_db),
    limit: int = Query(default=20),
    x_nats_token: Optional[str] = Header(default=None, alias="X-NATS-Token"),
) -> List[Dict[str, Any]]:
    """Unpublished outbox rows, oldest first, for the flusher to drain."""
    _check_token(x_nats_token)
    rows = db.scalars(
        select(PendingNatsPublish)
        .where(PendingNatsPublish.published_at.is_(None))
        .order_by(PendingNatsPublish.created_at, PendingNatsPublish.id)
        .limit(limit)
    ).all()
    return [
        {
            "id": str(row.id),
            "cohort_id": row.cohort_id,
            "subject": row.subject,
            "report_json": row.report_json,
            "created_at": row.created_at.isoformat()
            if row.created_at is not None
            else None,
        }
        for row in rows
    ]


@router.post("/mark-published")
def mark_published(
    body: MarkPublishedIn,
    db: Session = Depends(get_db),
    x_nats_token: Optional[str] = Header(default=None, alias="X-NATS-Token"),
) -> Dict[str, Any]:
    """Stamp one outbox row as published (published_at = now)."""
    _check_token(x_nats_token)
    row = db.get(PendingNatsPublish, body.id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown outbox row")
    row.published_at = utcnow()
    db.commit()
    return {"id": str(row.id), "published_at": row.published_at.isoformat()}
