"""0007_audit_view: allowed_users audit-visibility columns + audit_view

Revision ID: 0007_audit_view
Revises: 0006_audit_log
Create Date: 2026-11-18

AUDIT_LOG_TICKETS.md, ticket AL2. Two changes, both idempotent:

1. ``allowed_users`` gains the audit-visibility columns (decision 5):

    role          VARCHAR(16) NOT NULL DEFAULT 'user'  (user|admin)
    logic_groups  JSONB NOT NULL DEFAULT '[]'          (logic names)

    An admin sees every audit event; a user sees events of the
    pairing logics in their groups plus their own actions; a user
    with no groups sees only their own actions.

2. The read-only ``audit_view`` over ``audit_log`` — the same
   canonical DDL string the models.py ``after_create`` listener uses
   (syzygy.audit.AUDIT_VIEW_DDL) — plus the PG grant.

Idempotency contract (mirrors 0005): an existing column or view is
skipped — ``create_all`` on a fresh database already creates both
(the model columns and the listener-built view), so stamping a prior
revision and running ``upgrade head`` is safe.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from syzygy.audit import AUDIT_VIEW_DDL

# revision identifiers, used by Alembic.
revision: str = "0007_audit_view"
down_revision: Union[str, None] = "0006_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: column name -> DDL type expression (decision 5; matches the model).
ALLOWED_USER_COLUMNS: dict = {
    "role": "VARCHAR(16) NOT NULL DEFAULT 'user'",
    "logic_groups": "JSONB NOT NULL DEFAULT '[]'",
}


def _columns(conn, table: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def _views(conn) -> set:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = current_schema()"
        )
    ).all()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn, "allowed_users")
    if not cols:
        # allowed_users not present — nothing to do (defensive: the
        # baseline revision creates it on a fresh database).
        return
    for name, ddl in ALLOWED_USER_COLUMNS.items():
        if name not in cols:
            op.execute(f"ALTER TABLE allowed_users ADD COLUMN {name} {ddl}")
    # Fresh planner statistics for the new columns.
    op.execute("ANALYZE allowed_users")
    if "audit_view" not in _views(conn):
        op.execute(AUDIT_VIEW_DDL)
        op.execute("GRANT SELECT ON audit_view TO PUBLIC")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON audit_view FROM PUBLIC")
    op.execute("DROP VIEW IF EXISTS audit_view")
    for name in ALLOWED_USER_COLUMNS:
        op.execute(f"ALTER TABLE allowed_users DROP COLUMN IF EXISTS {name}")
