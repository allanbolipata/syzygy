"""audit_log: append-only audit table + REVOKE + guard trigger

Revision ID: 0006_audit_log
Revises: 0005_redaction
Create Date: 2026-11-17

AUDIT_LOG_TICKETS.md, ticket AL1.  Every sensitive write point emits
exactly one append-only event (syzygy/audit.py::record).  Enforced at
the database level (DECIDED #2): UPDATE/DELETE are REVOKE'd for
non-owner roles and a BEFORE UPDATE OR DELETE trigger rejects them for
everyone, owner included.

CREATE TABLE IF NOT EXISTS: on databases the app has already
create_all'd, the table (and its indexes) already exist — the guarded
DDL still applies (trigger + REVOKE).
"""

from alembic import op

from syzygy.audit import PG_GUARD_STATEMENTS

# revision identifiers, used by Alembic.
revision = "0006_audit_log"
down_revision = "0005_redaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id UUID PRIMARY KEY,
            occurred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            actor TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            pairing_logic TEXT,
            payload JSONB NOT NULL,
            reverse_of UUID,
            request_id TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_occurred_at "
        "ON audit_log (occurred_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_pairing_logic_occurred_at "
        "ON audit_log (pairing_logic, occurred_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_entity_type_entity_id "
        "ON audit_log (entity_type, entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_actor_occurred_at "
        "ON audit_log (actor, occurred_at)"
    )
    # Append-only enforcement (DECIDED #2): the app role keeps INSERT
    # and SELECT; UPDATE/DELETE are rejected at the database level.
    for statement in PG_GUARD_STATEMENTS:
        op.execute(statement)
    op.execute("ANALYZE audit_log")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only_guard()")
    # PG DROP INDEX takes the index name only — no ON clause (unlike
    # DROP TRIGGER).
    op.execute("DROP INDEX IF EXISTS ix_audit_log_actor_occurred_at")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_entity_type_entity_id")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_pairing_logic_occurred_at")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_occurred_at")
    op.execute("DROP TABLE IF EXISTS audit_log")
