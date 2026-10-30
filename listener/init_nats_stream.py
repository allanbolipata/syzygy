"""One-shot dev bootstrap: create the JetStream MDB_STREAM idempotently.

Run by the nats-init compose service before the smile-listener starts.
The three cmo subjects live on MDB_STREAM in production; creating the
stream first means the listener's durable consumer has a valid stream to
attach to. Already-exists is not an error (safe to re-run).
"""
import asyncio
import os
import sys

import nats
from nats.js.api import RetentionPolicy, StreamConfig

STREAM_NAME = "MDB_STREAM"
SUBJECTS = [
    "MDB_STREAM.server.cpt-gateway.cmo-new-request",
    "MDB_STREAM.server.cpt-gateway.cmo-request-update",
    "MDB_STREAM.server.cpt-gateway.cmo-sample-update",
    "MDB_STREAM.consumers.tempo.new-cohort-submit",
    "MDB_STREAM.consumers.tempo.cohort-update",
    "MDB_STREAM.consumers.tempo.cohort-result",
]


async def main() -> None:
    url = os.environ["NATS_URL"]
    nc = await nats.connect(servers=[url])
    js = nc.jetstream()
    try:
        try:
            info = await js.stream_info(STREAM_NAME)
            print(
                f"Stream {STREAM_NAME} already exists "
                f"({info.state.msgs} messages); nothing to do"
            )
            return
        except Exception:
            pass
        cfg = StreamConfig(
            name=STREAM_NAME,
            subjects=SUBJECTS,
            retention=RetentionPolicy.LIMITS,
            max_bytes=-1,
            max_msgs=-1,
        )
        await js.add_stream(cfg)
        print(f"Created JetStream stream {STREAM_NAME} with subjects:")
        for subject in SUBJECTS:
            print(f"  {subject}")
    finally:
        await nc.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"nats-init failed: {exc}", file=sys.stderr)
        sys.exit(1)
