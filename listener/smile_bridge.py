"""Bridge from the standalone smile-client listener to the syzygy app.

smile-client invokes this callback synchronously inside its consumer loop
for every consumed JetStream message. Each message is forwarded to the
app's internal ingest endpoint (/v1/ingest/nats), which dispatches it by
subject. Failures are logged, never swallowed silently: note that
smile-client acks the message regardless of the callback outcome, so a
persistently unreachable app drops messages (visible in these logs).
"""
import logging
import os

import httpx

logger = logging.getLogger("smile_bridge")

INGEST_PATH = "/v1/ingest/nats"
POST_TIMEOUT_S = 30.0


def ingest_callback(msg):
    """SmileClient callback (msg: SmileMessage with .subject and .data)."""
    app_url = os.environ["SYZYGY_APP_URL"]
    headers = {"X-NATS-Token": os.environ.get("SYZYGY_NATS_TOKEN", "")}
    url = app_url.rstrip("/") + INGEST_PATH
    try:
        response = httpx.post(
            url,
            json={"subject": msg.subject, "payload": msg.data},
            headers=headers,
            timeout=POST_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        logger.error("POST %s failed: %s", url, exc)
        return
    if response.is_success:
        logger.info("POST %s -> %s", msg.subject, response.status_code)
    else:
        logger.error(
            "POST %s -> %s: %s", msg.subject, response.status_code, response.text
        )
