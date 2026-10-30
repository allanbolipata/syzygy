"""Live end-to-end test for the cohort chain over the dev stack (T9).

Publishes a valid, pairable fixture cohort to the live dev NATS server
(``docker-compose.dev.yml`` stack) on ``MDB_STREAM.consumers.tempo.
new-cohort-submit`` and follows the full production path:

    dev NATS -> smile-listener (tempo durable) -> app /v1/ingest/nats
    (cohort_submit_handler: validate T3 -> classify T5 -> CRF write T4)
    -> report fast-path publish back to the cohort-result subject
    -> CRF file under the host bind mount ``crf_output_dev/``.

Follows the live-NATS pattern of ``tests/test_nats_ingest.py``: skipped
when the dev stack is not up (no NATS with MDB_STREAM at 127.0.0.1:4222,
or no dev Postgres at 127.0.0.1:5435).

Setup (G8): the fixture cohort is seeded into the dev Postgres with
direct SQL inserts (the app API is in-network only at ``app:8123``). A
run-unique 5-digit token names everything — cohortId, patient CMO,
sample ids, bait set, pairing logic — so re-runs are safe and the
tumor's only eligible normal (default-strategy hard keys: patient,
gene panel, bait set) is the one this run seeded. Seeded DB rows are
removed in a finally block; the generated CRF file is left in
``crf_output_dev/`` as evidence (it is uniquely named per run).

Result subject: env ``NATS_RESULT_SUBJECT`` (default
``MDB_STREAM.consumers.tempo.cohort-result``) — the same env var the
listener uses, so a rename flows through one place.
"""
import asyncio
import json
import logging
import os
import random
import socket
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Host-side CRF dir (the T6 bind mount); the in-container path
# /app/crf_output_dev is never visible to a host-side test.
CRF_DIR_HOST = REPO_ROOT / "crf_output_dev"

LIVE_NATS_URL = "nats://127.0.0.1:4222"
DEV_DB_PORT = 5435
# Override with SYZYGY_DEV_DB_URL if the dev stack uses different creds.
DEV_DB_URL = os.environ.get(
    "SYZYGY_DEV_DB_URL",
    "postgresql+psycopg2://app:app@127.0.0.1:%d/app" % DEV_DB_PORT,
)

SUBMIT_SUBJECT = "MDB_STREAM.consumers.tempo.new-cohort-submit"
RESULT_SUBJECT = os.environ.get(
    "NATS_RESULT_SUBJECT", "MDB_STREAM.consumers.tempo.cohort-result"
)
RESULT_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Dev-stack probe (skipif, evaluated at collection time)
# ---------------------------------------------------------------------------


