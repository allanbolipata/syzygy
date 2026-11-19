"""0005_redaction: sample redaction columns (spec R1)

Revision ID: 0005_redaction
Revises: 0004_dmp_generated_columns
Create Date: 2026-11-17

Spec R1 (Projects/Syzygy/Factory/REDACTION_TICKETS.md): data redaction
of sample-level sensitive fields. A redacted sample is excluded from
every strategy's candidate query and from the bulk pairing sweeps, and
explicit pairing triggers on it return 400; candidate listings still
show it, flagged. Redaction never retires existing pairings (pairing
history is immutable).

Adds four columns to ``samples``:

    redacted          BOOLEAN NOT NULL DEFAULT false — the flag.
    redacted_at       TIMESTAMP WITH TIME ZONE NULL — audit: when.
    redacted_by       TEXT NULL — audit: the acting user's email.
    redaction_reason  TEXT NULL — operator-provided reason.

Together these four columns ARE the redaction audit trail (this branch
predates the audit-log feature; spec R1 DECIDED).

Idempotency contract (mirrors 0001/0004): a column that already exists
is skipped — ``create_all`` on a fresh database already creates it (the
model is the source of truth for fresh-DB state), and a database that
was migrated by an earlier hand-rolled state is only missing what this
revision adds. So stamping ``0001_baseline`` on a fully migrated
database and then ``alembic upgrade head`` is safe, and a fresh
database passes through this revision unchanged.

downgrade is destructive: the columns (and with them the audit trail)
are dropped.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0005_redaction"
down_revision: Union[str, None] = "0004_dmp_generated_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: column name -> DDL type expression (spec R1).
REDACTION_COLUMNS: dict = {
    "redacted": "BOOLEAN NOT NULL DEFAULT FALSE",
    "redacted_at": "TIMESTAMP WITH TIME ZONE",
    "redacted_by": "TEXT",
    "redaction_reason": "TEXT",
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


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn, "samples")
    if not cols:
        # samples table not present — nothing to do (defensive: the
        # baseline revision creates it on a fresh database).
        return
    for name, ddl in REDACTION_COLUMNS.items():
        if name not in cols:
            op.execute(f"ALTER TABLE samples ADD COLUMN {name} {ddl}")
    # Fresh planner statistics for the new columns.
    op.execute("ANALYZE samples")


def downgrade() -> None:
    # Destructive: the redaction columns (and with them the audit
    # trail) are dropped.
    for name in REDACTION_COLUMNS:
        op.execute(f"ALTER TABLE samples DROP COLUMN IF EXISTS {name}")
