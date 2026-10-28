"""Internal NATS ingest endpoint for the standalone smile-listener service.

Not part of the public API surface: no Keycloak auth — a shared-secret
X-NATS-Token header when settings.nats_ingest_token is configured (an
empty token allows unauthenticated ingest and logs a warning, the dev
default). The listener POSTs each consumed JetStream message here as
{"subject": ..., "payload": ...}; the message is dispatched by subject
to the per-event stub handlers (see nats_ingest.py).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, settings
from nats_ingest import dispatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["nats-ingest"])


class NatsIngestIn(BaseModel):
    subject: str
    # JSON-decoded NATS message body. New-request and sample-update carry a
    # JSON object; request-update carries a JSON array (beagle's own
    # smile_callback branches on isinstance(data, list)), so the payload is
    # dict-or-list, not dict alone.
    payload: Dict[str, Any] | List[Any]


@router.post("/nats")
def ingest_nats(
    body: NatsIngestIn,
    db: Session = Depends(get_db),
    x_nats_token: Optional[str] = Header(default=None, alias="X-NATS-Token"),
) -> Dict[str, Any]:
    """Receive one NATS message from the smile-listener and dispatch it."""
    if settings.nats_ingest_token:
        if x_nats_token != settings.nats_ingest_token:
            raise HTTPException(status_code=403, detail="invalid X-NATS-Token")
    else:
        logger.warning(
            "NATS ingest: no token configured; allowing unauthenticated ingest"
        )
    result = dispatch(db, body.subject, body.payload)
    if result["status"] == "error":
        raise HTTPException(status_code=422, detail=result["detail"])
    return {
        "subject": body.subject,
        "status": result["status"],
        "detail": result["detail"],
    }
