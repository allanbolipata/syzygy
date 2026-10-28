#!/usr/bin/env python3
"""End-to-end automation of the mock SMILE (NATS) ingest flow.

PREREQUISITE: the full dev stack is already up —

    podman compose -f docker-compose.yml -f docker-compose.dev.yml up -d

(app, db, keycloak, keycloak-db, nats, nats-init, smile-listener).
This script assumes the stack is running; it does not bring it up.

What it does (idempotent, re-runnable):

1. JetStream-publishes the three vendored fixtures in order
   (new-request 99908_C, update-sample 99908_C_1, request-update
   99990_D_2); the standalone smile-listener consumes them and POSTs
   each to the app's /v1/ingest/nats endpoint.
2. Polls the dev DB until the ingest effects are observable:
   - the 5 samples of request 99908_C are registered (file_group
     'SMILE'), each carrying additional_metadata["requestMetadata"]
     (parsed from requestJson) and ["requestStatus"];
   - 99908_C_1 is TUMOR with its sampleMetadataList preserved;
   - request 99990_D_2 left the sample set unchanged (an unknown
     request is reported missing — WARN in the app log — and nothing
     is created; pre-existing 99990_D_2 rows are updated in place).
3. Proves the automatic pairing path: publishes one synthetic
   new-request whose single TUMOR mirrors an existing pairable
   NORMAL (patient_id, gene_panel, bait_set, run_mode and
   preservation/sample_origin copied from it) and asserts a
   pairing_table row for that tumor with trigger_type='automatic'.

   The task's alternative — a synthetic NORMAL matching tumor
   99908_C_1 — is impossible: 99908_C_1's preservation/origin
   combination (Frozen + Tissue) is unresolvable and the ARGOS
   preservation gate blocks every pair involving it, so no normal
   can ever auto-pair with it.

Exits 0 when every check passes, 1 otherwise, with a printed summary.

Run with the syzygy venv (nats-py is a scripts/-only dependency):

    python scripts/e2e_nats.py [--nats nats://127.0.0.1:4222]
                               [--db-url postgresql://app:app@127.0.0.1:5435/app]
"""
import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import nats
import psycopg2

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from mock_smile import SUBJECTS  # noqa: E402

DEFAULT_NATS = "nats://127.0.0.1:4222"
DEFAULT_DB = "postgresql://app:app@127.0.0.1:5435/app"
INGEST_TIMEOUT = 60.0
POLL_INTERVAL = 0.5

FIXTURES = [
    ("new-request", "tests/fixtures/nats/99908_C_new_request.json"),
    ("update-sample", "tests/fixtures/nats/99908_C_1_update_sample.json"),
    ("request-update", "tests/fixtures/nats/99990_D_request_update.json"),
]

SYNTHETIC_REQUEST_ID = "99999_E2E"
SYNTHETIC_PRIMARY_PREFIX = "E2E_NAT_"

PROVEN_NORMAL_SQL = """
    SELECT s.primary_id, s.patient_id, s.gene_panel, s.bait_set,
           s.run_mode, s.preservation, s.sample_origin, s.sample_type
    FROM samples s
    JOIN pairing_table p
      ON p.normal_id = s.id AND p.latest AND p.trigger_type = 'automatic'
    WHERE s.tumor_or_normal = 'NORMAL'
      AND s.patient_id IS NOT NULL
      AND s.request_id <> %(syn)s
      AND s.primary_id NOT LIKE %(prefix)s
    ORDER BY s.patient_id, s.primary_id
    LIMIT 1
"""


