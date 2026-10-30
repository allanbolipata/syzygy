"""Standalone smile-client listener for the syzygy dev stack.

Run as its own process (not inside the app): smile-client's
start_consuming installs its own SIGINT/SIGTERM handlers and the package
imports django.conf at module load, so a dedicated listener process keeps
uvicorn's shutdown clean and mirrors beagle's separate-consumer-process
pattern. The app itself never connects to NATS; this process POSTs every
consumed message to the app's internal ingest endpoint.

Two SmileClients run in one process, one asyncio task each:

* legacy — ``MDB_STREAM.server.cpt-gateway.>`` (durable ``syzygy``)
* tempo  — ``MDB_STREAM.consumers.tempo.>``    (durable ``syzygy-tmp``)

Each client is supervised independently: a dead consumer task is
restarted with backoff, and a failure of one client never takes the
other down. The two clients keep separate durable consumers on the same
JetStream stream, so the server fans the stream out independently —
each durable has its own redelivery position (shadow mode).

This module owns the process's SIGINT/SIGTERM handlers. The vendored
client installs process-wide handlers via ``signal.signal()`` inside
``start_consuming`` (each setting only its own stop event); with two
clients the last one to start would win and the other consumer could
never be told to stop (shutdown hangs). The vendored setup is disabled
and one consolidated handler here stops both clients.
"""
import asyncio
import logging
import os
import signal

from smile_client.smile_client import SmileClient

logger = logging.getLogger("smile_listener")

# Backoff for restarting a dead consumer task (doubles per failure).
RESTART_INITIAL_DELAY_S = 2.0
RESTART_MAX_DELAY_S = 60.0
# How long shutdown waits for the consumer tasks to wind down before
# cancelling whatever is still stuck (e.g. a client mid-connect when the
# stop signal landed).
SHUTDOWN_GRACE_S = 10.0


def _build_config(
    filter_env: str,
    durable_env: str,
    default_filter: str,
    default_durable: str,
) -> dict:
    """Build one smile-client connection config from the environment."""
    config = {
        "NATS_URL": os.environ["NATS_URL"],
        "NATS_FILTER_SUBJECT": os.environ.get(filter_env, default_filter),
        "NATS_DURABLE": os.environ.get(durable_env, default_durable),
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


def build_config() -> dict:
    """Config for the legacy cpt-gateway client
    (NATS_FILTER_SUBJECT / NATS_DURABLE)."""
    return _build_config(
        filter_env="NATS_FILTER_SUBJECT",
        durable_env="NATS_DURABLE",
        default_filter="MDB_STREAM.server.cpt-gateway.>",
        default_durable="syzygy",
    )


def build_tempo_config() -> dict:
    """Config for the tempo (cohort) client
    (NATS_TEMPO_FILTER_SUBJECT / NATS_TEMPO_DURABLE)."""
    return _build_config(
        filter_env="NATS_TEMPO_FILTER_SUBJECT",
        durable_env="NATS_TEMPO_DURABLE",
        default_filter="MDB_STREAM.consumers.tempo.>",
        default_durable="syzygy-tmp",
    )


def _install_signals(
    loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event, clients: tuple
) -> None:
    """Install this process's ONLY SIGINT/SIGTERM handler.

    SmileClient.start_consuming() installs its own process-wide handlers
    via signal.signal(), each setting only that client's stop event;
    with two clients the last one to start would win and the other
    consumer could never stop. Disable the vendored setup (the package
    itself is not modified) and route every client's stop event from one
    handler here.

    The handler is installed with loop.add_signal_handler rather than
    signal.signal: the callback must wake the event loop, and a bare
    signal.signal handler calling Event.set() while the loop is blocked
    in select() would not be processed until that select call times out
    (up to the NATS ping interval), so shutdown would hang.
    """

    def _disabled(self):
        # run.py owns the process signal handlers (see _install_signals).
        return None

    SmileClient._setup_signal_handlers = _disabled  # type: ignore[method-assign]

    def handle(signum):
        logger.info("signal %s: shutting down", signum)
        shutdown.set()
        for client in clients:
            client._stop_event.set()

    loop.add_signal_handler(signal.SIGINT, handle, signal.SIGINT)
    loop.add_signal_handler(signal.SIGTERM, handle, signal.SIGTERM)


async def _supervise(
    label: str, client: SmileClient, subject: str, shutdown: asyncio.Event
) -> None:
    """Run one client's consumer loop, restarting it with backoff on death.

    Only the shared ``shutdown`` event ends this loop. A crash, or a
    NATS error/closed callback that set the client's own stop event,
    restarts just this client — the sibling task is untouched.
    """
    delay = RESTART_INITIAL_DELAY_S
    while not shutdown.is_set():
        try:
            await client.start_consuming(subject, None)
        except Exception:
            logger.exception("%s consumer crashed", label)
        if shutdown.is_set():
            return
        # start_consuming also RETURNS (rather than raising) when the
        # client's own stop event is set by its NATS error/closed
        # callbacks — that is a failure to restart, not a shutdown, so
        # clear the event so the next start_consuming actually runs.
        if client._stop_event.is_set():
            logger.warning("%s consumer self-stopped (NATS error)", label)
            client._stop_event.clear()
        logger.info("restarting %s consumer in %.0fs", label, delay)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, RESTART_MAX_DELAY_S)


async def _run() -> None:
    legacy = SmileClient(build_config())
    tempo = SmileClient(build_tempo_config())
    shutdown = asyncio.Event()
    _install_signals(asyncio.get_running_loop(), shutdown, (legacy, tempo))

    # One task per client in one loop; the supervisors keep the clients
    # independent — a failure of one must not kill the other.
    legacy_task = asyncio.create_task(
        _supervise("legacy", legacy, legacy.filter_subject, shutdown),
        name="smile-listener-legacy",
    )
    tempo_task = asyncio.create_task(
        _supervise("tempo", tempo, tempo.filter_subject, shutdown),
        name="smile-listener-tempo",
    )
    try:
        await shutdown.wait()
    finally:
        logger.info("shutting down: stopping both consumers")
        # Make the consumers wind down promptly: their stop events are set
        # (so start_consuming leaves its loop) and _disconnect() ends their
        # message iterators so they cannot sit blocked in `async for`.
        for client in (legacy, tempo):
            await client._disconnect()
        done, pending = await asyncio.wait(
            [legacy_task, tempo_task], timeout=SHUTDOWN_GRACE_S
        )
        for task in pending:
            logger.warning(
                "consumer task %s still running; cancelling it", task.get_name()
            )
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    logger.info("listener stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # The fail-closed ingest endpoint rejects every message without a
    # matching token, so warn loudly at startup if ours is unset.
    if not os.environ.get("SYZYGY_NATS_TOKEN"):
        logging.warning(
            "SYZYGY_NATS_TOKEN not set: the ingest endpoint (fail-closed) "
            "will reject every message this listener sends"
        )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