def _pg_up(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dev_stack_available() -> bool:
    """True if the dev stack is up: NATS answering at 127.0.0.1:4222 with
    the MDB_STREAM stream present, and the dev Postgres answering at
    127.0.0.1:5435."""
    if not _pg_up("127.0.0.1", DEV_DB_PORT):
        return False
    try:
        import nats

        async def probe() -> bool:
            try:
                # nats-py retries a refused port in _select_next_server's
                # own loop (re-queueing the server every ~2s, independent
                # of allow_reconnect), so bound the attempt: a down stack
                # must skip quickly, not stall collection.
                nc = await asyncio.wait_for(
                    nats.connect(servers=[LIVE_NATS_URL],
                                 allow_reconnect=False,
                                 max_reconnect_attempts=1),
                    timeout=15.0,
                )
            except Exception:
                return False
            try:
                await nc.jetstream().stream_info("MDB_STREAM")
                return True
            except Exception:
                return False
            finally:
                await nc.close()

        return asyncio.run(probe())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fixture seeding / cleanup (direct SQL against the dev Postgres)
# ---------------------------------------------------------------------------


def _pick_run() -> int:
    """A run-unique 5-digit token with no collision in the dev samples
    table (the payload's primaryId must match the schema pattern
    ``^\\d{5}(?:_[A-Z]*)?(?:_\\d+){0,6}$``)."""
    engine = create_engine(DEV_DB_URL)
    try:
        for _ in range(10):
            run = random.randint(10000, 99999)
            tumor_cmo = f"C-{run}-X001-d01"
            normal_cmo = f"C-{run}-N001-d01"
            with engine.connect() as conn:
                hit = conn.execute(
                    text(
                        "select 1 from samples "
                        "where cmo_sample_name in (:tc, :nc) "
                        "or primary_id in (:tp, :np) limit 1"
                    ),
                    {
                        "tc": tumor_cmo,
                        "nc": normal_cmo,
                        "tp": f"s-{run}_A",
                        "np": f"s-{run}_N",
                    },
                ).first()
                if hit is None:
                    return run
        raise AssertionError(
            "could not find 10 collision-free run tokens in the dev samples table"
        )
    finally:
        engine.dispose()


def _seed_cohort(run: int) -> dict:
    """Seed a valid, pairable cohort into the dev Postgres.

    Returns the row ids (plus the fixture ids) for cleanup. The tumor's
    only eligible normal under the seeded pairing logic (default
    strategy hard keys: patient__cmo + gene_panel + bait_set, all
    run-unique) is the normal seeded here, so classification pairs them.
    """
    cohort_id = f"E2E-{run}"
    logic_name = f"e2e-logic-{run}"
    bait = f"e2e-bait-{run}"
    panel = "E2E-PANEL"
    patient = f"C-{run}"
    tumor_id, normal_id, logic_id, bait_id = (uuid.uuid4() for _ in range(4))
    fixture = {
        "cohort_id": cohort_id,
        "logic_name": logic_name,
        "tumor_cmo": f"C-{run}-X001-d01",
        "normal_cmo": f"C-{run}-N001-d01",
        "ids": {
            "logic": logic_id,
            "bait": bait_id,
            "tumor": tumor_id,
            "normal": normal_id,
        },
    }
    engine = create_engine(DEV_DB_URL)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into pairing_logic "
                    "(id, name, description, active, strategy_name, config, created_date) "
                    "values (:id, :name, 'T9 live e2e fixture', true, 'default', null, now())"
                ),
                {"id": str(logic_id), "name": logic_name},
            )
            conn.execute(
                text(
                    "insert into bait_sets "
                    "(id, bait_set, pairing_logic_id, created_date, modified_date) "
                    "values (:id, :bait, :logic_id, now(), now())"
                ),
                {"id": str(bait_id), "bait": bait, "logic_id": str(logic_id)},
            )
            for sid, pid, cmo, role in (
                (tumor_id, f"s-{run}_A", fixture["tumor_cmo"], "TUMOR"),
                (normal_id, f"s-{run}_N", fixture["normal_cmo"], "NORMAL"),
            ):
                conn.execute(
                    text(
                        "insert into samples "
                        "(id, primary_id, cmo_sample_name, patient__cmo, sample_type, "
                        " gene_panel, bait_set, tumor_or_normal, created_date, modified_date) "
                        "values (:id, :pid, :cmo, :patient, 'tissue', :panel, :bait, "
                        "        :role, now(), now())"
                    ),
                    {
                        "id": str(sid),
                        "pid": pid,
                        "cmo": cmo,
                        "patient": patient,
                        "panel": panel,
                        "bait": bait,
                        "role": role,
                    },
                )
    finally:
        engine.dispose()
    return fixture


def _cleanup(fixture: dict) -> None:
    """Remove this run's seeded rows (best-effort; the ids are run-unique)."""
    ids = fixture["ids"]
    try:
        engine = create_engine(DEV_DB_URL)
        with engine.begin() as conn:
            conn.execute(
                text("delete from pairing_table where pairing_logic_id = :id"),
                {"id": str(ids["logic"])},
            )
            # The app stages the report here on every outcome; the dev
            # listener's flusher is off by default, so remove it explicitly.
            conn.execute(
                text("delete from pending_nats_publishes where cohort_id = :id"),
                {"id": fixture["cohort_id"]},
            )
            conn.execute(
                text("delete from samples where id in (:t, :n)"),
                {"t": str(ids["tumor"]), "n": str(ids["normal"])},
            )
            conn.execute(text("delete from bait_sets where id = :id"), {"id": str(ids["bait"])})
            conn.execute(text("delete from pairing_logic where id = :id"), {"id": str(ids["logic"])})
        engine.dispose()
    except Exception:
        logger.exception("e2e cleanup failed for cohort %s", fixture["cohort_id"])


