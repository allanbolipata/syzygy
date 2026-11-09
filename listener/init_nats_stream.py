"""One-shot dev bootstrap: create the JetStream MDB_STREAM idempotently.

Run by the nats-init compose service before the smile-listener starts.
The cmo subjects and the DMP sample subject live on MDB_STREAM in
production; creating the
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
    "MDB_STREAM.server.dmp-gateway.dmp-sample",
]


async def main() -> None:
    url = os.environ["NATS_URL"]
    nc = await nats.connect(servers=[url])
    js = nc.jetstream()
    try:
        try:
            info = await js.stream_info(STREAM_NAME)
            existing = list(info.config.subjects or [])
            missing = [s for s in SUBJECTS if s not in existing]
            if not missing:
                print(
                    f"Stream {STREAM_NAME} already exists "
                    f"({info.state.msgs} messages); nothing to do"
                )
                return
            # An existing stream predates a subject (e.g. bootstrapped
            # before the DMP subject existed): add the missing subjects
            # rather than leaving the stream incomplete.
            info.config.subjects = existing + missing
            await js.update_stream(info.config)
            print(f"Stream {STREAM_NAME} updated; added subjects:")
            for subject in missing:
                print(f"  {subject}")
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
