"""Standalone smile-client listener for the syzygy dev stack.

Run as its own process (not inside the app): smile-client's
start_consuming installs its own SIGINT/SIGTERM handlers and the package
imports django.conf at module load, so a dedicated listener process keeps
uvicorn's shutdown clean and mirrors beagle's separate-consumer-process
pattern. The app itself never connects to NATS; this process POSTs every
consumed message to the app's internal ingest endpoint.
"""
import asyncio
import logging
import os

from smile_client.smile_client import SmileClient


def build_config() -> dict:
    """Build the smile-client connection config from the environment."""
    config = {
        "NATS_URL": os.environ["NATS_URL"],
        "NATS_FILTER_SUBJECT": os.environ.get(
            "NATS_FILTER_SUBJECT", "MDB_STREAM.server.cpt-gateway.>"
        ),
        "NATS_DURABLE": os.environ.get("NATS_DURABLE", "syzygy"),
        "CLIENT_TIMEOUT": float(os.environ.get("CLIENT_TIMEOUT", "3600")),
        "CALLBACK": "smile_bridge.ingest_callback",
    }
    # Optional auth/SSL: omitted from the config when unset (nats-py treats
    # a missing key as "no auth", while an empty string is not).
    for key in ("NATS_USERNAME", "NATS_PASSWORD"):
        value = os.environ.get(key)
        if value:
            config[key] = value
    for key in ("NATS_SSL_CERTFILE", "NATS_SSL_KEYFILE", "NATS_ROOT_CA"):
        value = os.environ.get(key)
        if value:
            config[key] = value
    return config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = build_config()
    # The fail-closed ingest endpoint rejects every message without a
    # matching token, so warn loudly at startup if ours is unset.
    if not os.environ.get("SYZYGY_NATS_TOKEN"):
        logging.warning(
            "SYZYGY_NATS_TOKEN not set: the ingest endpoint (fail-closed) "
            "will reject every message this listener sends"
        )
    # The subscribe subject and the consumer's filter subject are the same
    # wildcard, so one durable consumer receives all three cmo subjects.
    subject = config["NATS_FILTER_SUBJECT"]
    client = SmileClient(config)
    asyncio.run(client.start_consuming(subject, None))


if __name__ == "__main__":
    main()
