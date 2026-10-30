"""Integration test: mock SMILE publish -> smile-listener -> app ingest.

End-to-end against the live dev stack. Skips only when the stack is
genuinely down:

* a NATS server must answer at 127.0.0.1:4222 with the ``MDB_STREAM``
  JetStream stream present (checked via ``jetstream.stream_info``), and
* the dev DB must be reachable at 127.0.0.1:5435 (user ``app``, db
  ``app``).

When available, JetStream-publishes a ``cmo-new-request`` payload with a
FRESH primary_id (unique per run), polls the dev DB until the Sample row
lands (timeout ~30s) and asserts the mapped fields plus the
request-level metadata. The synthetic patient id guarantees no matching
normal exists, so the test leaves no pairing side effects behind.

Bring the stack up with:

    podman compose -f docker-compose.yml -f docker-compose.dev.yml up -d
"""
import asyncio
import json
import time

import pytest

NATS_URL = "nats://127.0.0.1:4222"
DB_URL = "postgresql://app:app@127.0.0.1:5435/app"
STREAM = "MDB_STREAM"
NEW_REQUEST_SUBJECT = "MDB_STREAM.server.cpt-gateway.cmo-new-request"
INGEST_TIMEOUT = 30.0


def _nats_client_available():
    try:
        import nats  # noqa: F401
        return True
    except ImportError:
        return False


def _nats_reachable():
    """A NATS server answers at NATS_URL and the MDB_STREAM stream exists."""
    try:
        import nats

        async def check():
            nc = await nats.connect(servers=[NATS_URL])
            try:
                js = nc.jetstream()
                info = await js.stream_info(STREAM)
                return info.config.name == STREAM
            finally:
                await nc.close()

        return asyncio.run(check())
    except Exception:
        return False


def _db_reachable():
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(DB_URL, connect_timeout=3)
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return True


def _skip_reason():
    if not _nats_client_available():
        return "nats-py is not installed in this environment"
    if not _nats_reachable():
        return (
            f"NATS not reachable at {NATS_URL} (or the "
            f"{STREAM} stream is missing) — dev stack is down"
        )
    if not _db_reachable():
        return f"dev DB not reachable at {DB_URL} — dev stack is down"
    return None


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


def test_new_request_ingest_via_nats():
    import nats
    import psycopg2

    request_id = "99907_E2EIT"
    stamp = int(time.time() * 1000)
    primary_id = f"E2EIT_{stamp}"
    patient__cmo = f"C-E2EIT-{stamp}"
    payload = {
        "igoRequestId": request_id,
        "requestJson": json.dumps({
            "requestId": request_id,
            "projectId": f"PRJ_E2EIT_{stamp}",
            "dataAccessEmails": [],
        }),
        "status": {"validationStatus": True, "validationReport": "[]"},
        "samples": [{
            "primaryId": primary_id,
            "cmoSampleName": f"E2EIT-{stamp}",
            "cmoPatientId": patient__cmo,
            "sampleName": "E2E integration tumor",
            "sampleType": "Biopsy",
            "species": "Human",
            "tumorOrNormal": "Tumor",
            "preservation": "Frozen",
            "sampleOrigin": None,
            "genePanel": "HC_IMPACT",
            "baitSet": "IMPACT505_BAITS",
            "libraries": [{
                "runs": [{
                    "runMode": "NovaSeq X 10B",
                    "runId": f"SEQRUN_{stamp}",
                    "flowCellId": "FCELL_E2EIT",
                    "runDate": "2026-02-01",
                }]
            }],
        }],
    }

    async def _publish():
        nc = await nats.connect(servers=[NATS_URL])
        try:
            js = nc.jetstream()
            await js.publish(NEW_REQUEST_SUBJECT, json.dumps(payload).encode())
        finally:
            await nc.close()

    asyncio.run(_publish())

    conn = psycopg2.connect(DB_URL, connect_timeout=3)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        row = None
        deadline = time.monotonic() + INGEST_TIMEOUT
        while time.monotonic() < deadline:
            cur.execute(
                """
                SELECT tumor_or_normal, patient__cmo, request_id, file_group,
                       gene_panel, bait_set, run_mode, sequencing_date,
                       additional_metadata::jsonb
                FROM samples
                WHERE primary_id = %s
                """,
                (primary_id,),
            )
            row = cur.fetchone()
            if row is not None:
                break
            time.sleep(1.0)
    finally:
        conn.close()

    assert row is not None, (
        f"sample {primary_id} did not land in the dev DB within "
        f"{INGEST_TIMEOUT:.0f}s — is the smile-listener consuming and the "
        "app ingest endpoint up?"
    )
    tumor, p_id, req_id, file_group, panel, bait, run_mode, _run_date, meta = row
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert tumor == "TUMOR"
    assert p_id == patient__cmo
    assert req_id == request_id
    assert file_group == "SMILE"
    assert panel == "HC_IMPACT"
    assert bait == "IMPACT505_BAITS"
    assert run_mode == "NovaSeq X 10B"
    assert isinstance(meta, dict), "additional_metadata missing"
    request_metadata = meta.get("requestMetadata")
    assert isinstance(request_metadata, dict), (
        "requestMetadata missing from additional_metadata"
    )
    assert request_metadata.get("requestId") == request_id
    assert meta.get("requestStatus") is not None
