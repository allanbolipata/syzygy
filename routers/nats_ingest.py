"""Internal NATS ingest endpoint for the standalone smile-listener service.

Not part of the public API surface: no Keycloak auth — a shared-secret
X-NATS-Token header, enforced fail-closed. An empty
settings.nats_ingest_token rejects EVERY request (403); a configured
token must match exactly. The listener POSTs each consumed JetStream
message here as {"subject": ..., "payload": ...}; the message is
dispatched by subject to the per-event stub handlers (see nats_ingest.py).
"""
import hmac
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, settings
from nats_ingest import dispatch

logger = logging.getLogger(__name__)

# Fail-closed: a missing token means the endpoint is disabled, not open.
if not settings.nats_ingest_token:
    logger.warning(
        "NATS ingest endpoint is DISABLED: NATS_INGEST_TOKEN is not set; "
        "every request to /v1/ingest/nats will be rejected (403)"
    )

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
    if not settings.nats_ingest_token or not hmac.compare_digest(
        x_nats_token or "", settings.nats_ingest_token
    ):
        raise HTTPException(status_code=403, detail="invalid X-NATS-Token")
    result = dispatch(db, body.subject, body.payload)
    if result["status"] == "error":
        raise HTTPException(status_code=422, detail=result["detail"])
    response: Dict[str, Any] = {
        "subject": body.subject,
        "status": result["status"],
        "detail": result["detail"],
    }
    # Cohort handlers carry the validation report for the listener's
    # fast-path publish (listener/smile_bridge.py reads body["report"] on
    # 2xx); pass it through verbatim.
    if "report" in result:
        response["report"] = result["report"]
    return response