def tcp_ok(url, timeout=3.0):
    """TCP-connect check against a nats:// or postgresql:// URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (4222 if "nats" in url else 5432)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def publish(server, subject, data):
    """JetStream-publish raw bytes to a subject; returns (stream, seq)."""

    async def run():
        nc = await nats.connect(servers=[server])
        try:
            js = nc.jetstream()
            ack = await js.publish(subject, data)
            return ack.stream, ack.seq
        finally:
            await nc.close()

    return asyncio.run(run())


def poll(description, fn, timeout=INGEST_TIMEOUT):
    """Call fn() until it returns non-None; raise TimeoutError otherwise."""
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result is not None:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for: {description}")
        time.sleep(POLL_INTERVAL)


def check_99908(conn):
    """5 samples of 99908_C, file_group SMILE, request metadata present."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT primary_id, tumor_or_normal, file_group,
               json_typeof((additional_metadata::jsonb->'requestMetadata')::json) = 'object' AS has_rm,
               additional_metadata::jsonb->'requestStatus' IS NOT NULL AS has_rs
        FROM samples
        WHERE request_id = '99908_C'
        """
    )
    rows = cur.fetchall()
    if len(rows) != 5:
        return None
    for row in rows:
        if row[2] != "SMILE" or not row[3] or not row[4]:
            return None
    return rows


def check_99908_c1(conn):
    """99908_C_1 flipped to TUMOR with sampleMetadataList preserved."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT tumor_or_normal,
               json_typeof((additional_metadata::jsonb->'sampleMetadataList')::json)
        FROM samples
        WHERE primary_id = '99908_C_1'
        """
    )
    row = cur.fetchone()
    if row is None or row[0] != "TUMOR" or row[1] != "array":
        return None
    return row


def count_d2(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM samples WHERE request_id = '99990_D_2'")
    return cur.fetchone()[0]


def check_synthetic_pairing(conn, tumor_primary_id):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.primary_id, n.primary_id, p.trigger_type, pl.name
        FROM pairing_table p
        JOIN samples t ON t.id = p.tumor_id
        JOIN samples n ON n.id = p.normal_id
        JOIN pairing_logic pl ON pl.id = p.pairing_logic_id
        WHERE p.latest
          AND p.trigger_type = 'automatic'
          AND t.primary_id = %s
        """,
        (tumor_primary_id,),
    )
    return cur.fetchone()


