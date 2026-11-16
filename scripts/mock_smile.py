#!/usr/bin/env python3
"""Mock SMILE publisher/subscriber for local NATS testing.

Standalone debugging tool (runs with the syzygy venv; nats-py is a
scripts/-only dependency). It publishes the vendored beagle fixtures onto
the same JetStream subjects the real SMILE gateway uses, so the full
listener -> app ingest chain can be exercised locally without SMILE:

  pub:      python scripts/mock_smile.py pub <event> <path/to/json> [--server nats://127.0.0.1:4222]
  sub:      python scripts/mock_smile.py sub <event> [--count N] [--timeout S]
  pub-dir:  python scripts/mock_smile.py pub-dir <path/to/dir> [--delay S] [--dry-run]
  cohort:   python scripts/mock_smile.py cohort-validate [--cohort-id ID] [--primary-id ID] [--cmo-id ID] [--end-user EMAIL] [--pm-user EMAIL] [--server ...]

Events: new-request | request-update | update-sample | dmp-sample |
cohort-validate | cohort-result.

pub does a JetStream publish (js.publish) of the file contents to the
matching subject. sub does a JetStream pull-subscribe (ephemeral consumer
on stream MDB_STREAM) and prints the received messages.

pub-dir publishes every *.json in a directory in a deterministic
ingest-safe order (new-requests -> sample-update -> request-update ->
dmp-sample -> cohort-validate; alphabetical within a stage), choosing the
subject from the filename token (dev_argos/access/tempo _new_request ->
new-request, sample_update -> update-sample, request_update ->
request-update, dmp_sample -> dmp-sample, cohort_validate ->
cohort-validate). This is the one-command way to exercise the whole dev
ingest chain from tests/fixtures/nats/dev/. --dry-run prints the planned
order without connecting (works without nats-py).

cohort-validate builds a synthetic cohort request (validated locally
against syzygy/data/cohort-request.schema.json) and publishes it to the
cohort subject — pair it with `sub cohort-result` to watch the listener's
validation report come back out of the dev stack.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

STREAM = "MDB_STREAM"
SUBJECTS = {
    "new-request": "MDB_STREAM.server.cpt-gateway.cmo-new-request",
    "request-update": "MDB_STREAM.server.cpt-gateway.cmo-request-update",
    "update-sample": "MDB_STREAM.server.cpt-gateway.cmo-sample-update",
    "dmp-sample": "MDB_STREAM.server.dmp-gateway.dmp-sample",
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

# pub-dir: stage order is the ingest-safe dependency order (requests must
# exist before the sample/request updates that reference them; the cohort
# validation must come last, after everything it references is ingested).
_PUB_DIR_STAGES = [
    "new-request",
    "update-sample",
    "request-update",
    "dmp-sample",
    "cohort-validate",
]


def _classify_filename(name: str) -> str | None:
    """pub-dir filename token -> event. None = not a known fixture."""
    if "_new_request" in name:  # dev_argos/access/tempo_new_request.json
        return "new-request"
    if "sample_update" in name:
        return "update-sample"
    if "request_update" in name:
        return "request-update"
    if "dmp_sample" in name:
        return "dmp-sample"
    if "cohort_validate" in name:
        return "cohort-validate"
    return None


def _pub(server: str, subject: str, path: str) -> int:
    return _pub_data(server, subject, Path(path).read_bytes())


def _pub_data(server: str, subject: str, data: bytes) -> int:
    import nats  # local import: --dry-run/--help work without nats-py

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
    import nats  # local import: --dry-run/--help work without nats-py

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


def _pub_dir(server: str, path: str, delay: float, dry_run: bool) -> int:
    """Publish every *.json in a directory in deterministic ingest order."""
    root = Path(path)
    if not root.is_dir():
        print(f"error: {path} is not a directory", file=sys.stderr)
        return 2
    files = sorted(p for p in root.glob("*.json") if p.is_file())
    if not files:
        print(f"error: no *.json files in {path}", file=sys.stderr)
        return 1
    by_stage: dict[str, list[Path]] = {stage: [] for stage in _PUB_DIR_STAGES}
    for p in files:
        event = _classify_filename(p.name)
        if event is None:
            print(
                f"skipping {p.name}: no known filename token "
                "(_new_request, sample_update, request_update, "
                "dmp_sample, cohort_validate)",
                file=sys.stderr,
            )
            continue
        by_stage[event].append(p)
    plan: list[tuple[str, Path]] = []
    for stage in _PUB_DIR_STAGES:
        plan.extend((SUBJECTS[stage], p) for p in by_stage[stage])
    if not plan:
        print("error: no publishable *.json files found", file=sys.stderr)
        return 1
    print(f"publishing {len(plan)} file(s) from {path} to {server}:")
    for i, (subject, p) in enumerate(plan, 1):
        print(f"  [{i}/{len(plan)}] {subject} <- {p.name}")
    if dry_run:
        print("dry run: nothing published")
        return 0
    for i, (subject, p) in enumerate(plan, 1):
        print(f"({i}/{len(plan)}) ", end="", flush=True)
        rc = _pub_data(server, subject, p.read_bytes())
        if rc != 0:
            return rc
        if i < len(plan) and delay > 0:
            time.sleep(delay)
    return 0


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

    pubdir = subparsers.add_parser(
        "pub-dir",
        help=(
            "publish every *.json in a directory in deterministic "
            "ingest order (new-requests -> sample-update -> "
            "request-update -> dmp-sample -> cohort-validate); "
            "subject chosen by filename token"
        ),
    )
    pubdir.add_argument("path", help="directory containing *.json fixtures")
    pubdir.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds between publishes (default 1.0; 0 = back-to-back)",
    )
    pubdir.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned publish order without connecting",
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
    if args.action == "pub-dir":
        sys.exit(_pub_dir(args.server, args.path, args.delay, args.dry_run))
    subject = SUBJECTS[args.event]
    if args.action == "pub":
        sys.exit(_pub(args.server, subject, args.path))
    sys.exit(_sub(args.server, subject, args.count, args.timeout))


if __name__ == "__main__":
    main()
