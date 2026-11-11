"""Bridge from the standalone smile-client listener to the syzygy app.

smile-client invokes this callback synchronously inside its consumer loop
for every consumed JetStream message. Each message is forwarded to the
app's internal ingest endpoint (/v1/ingest/nats), which dispatches it by
subject. Failures are logged, never swallowed silently: note that
smile-client acks the message regardless of the callback outcome, so a
persistently unreachable app drops messages (visible in these logs).

Cohort fast-path publish: when the app's 2xx body carries a ``report`` key
(the cohort validation report), this bridge publishes it straight to the
result subject (NATS_RESULT_SUBJECT) — the same publish the background
outbox flusher performs. The JetStream context is supplied by ``run.py``
(via ``set_js_context_provider``) from the tempo client; the bridge holds no
NATS connection of its own. This is best-effort: a publish failure — or a
missing context while the tempo client reconnects — is logged and dropped,
because the app already staged the outbox row in the same transaction and the
flusher retries it. The ack is never affected: the publish is scheduled as a
task on the running loop, so the callback returns immediately.
"""
import asyncio
import logging
import os

import httpx

from nats_publisher import publish_result, result_subject

logger = logging.getLogger("smile_bridge")

INGEST_PATH = "/v1/ingest/nats"
POST_TIMEOUT_S = 30.0

# run.py installs a provider built from the tempo SmileClient. It returns the
# tempo client's live JetStream context, or None while it is not connected.
_js_context_provider = None
# References to in-flight fast-path publish tasks (kept so the loop does not
# garbage-collect a task the callback just scheduled).
_pending_publishes = set()


def set_js_context_provider(provider) -> None:
    """Register the callable run.py uses to hand the bridge a JetStream context.

    ``provider()`` is called at publish time and must return the tempo
    client's current JetStream context, or None when it is not connected.
    """
    global _js_context_provider
    _js_context_provider = provider


def _token_headers() -> dict:
    return {"X-NATS-Token": os.environ.get("SYZYGY_NATS_TOKEN", "")}


async def _do_publish(js_context, report, cohort_id) -> None:
    """Publish one report; log (never raise) on failure."""
    subject = result_subject()
    try:
        await publish_result(js_context, subject, cohort_id, report)
    except Exception as exc:  # noqa: BLE001 - log; the flusher retries
        logger.error(
            "report %s: fast-path publish to %s failed: %s (outbox will retry)",
            cohort_id, subject, exc,
        )
    else:
        logger.info("report %s: fast-path publish to %s", cohort_id, subject)


def _schedule_report_publish(report: dict) -> None:
    """Schedule a best-effort publish of ``report`` on the running loop.

    Called from the (synchronous) consumer callback, so a running event loop
    is always present. Never raises: a missing JetStream context, or any
    scheduling error, is logged and dropped — the app-staged outbox row is
    retried by the flusher, and the ack must never depend on this.
    """
    cohort_id = report.get("cohortId")
    try:
        js_context = _js_context_provider() if _js_context_provider is not None else None
        if js_context is None:
            logger.info(
                "report %s: no JetStream context yet; outbox flusher will retry",
                cohort_id,
            )
            return
        task = asyncio.get_running_loop().create_task(
            _do_publish(js_context, report, cohort_id)
        )
    except Exception:  # noqa: BLE001 - the ack must never change
        logger.exception("report %s: failed to schedule fast-path publish", cohort_id)
        return
    _pending_publishes.add(task)
    task.add_done_callback(_pending_publishes.discard)


def _maybe_publish_report(response) -> None:
    """If a 2xx body carries a ``report`` object, fast-path publish it."""
    try:
        body = response.json()
    except (ValueError, httpx.DecodingError):
        return
    if not isinstance(body, dict):
        return
    report = body.get("report")
    if isinstance(report, dict):
        _schedule_report_publish(report)


def ingest_callback(msg):
    """SmileClient callback (msg: SmileMessage with .subject and .data)."""
    app_url = os.environ["SYZYGY_APP_URL"]
    headers = _token_headers()
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
        _maybe_publish_report(response)
    else:
        logger.error(
            "POST %s -> %s: %s", msg.subject, response.status_code, response.text
        )