# ---------------------------------------------------------------------------
# Live round trip
# ---------------------------------------------------------------------------


def _publish_and_wait(fixture: dict) -> dict:
    """Publish the cohort submit message and wait for its report on the
    result subject. Returns the report dict (matched on cohortId — other
    runs may publish on the same subject concurrently)."""
    import nats

    cohort_id = fixture["cohort_id"]
    payload = {
        "cohortId": cohort_id,
        "endUsers": ["analyst@example.org"],
        "pmUsers": ["pm@example.org"],
        "projectTitle": "T9 live e2e cohort",
        "samples": [{"primaryId": f"{cohort_id.rsplit('-', 1)[1]}_A", "cmoId": fixture["tumor_cmo"]}],
    }

    async def run() -> dict:
        # Fail fast on a dropped NATS (the probe just confirmed it up):
        # nats-py would otherwise retry the dead port for ~2 min.
        nc = await asyncio.wait_for(
            nats.connect(servers=[LIVE_NATS_URL],
                         allow_reconnect=False,
                         max_reconnect_attempts=1),
            timeout=15.0,
        )
        sub = None
        try:
            js = nc.jetstream()
            # Subscribe BEFORE publishing: the JetStream stream retains
            # the result even if the fast path wins the race.
            sub = await js.pull_subscribe(RESULT_SUBJECT, stream="MDB_STREAM")
            ack = await js.publish(SUBMIT_SUBJECT, json.dumps(payload).encode("utf-8"))
            assert ack.stream == "MDB_STREAM", f"published to {ack.stream!r}"
            deadline = time.monotonic() + RESULT_TIMEOUT_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"no cohort result for {cohort_id} on {RESULT_SUBJECT} "
                        f"within {RESULT_TIMEOUT_S:.0f}s (listener or app not processing?)"
                    )
                # nats-py's fetch RAISES TimeoutError when the window
                # expires empty (it does not return []), so swallow the
                # timeout and re-fetch until the deadline. batch=1: the
                # result is matched per message on cohortId, and _fetch_one
                # re-drains the pending queue first, so a message delivered
                # mid-timeout is picked up on the next iteration.
                try:
                    msgs = await sub.fetch(batch=1, timeout=min(5.0, remaining))
                except nats.errors.TimeoutError:
                    continue
                for msg in msgs:
                    body = json.loads(msg.data)
                    if isinstance(body, dict) and body.get("cohortId") == cohort_id:
                        await msg.ack()
                        return body
        finally:
            if sub is not None:
                await sub.unsubscribe()
            await nc.close()

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _dev_stack_available(),
    reason="dev stack (docker-compose.dev.yml) not up: "
    "no NATS/MDB_STREAM at 127.0.0.1:4222 or no dev Postgres at 127.0.0.1:5435",
)
def test_cohort_submit_live_e2e():
    """Full production path over live dev NATS: publish a valid, pairable
    fixture cohort, get the pass report on the result subject, and find
    the CRF under the host bind mount."""
    run = _pick_run()
    fixture = _seed_cohort(run)
    cohort_id = fixture["cohort_id"]
    logger.info("e2e cohort %s: run token %d", cohort_id, run)
    try:
        report = _publish_and_wait(fixture)

        # Report shape (the validator schema's required fields).
        assert report["cohortId"] == cohort_id
        assert report["jsonSchemaValidated"] is True
        assert report["passesCheck"] is True

        # CRF at the HOST path (the in-container path is never visible here).
        crf = CRF_DIR_HOST / f"{cohort_id}.cohort.txt"
        assert crf.is_file(), f"CRF not found at host path {crf}"
        # The resolved pair round-tripped: one data row, tumor -> normal.
        lines = [line for line in crf.read_text().splitlines() if not line.startswith("#")]
        assert lines == [f"{fixture['tumor_cmo']}\t{fixture['normal_cmo']}"]
    finally:
        _cleanup(fixture)
