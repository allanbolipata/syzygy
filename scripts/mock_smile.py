#!/usr/bin/env python3
"""Mock SMILE publisher/subscriber for local NATS testing.

Standalone debugging tool (runs with the syzygy venv; nats-py is a
scripts/-only dependency). It publishes the vendored beagle fixtures onto
the same JetStream subjects the real SMILE gateway uses, so the full
listener -> app ingest chain can be exercised locally without SMILE:

  pub:    python scripts/mock_smile.py pub <new-request|update-sample|request-update|cohort-validate|cohort-result> <path/to/json> [--server nats://127.0.0.1:4222]
  sub:    python scripts/mock_smile.py sub <new-request|update-sample|request-update|cohort-validate|cohort-result> [--count N] [--timeout S]
  cohort: python scripts/mock_smile.py cohort-validate [--cohort-id ID] [--primary-id ID] [--cmo-id ID] [--end-user EMAIL] [--pm-user EMAIL] [--server ...]

pub does a JetStream publish (js.publish) of the file contents to the
matching subject. sub does a JetStream pull-subscribe (ephemeral consumer
on stream MDB_STREAM) and prints the received messages. cohort-validate
builds a synthetic cohort request (validated locally against
syzygy/data/cohort-request.schema.json) and publishes it to the cohort
subject — pair it with `sub cohort-result` to watch the listener's
validation report come back out of the dev stack.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import nats

STREAM = "MDB_STREAM"
SUBJECTS = {
    "new-request": "MDB_STREAM.server.cpt-gateway.cmo-new-request",
    "request-update": "MDB_STREAM.server.cpt-gateway.cmo-request-update",
    "update-sample": "MDB_STREAM.server.cpt-gateway.cmo-sample-update",
    "cohort-validate": "MDB_STREAM.syzygy.inbound.tempo-cohort-validate",
    "cohort-result": "MDB_STREAM.server.syzygy.outbound.tempo-cohort-result",
}
_COHORT_REQUEST_SCHEMA = (
    Path(__file__).resolve().parent.parent
    / "syzygy"
    / "data"
    / "cohort-request.schema.json"
)
DEFAULT_SERVER = "nats://127.0.0.1:4222"


def _pub(server: str, subject: str, path: str) -> int:
    return _pub_data(server, subject, Path(path).read_bytes())


def _pub_data(server: str, subject: str, data: bytes) -> int:
    async def run() -> int:
        nc = await nats.connect(servers=[server])
        try:
            js = nc.jetstream()
            ack = await js.publish(subject, data)
            print(
                f"published {len(data)} bytes to {subject} "
                f"(stream={ack.stream}, seq={ack.seq})"
            )
            return 0
        finally:
            await nc.close()

    return asyncio.run(run())


def _sub(server: str, subject: str, count: int, timeout: float) -> int:
    async def run() -> int:
        nc = await nats.connect(servers=[server])
        sub = None
        try:
            js = nc.jetstream()
            # Ephemeral pull consumer on the stream (durable=None).
            sub = await js.pull_subscribe(subject, stream=STREAM)
            received = 0
            while received < count:
                msgs = await sub.fetch(batch=count - received, timeout=timeout)
                if not msgs:
                    print(f"timed out after {received}/{count} messages")
                    return 1
                for msg in msgs:
                    received += 1
                    print(f"[{received}] subject={msg.subject}")
                    print(msg.data.decode())
                    await msg.ack()
            return 0
        finally:
            if sub is not None:
                await sub.unsubscribe()
            await nc.close()

    return asyncio.run(run())


def _syn_cohort(args) -> int:
    """Build a synthetic cohort request and publish it to cohort-validate.

    The payload conforms to the vendored request schema (the same one
    `validate_cohort_request` applies in nats_ingest). It is validated
    locally before publishing so ad-hoc poking never sends a payload the
    handler would only answer with a schema-failure report.
    """
    payload = {
        "cohortId": args.cohort_id,
        "endUsers": [args.end_user],
        "pmUsers": [args.pm_user],
        "projectTitle": "mock_smile synthetic cohort",
        "samples": [{"primaryId": args.primary_id, "cmoId": args.cmo_id}],
    }
    try:
        import jsonschema
    except ImportError:
        print(
            "warning: jsonschema not installed; publishing without local validation",
            file=sys.stderr,
        )
    else:
        schema = json.loads(_COHORT_REQUEST_SCHEMA.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as exc:
            print(
                f"synthetic cohort payload failed schema validation: {exc.message}",
                file=sys.stderr,
            )
            return 2
    return _pub_data(
        args.server,
        SUBJECTS["cohort-validate"],
        json.dumps(payload).encode("utf-8"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mock SMILE publisher/subscriber (JetStream MDB_STREAM)."
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"NATS server URL (default {DEFAULT_SERVER})",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    pub = subparsers.add_parser(
        "pub", help="JetStream-publish a JSON file to a subject"
    )
    pub.add_argument("event", choices=sorted(SUBJECTS))
    pub.add_argument("path", help="path to the JSON payload file")

    sub = subparsers.add_parser(
        "sub", help="pull-subscribe to a subject and print messages"
    )
    sub.add_argument("event", choices=sorted(SUBJECTS))
    sub.add_argument(
        "--count", type=int, default=1, help="how many messages to wait for"
    )
    sub.add_argument(
        "--timeout", type=float, default=10.0, help="fetch timeout in seconds"
    )

    coh = subparsers.add_parser(
        "cohort-validate",
        help=(
            "publish a synthetic (schema-validated) cohort request to "
            "the cohort-validate subject"
        ),
    )
    coh.add_argument("--cohort-id", default="mock-99999")
    coh.add_argument(
        "--primary-id",
        default="99999_A",
        help="tumor primaryId (schema pattern ^\\d{5}...)",
    )
    coh.add_argument("--cmo-id", default="C99999")
    coh.add_argument("--end-user", default="analyst@example.org")
    coh.add_argument("--pm-user", default="pm@example.org")

    args = parser.parse_args()
    if args.action == "cohort-validate":
        sys.exit(_syn_cohort(args))
    subject = SUBJECTS[args.event]
    if args.action == "pub":
        sys.exit(_pub(args.server, subject, args.path))
    sys.exit(_sub(args.server, subject, args.count, args.timeout))


if __name__ == "__main__":
    main()
