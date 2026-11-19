"""0002_patient_columns: rename patient_id -> patient__cmo, add DMP patient columns

Revision ID: 0002_patient_columns
Revises: 0001_baseline
Create Date: 2026-11-17

The Alembic twin of ``scripts/migrate_patient_columns.py`` (legacy shim —
the script stays functional for one release cycle). Renames
``samples.patient_id`` to ``samples.patient__cmo`` and adds the two
DMP-only columns (``patient__dmp``, ``anon_id``) with their indexes,
plus the index rename that keeps a migrated database carrying the same
index names a fresh ``create_all`` does.

Idempotency contract (mirrors the script's information_schema guards):
every step is skipped when the change is already present, so running
this revision against a database where the legacy script (or the
baseline on a fresh database) already made the change is a no-op —
stamping ``0001_baseline`` on a fully migrated database and then
``alembic upgrade head`` is safe.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0002_patient_columns"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def _indexes(conn, table: str) -> set:
    rows = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = :t"
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
    if "patient_id" in cols and "patient__cmo" not in cols:
        op.execute(
            "ALTER TABLE samples RENAME COLUMN patient_id TO patient__cmo"
        )
    if "patient__dmp" not in cols:
        op.execute("ALTER TABLE samples ADD COLUMN patient__dmp VARCHAR(255)")
    if "anon_id" not in cols:
        op.execute("ALTER TABLE samples ADD COLUMN anon_id VARCHAR(255)")
    # The old index follows the column rename automatically; rename it
    # explicitly only in the partial-run corner case where the column is
    # already renamed but the index name lags behind.
    indexes = _indexes(conn, "samples")
    if "ix_samples_patient_id" in indexes and "ix_samples_patient__cmo" not in indexes:
        op.execute(
            "ALTER INDEX ix_samples_patient_id RENAME TO ix_samples_patient__cmo"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_samples_patient__dmp "
        "ON samples (patient__dmp)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_samples_anon_id ON samples (anon_id)"
    )


def downgrade() -> None:
    # Best-effort inverse: drop what the upgrade added, rename what it
    # renamed. Data written to patient__dmp/anon_id is lost, as with the
    # legacy script (which has no downgrade at all).
    conn = op.get_bind()
    op.execute("DROP INDEX IF EXISTS ix_samples_anon_id")
    op.execute("DROP INDEX IF EXISTS ix_samples_patient__dmp")
    indexes = _indexes(conn, "samples")
    if "ix_samples_patient__cmo" in indexes and "ix_samples_patient_id" not in indexes:
        op.execute(
            "ALTER INDEX ix_samples_patient__cmo RENAME TO ix_samples_patient_id"
        )
    cols = _columns(conn, "samples")
    if "anon_id" in cols:
        op.execute("ALTER TABLE samples DROP COLUMN anon_id")
    if "patient__dmp" in cols:
        op.execute("ALTER TABLE samples DROP COLUMN patient__dmp")
    if "patient__cmo" in cols and "patient_id" not in cols:
        op.execute(
            "ALTER TABLE samples RENAME COLUMN patient__cmo TO patient_id"
        )
