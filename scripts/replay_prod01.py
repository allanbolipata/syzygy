#!/usr/bin/env python3
"""Replay real SMILE message bodies from the prod01 ETL archive into a NATS server.

This is the prod01-backed sibling of scripts/mock_smile.py: instead of
hand-written fixtures it pulls the `message` bodies stored by the Voyager
ETL importer (GET http://isvvoyagerprod01:5007/v0/etl/import/messages/)
and JetStream-publishes each one, byte-for-byte, onto the SAME subjects the
real SMILE gateway uses. The listener -> app ingest chain cannot tell the
difference — when the real feed goes live you stop this script and change
nothing else (that is the whole switch-over story).

  replay: python scripts/replay_prod01.py [--server nats://127.0.0.1:4222]
          [--status COMPLETED] [--topic <prod01 topic>] [--limit N]
          [--delay 0.2] [--dry-run]

Credentials (env only, never on the command line or in a file):
  PROD01_USER / PROD01_PASS   basic-auth for the prod01 ETL API

Safety rules baked in:
  * prod01 is PRODUCTION: this script only ever issues GET requests.
  * Message bodies are streamed API -> NATS in memory; nothing raw is
    written to disk. Do NOT add a --save flag; the repo is public.
  * Default --status COMPLETED replays only messages the prod consumer
    actually validated (richest, known-good bodies).

Topic mapping (prod01 stores the ETL consumer's own subjects; syzygy
listens on the gateway subjects):
  MDB_STREAM.consumers.cmo-new-request     -> MDB_STREAM.server.cpt-gateway.cmo-new-request
  MDB_STREAM.consumers.cmo-request-update  -> MDB_STREAM.server.cpt-gateway.cmo-request-update
  MDB_STREAM.consumers.cmo-sample-update   -> MDB_STREAM.server.cpt-gateway.cmo-sample-update
The dmp-* prod01 topics carry SMILE-dialect sample messages that the ETL
consumer itself rejects (NOT_SUPPORTED); they have no syzygy subject by
default. Map one explicitly with --map <prod01-topic>=<local-subject> if
you want them (e.g. onto the sample-update subject for a dialect probe).
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.request

STREAM = "MDB_STREAM"
API_BASE = os.environ.get("PROD01_API", "http://isvvoyagerprod01:5007/v0/etl/import/messages")
DEFAULT_SERVER = "nats://127.0.0.1:4222"

TOPIC_MAP = {
    "MDB_STREAM.consumers.cmo-new-request": "MDB_STREAM.server.cpt-gateway.cmo-new-request",
    "MDB_STREAM.consumers.cmo-request-update": "MDB_STREAM.server.cpt-gateway.cmo-request-update",
    "MDB_STREAM.consumers.cmo-sample-update": "MDB_STREAM.server.cpt-gateway.cmo-sample-update",
}


def _fetch_page(params: dict) -> dict:
    """GET one page of the prod01 messages API (GET ONLY — production)."""
    user = os.environ.get("PROD01_USER")
    pw = os.environ.get("PROD01_PASS")
    if not user or not pw:
        sys.exit("set PROD01_USER / PROD01_PASS in the environment first")
    import base64
    import urllib.parse
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API_BASE}/?{qs}")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _iter_messages(status: str | None, topic: str | None, limit: int):
    page, yielded = 1, 0
    while yielded < limit:
        params: dict[str, str] = {"page": str(page), "page_size": "100"}
        if status:
            params["status"] = status
        if topic:
            params["topic"] = topic
        data = _fetch_page(params)
        results = data.get("results") or []
        if not results:
            return
        for row in results:
            if yielded >= limit:
                return
            yield row
            yielded += 1
        if not data.get("next"):
            return
        page += 1


async def _publish(server: str, plan: list[tuple[str, bytes]], delay: float) -> None:
    import nats
    nc = await nats.connect(servers=[server])
    js = nc.jetstream()
    try:
        await js.find_stream_name_by_subject(plan[0][0])
    except Exception:
        print(f"warning: no stream claims {plan[0][0]} — run listener/init_nats_stream.py first")
    for i, (subject, body) in enumerate(plan, 1):
        ack = await js.publish(subject, body)
        print(f"[{i}/{len(plan)}] {subject} -> stream={ack.stream} seq={ack.seq} ({len(body)}B)")
        if delay:
            await asyncio.sleep(delay)
    await nc.drain()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--status", default="COMPLETED",
                    help="prod01 status filter (COMPLETED|FAILED|IN_PROGRESS|NOT_SUPPORTED|none)")
    ap.add_argument("--topic", default=None, help="restrict to one prod01 topic")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between publishes")
    ap.add_argument("--map", action="append", default=[], metavar="TOPIC=SUBJECT",
                    help="extra/override topic->subject mapping")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, do not connect")
    args = ap.parse_args()

    topic_map = dict(TOPIC_MAP)
    for pair in args.map:
        t, _, s = pair.partition("=")
        if not s:
            sys.exit(f"--map expects TOPIC=SUBJECT, got {pair!r}")
        topic_map[t] = s

    plan: list[tuple[str, bytes]] = []
    skipped = 0
    for row in _iter_messages(args.status, args.topic, args.limit * 4):
        subject = topic_map.get(row["topic"])
        if subject is None:
            skipped += 1
            continue
        body = row["message"].encode()
        json.loads(body)  # sanity: body must be valid JSON before publishing
        plan.append((subject, body))
        if len(plan) >= args.limit:
            break

    if skipped:
        print(f"skipped {skipped} message(s) with unmapped topics (see --map)")
    if not plan:
        print("nothing to publish (check --status/--topic filters)")
        return 1
    if args.dry_run:
        for subject, body in plan:
            print(f"would publish {len(body)}B to {subject}")
        return 0
    asyncio.run(_publish(args.server, plan, args.delay))
    print(f"replayed {len(plan)} message(s) from prod01 archive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