def _maybe_json(value):
    """psycopg2 returns json columns as strings; re-parse if needed."""
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def build_synthetic(normal):
    """A synthetic new-request whose TUMOR mirrors the given NORMAL."""
    (primary_id, patient_id, gene_panel, bait_set,
     run_mode, preservation, sample_origin, sample_type) = normal
    tumor_primary_id = f"{SYNTHETIC_PRIMARY_PREFIX}{primary_id}"
    payload = {
        "igoRequestId": SYNTHETIC_REQUEST_ID,
        "requestJson": json.dumps({
            "requestId": SYNTHETIC_REQUEST_ID,
            "projectId": "PRJ_SYNTH_E2E",
            "dataAccessEmails": [],
        }),
        "status": {"validationStatus": True, "validationReport": "[]"},
        "samples": [{
            "primaryId": tumor_primary_id,
            "cmoSampleName": f"E2E-NAT-{primary_id}",
            "cmoPatientId": patient_id,
            "sampleName": "E2E synthetic tumor",
            "sampleType": (
                sample_type
                if sample_type and str(sample_type).strip().upper() != "DMP"
                else "Biopsy"
            ),
            "species": "Human",
            "tumorOrNormal": "Tumor",
            "preservation": _maybe_json(preservation),
            "sampleOrigin": _maybe_json(sample_origin),
            "genePanel": gene_panel,
            "baitSet": bait_set,
            "importDate": "2026-01-01",
            "libraries": [{
                "runs": [{
                    "runMode": run_mode,
                    "runId": "SEQRUN_E2E",
                    "flowCellId": "FCELL_E2E",
                    "runDate": "2026-01-10",
                }]
            }],
        }],
    }
    return tumor_primary_id, payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-runnable e2e automation of the mock SMILE (NATS) flow."
    )
    parser.add_argument("--nats", default=os.environ.get("NATS_URL", DEFAULT_NATS))
    parser.add_argument("--db-url", default=os.environ.get("E2E_DB_URL", DEFAULT_DB))
    args = parser.parse_args()

    # Prerequisites: NATS and the dev DB must be up (documented above).
    if not tcp_ok(args.nats):
        print(f"FAIL: NATS not reachable at {args.nats} — bring up the dev stack first")
        return 1
    if not tcp_ok(args.db_url):
        print(f"FAIL: dev DB not reachable at {args.db_url} — bring up the dev stack first")
        return 1
    try:
        conn = psycopg2.connect(args.db_url)
    except psycopg2.OperationalError as exc:
        print(f"FAIL: dev DB connection failed: {exc}")
        return 1
    conn.autocommit = True  # each poll must see fresh data, not one snapshot

    failures = []
    d2_outcome = ""
    pairing_info = None

    pre_d2 = count_d2(conn)
    print(f"[1] 99990_D_2 samples before fixtures: {pre_d2}")

    for event, rel_path in FIXTURES:
        stream, seq = publish(
            args.nats, SUBJECTS[event], (REPO_ROOT / rel_path).read_bytes()
        )
        print(f"[2] published {event} ({rel_path}) -> stream={stream} seq={seq}")

    try:
        rows = poll("99908_C registration (5 samples with request metadata)",
                    lambda: check_99908(conn))
        print(f"[3] 99908_C: {len(rows)} samples registered (file_group=SMILE, "
              "requestMetadata + requestStatus on each)")
    except TimeoutError as exc:
        print(f"[3] FAIL: {exc}")
        failures.append(str(exc))

    try:
        poll("99908_C_1 flip to TUMOR with sampleMetadataList",
             lambda: check_99908_c1(conn))
        print("[4] 99908_C_1: TUMOR after update-sample, sampleMetadataList preserved")
    except TimeoutError as exc:
        print(f"[4] FAIL: {exc}")
        failures.append(str(exc))

    post_d2 = count_d2(conn)
    if post_d2 == pre_d2:
        if pre_d2 == 0:
            d2_outcome = ("no registered samples — request-update reported the "
                          "request missing (WARN in app log), nothing created")
        else:
            d2_outcome = (f"{pre_d2} pre-existing sample(s) from an earlier "
                          "populate were updated in place (count unchanged)")
    else:
        d2_outcome = f"count changed {pre_d2} -> {post_d2} (unexpected)"
        failures.append("99990_D_2 sample count changed")
    print(f"[5] 99990_D_2: {d2_outcome}")

    cur = conn.cursor()
    cur.execute(
        PROVEN_NORMAL_SQL,
        {"syn": SYNTHETIC_REQUEST_ID,
         "prefix": f"{SYNTHETIC_PRIMARY_PREFIX}%"},
    )
    normal = cur.fetchone()
    if normal is None:
        print("[6] FAIL: no existing automatic-paired NORMAL in the dev DB to "
              "mirror for the auto-pairing proof")
        failures.append("no proven-pairable normal found")
    else:
        tumor_primary_id, payload = build_synthetic(normal)
        stream, seq = publish(
            args.nats, SUBJECTS["new-request"], json.dumps(payload).encode()
        )
        print(f"[6] published synthetic new-request {SYNTHETIC_REQUEST_ID} "
              f"(tumor {tumor_primary_id} mirroring normal {normal[0]}) "
              f"-> stream={stream} seq={seq}")
        try:
            pairing = poll(
                f"automatic pairing for {tumor_primary_id}",
                lambda: check_synthetic_pairing(conn, tumor_primary_id),
            )
            pairing_info = pairing
            print(f"[7] pairing: {pairing[0]} (TUMOR) <-> {pairing[1]} (NORMAL), "
                  f"trigger_type={pairing[2]}, logic={pairing[3]}")
        except TimeoutError as exc:
            print(f"[7] FAIL: {exc}")
            failures.append(str(exc))

    conn.close()

    print()
    print("=" * 64)
    if failures:
        print("E2E RESULT: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("E2E RESULT: PASS")
    print("  - 99908_C: 5 samples registered (file_group SMILE) with "
          "requestMetadata/requestStatus")
    print("  - 99908_C_1: TUMOR after update-sample, sampleMetadataList preserved")
    print(f"  - 99990_D_2: {d2_outcome}")
    if pairing_info is not None:
        print(f"  - automatic pairing: {pairing_info[0]} (TUMOR) <-> "
              f"{pairing_info[1]} (NORMAL) under {pairing_info[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
