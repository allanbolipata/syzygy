"""JetStream result publishing + outbox flush for the cohort listener.

The listener process owns the process's only NATS connection (the tempo
SmileClient). This module is the NATS-publishing half of the cohort flow:

* ``publish_result`` — publish one validation report to the result subject,
  using the ccs_cohort_utils EventHandler convention (the ``Nats-Msg-Id``
  header carries the cohort id) plus a ``Nats-Msg-Subject`` header for legacy
  parity.
* ``flush_pending`` — one cycle of the background outbox flusher (the task
  that runs it lives in ``run.py``): poll the app's publish-outbox for
  unpublished rows, republish each report, and mark the row published. The
  app stages an outbox row in the same transaction as it processes a cohort,
  so a fast-path publish that failed in ``smile_bridge`` is retried here.

It imports only nats-py + httpx (no Django, no smile_client), so the app stays
NATS-free and this module is importable in the test suite (``import
listener.nats_publisher``).
"""
import json
import logging
import os

import httpx

logger = logging.getLogger("nats_publisher")

# Result subject the report is published to. In dev, ONE compose variable
# (docker-compose.dev.yml) feeds both the app's nats_cohort_result_subject and
# this listener's NATS_RESULT_SUBJECT, so a rename touches one place. The
# default below is the same placeholder, pending the meeting agreement.
DEFAULT_RESULT_SUBJECT = "MDB_STREAM.consumers.tempo.cohort-result"

# App-internal publish-outbox endpoints (routers/publish_outbox.py).
PENDING_PATH = "/v1/publish-outbox/pending"
MARK_PUBLISHED_PATH = "/v1/publish-outbox/mark-published"
HTTP_TIMEOUT_S = 30.0
PENDING_LIMIT = 20


def result_subject() -> str:
    """The result subject: env NATS_RESULT_SUBJECT or the default placeholder."""
    return os.environ.get("NATS_RESULT_SUBJECT", DEFAULT_RESULT_SUBJECT)


def _token_headers() -> dict:
    """Same shared-secret header the ingest path uses (fail-closed on the app)."""
    return {"X-NATS-Token": os.environ.get("SYZYGY_NATS_TOKEN", "")}


async def publish_result(js_context, subject, cohort_id, report) -> None:
    """Publish a cohort validation report to JetStream.

    ``js_context`` is the tempo client's JetStream context (the publisher
    holds no NATS connection of its own). The report is the JSON payload; the
    ``Nats-Msg-Id`` header is the cohort id (the ccs_cohort_utils EventHandler
    idempotency convention) and ``Nats-Msg-Subject`` mirrors the subject for
    legacy parity. Raises on failure; callers (smile_bridge fast path, the
    flusher) catch and log.
    """
    if js_context is None:
        raise RuntimeError("publish_result called with no JetStream context")
    headers = {
        "Nats-Msg-Id": str(cohort_id),
        "Nats-Msg-Subject": subject,
    }
    payload = json.dumps(report).encode("utf-8")
    await js_context.publish(subject, payload=payload, headers=headers)


async def fetch_pending_rows(app_url: str, limit: int = PENDING_LIMIT) -> list:
    """Unpublished outbox rows from the app, oldest first."""
    url = app_url.rstrip("/") + PENDING_PATH
    response = httpx.get(
        url,
        params={"limit": limit},
        headers=_token_headers(),
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


async def mark_published(app_url: str, row_id) -> None:
    """Stamp one outbox row as published (published_at = now on the app)."""
    url = app_url.rstrip("/") + MARK_PUBLISHED_PATH
    response = httpx.post(
        url,
        json={"id": str(row_id)},
        headers=_token_headers(),
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()


async def flush_pending(js_context, app_url: str, limit: int = PENDING_LIMIT) -> int:
    """One outbox flush cycle: republish each pending row, then mark it done.

    For each row the app lists at GET /pending, publish the stored report to
    its subject and POST /mark-published. A publish failure is logged and the
    row is left unpublished (retried next cycle); a mark failure is logged the
    same way. A poll failure (app unreachable / bad status) returns 0 without
    touching any row. Returns the number of rows published this cycle.
    """
    try:
        rows = await fetch_pending_rows(app_url, limit)
    except httpx.HTTPError as exc:
        logger.error("outbox poll %s failed: %s", app_url, exc)
        return 0
    if not isinstance(rows, list):
        logger.error("outbox poll %s returned a non-list body", app_url)
        return 0

    published = 0
    for row in rows:
        row_id = row.get("id")
        cohort_id = row.get("cohort_id")
        subject = row.get("subject")
        try:
            report = row.get("report_json")
            if isinstance(report, str):
                report = json.loads(report)
            await publish_result(js_context, subject, cohort_id, report)
        except Exception as exc:  # noqa: BLE001 - log; the row is retried next cycle
            logger.error(
                "outbox publish failed for cohort %s (row %s): %s",
                cohort_id, row_id, exc,
            )
            continue
        try:
            await mark_published(app_url, row_id)
        except httpx.HTTPError as exc:
            logger.error("outbox mark-published failed for row %s: %s", row_id, exc)
            continue
        published += 1
    return published
