"""Append-only audit log writer (AUDIT_LOG_TICKETS.md, ticket AL1).

Every sensitive write point in the app calls :func:`record` inside the
caller's own transaction, so the audit row commits atomically with the
mutation it describes (decision 3).

Design decisions (see AUDIT_LOG_TICKETS.md, "DECIDED"):

* The table is append-only at the DATABASE level: revision
  ``0006_audit_log`` REVOKEs UPDATE/DELETE and installs a guard trigger
  that rejects them for everyone, owner included.  This module never
  issues UPDATE or DELETE.
* Actor is an e-mail address for user actions and ``"system"`` for
  machine-driven paths; ``actor_kind`` is derived from it
  (``user`` / ``system``) — decision 4.
* Payloads carry ids and names ONLY — never patient, clinical or
  variant data (binding sanitization rule, decision 5).
* The writer NEVER raises: an audit failure is logged and the mutation
  proceeds, so a broken audit path can never break pairing/ingest.
* The table is created by Alembic revision ``0006_audit_log`` (and by
  ``create_all`` in dev/test).  If it is missing at runtime, the writer
  skips the event gracefully and says so once per process.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from syzygy.models import AuditLog, utcnow

logger = logging.getLogger(__name__)

#: Actor value for machine-driven paths (ingest, bulk passes, the
#: outbox flusher, startup self-heal) — decision 4.
SYSTEM_ACTOR = "system"

#: Upper bound on per-row ``pairing.created`` detail events recorded by
#: one bulk pass; the pass always writes ONE ``pairing.bulk`` summary
#: event carrying the full counts (decision 4, "bulk pass: one summary
#: event per pass, plus one pairing.created per row created, capped at
#: BULK_AUDIT_DETAIL_CAP").
BULK_AUDIT_DETAIL_CAP = 500

# _bulk_detail_records counts detail events for the CURRENT bulk pass.
_bulk_detail_records = 0

# engine (id) -> whether audit_log exists on that engine.  The table
# never appears/disappears within a process in production; the cache
# keeps record() free of per-event reflection round-trips.  Keyed by
# id(): a recycled id can only return a stale answer for an engine that
# no longer exists — harmless.
_engine_cache: Dict[int, bool] = {}


def reset_table_cache() -> None:
    """Clear the per-engine table-existence cache (test hook)."""
    global _bulk_detail_records
    _engine_cache.clear()
    _bulk_detail_records = 0


def actor_from_user(current_user: Any) -> str:
    """Resolve the audit actor from a ``get_current_user()`` result.

    A user dict -> its e-mail address; a system principal or anything
    without a usable identity (e.g. a direct service call with no
    request context) -> ``"system"``.
    """
    if isinstance(current_user, dict):
        email = current_user.get("email") or current_user.get("sub")
        if isinstance(email, str) and email:
            return email
    return SYSTEM_ACTOR


def _actor_kind(actor: Any) -> str:
    return "user" if isinstance(actor, str) and actor and actor != SYSTEM_ACTOR else "system"


def _safe_payload(payload: Any) -> Dict[str, Any]:
    """Coerce the payload to JSON-safe values (ids/names only by
    contract — this only guards against non-serializable objects)."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    try:
        return json.loads(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return {"unserializable": True}


def _table_present_for(db: Session) -> bool:
    engine = db.get_bind()
    key = id(engine)
    present = _engine_cache.get(key)
    if present is None:
        # Reflect on the SESSION'S OWN connection.  Opening a fresh pool
        # connection here (inspect(engine)) and closing it again would
        # roll back the session's in-flight transaction on a shared
        # single-connection pool (the in-memory test engine) — the
        # sample INSERT flushed moments earlier would vanish.
        present = inspect(db.connection()).has_table("audit_log")
        _engine_cache[key] = present
    return present


def record(
    db: Session,
    actor: Any,
    action: str,
    *,
    entity_type: str,
    entity_id: Any,
    pairing_logic: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    reverse_of: Optional[uuid.UUID] = None,
) -> Optional[AuditLog]:
    """Record one audit event inside the caller's transaction.

    ``actor`` is an e-mail address (user action) or ``"system"``.
    ``entity_type``/``entity_id`` identify the mutated row (ids/names
    only).  ``pairing_logic`` is the visibility-scope key for AL2
    (NULL = admin-only, unless it is the actor's own action).

    Returns the flushed row, or ``None`` when the event was skipped or
    the write failed.  NEVER raises — an audit failure must never break
    the mutation it documents.
    """
    row: Optional[AuditLog] = None
    try:
        if not _table_present_for(db):
            logger.warning(
                "audit: audit_log table missing — skipping event %s "
                "(run Alembic revision 0006_audit_log or create_all)",
                action,
            )
            return None
        row = AuditLog(
            occurred_at=utcnow(),
            actor=actor if isinstance(actor, str) and actor else SYSTEM_ACTOR,
            actor_kind=_actor_kind(actor),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            pairing_logic=pairing_logic,
            payload=_safe_payload(payload),
            request_id=request_id,
            reverse_of=reverse_of,
        )
        db.add(row)
        db.flush()
        return row
    except Exception:
        logger.exception(
            "audit: failed to record %s — continuing without the audit row", action
        )
        if row is not None:
            try:
                db.expunge(row)
            except Exception:
                pass
        return None


def record_pairing_created(
    db: Session,
    actor: Any,
    *,
    pairing,
    logic_name: Optional[str],
    payload: Dict[str, Any],
    in_bulk: bool = False,
) -> Optional[AuditLog]:
    """``pairing.created`` detail event, honoring the bulk detail cap.

    Outside bulk passes every call records; inside a bulk pass only the
    first :data:`BULK_AUDIT_DETAIL_CAP` calls record (the pass's
    summary event carries the full counts).
    """
    global _bulk_detail_records
    if in_bulk and _bulk_detail_records >= BULK_AUDIT_DETAIL_CAP:
        return None
    row = record(
        db,
        actor,
        "pairing.created",
        entity_type="pairing",
        entity_id=pairing.id,
        pairing_logic=logic_name,
        payload=payload,
    )
    if row is not None:
        _bulk_detail_records += 1
    return row


def begin_bulk_pass() -> None:
    """Reset the per-pass detail-event counter (called at pass start)."""
    global _bulk_detail_records
    _bulk_detail_records = 0


def bulk_detail_count() -> int:
    """Detail events recorded in the CURRENT bulk pass (for the pass's
    ``pairing.bulk`` summary payload)."""
    return _bulk_detail_records


# ---------------------------------------------------------------------------
# Append-only guard (decision 2)
#
# The Alembic revision applies these to PostgreSQL; the SQLite variant
# exists so the guard is testable (and enforced) in dev/test engines.
# ---------------------------------------------------------------------------

PG_GUARD_STATEMENTS: tuple = (
    (
        "CREATE OR REPLACE FUNCTION audit_log_append_only_guard() "
        "RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    ),
    "DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log",
    (
        "CREATE TRIGGER audit_log_append_only "
        "BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only_guard()"
    ),
    # Non-owner roles lose UPDATE/DELETE outright; the trigger above
    # stops the owner as well (triggers fire for the table owner).
    "REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC",
)

SQLITE_GUARD_STATEMENTS: tuple = (
    # SQLite triggers take a single event — one trigger per verb.
    "DROP TRIGGER IF EXISTS audit_log_append_only_update",
    (
        "CREATE TRIGGER audit_log_append_only_update "
        "BEFORE UPDATE ON audit_log "
        "FOR EACH ROW "
        "BEGIN "
        "    SELECT RAISE(ABORT, 'audit_log is append-only'); "
        "END"
    ),
    "DROP TRIGGER IF EXISTS audit_log_append_only_delete",
    (
        "CREATE TRIGGER audit_log_append_only_delete "
        "BEFORE DELETE ON audit_log "
        "FOR EACH ROW "
        "BEGIN "
        "    SELECT RAISE(ABORT, 'audit_log is append-only'); "
        "END"
    ),
)


def guard_statements(dialect_name: str) -> List[str]:
    """The guard DDL statements for a dialect (sqlite vs postgres)."""
    if dialect_name == "sqlite":
        return list(SQLITE_GUARD_STATEMENTS)
    return list(PG_GUARD_STATEMENTS)


def install_guard(conn: Connection) -> None:
    """Install the append-only guard trigger on ``conn``'s dialect."""
    for statement in guard_statements(conn.dialect.name):
        conn.exec_driver_sql(statement)


def drop_guard(conn: Connection) -> None:
    """Drop the append-only guard (test teardown)."""
    for name in (
        "audit_log_append_only",  # legacy single-trigger name
        "audit_log_append_only_update",
        "audit_log_append_only_delete",
    ):
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    if conn.dialect.name != "sqlite":
        conn.exec_driver_sql("DROP FUNCTION IF EXISTS audit_log_append_only_guard()")
