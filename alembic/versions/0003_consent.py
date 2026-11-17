"""0003_consent: add samples.consent and backfill DMP rows to false

Revision ID: 0003_consent
Revises: 0002_patient_columns
Create Date: 2026-11-17

The Alembic twin of ``scripts/migrate_consent_column.py`` (legacy shim —
the script stays functional for one release cycle). Adds the nullable
``consent`` BOOLEAN column to ``samples`` and backfills existing DMP
rows to ``consent = false`` (the placeholder default — user decision
2026-11-05, treat as non-consented until the DMP payload carries the
real value); non-DMP rows stay NULL. No index is needed on ``consent``
(boolean, low cardinality, not a lookup key yet).

Idempotency contract (mirrors the script's information_schema guards):
the ADD COLUMN is skipped when ``consent`` already exists, and the
backfill is a no-op on re-run (it matches zero rows once every DMP row
carries ``consent = false``) — so stamping ``0001_baseline`` on a fully
migrated database and then ``alembic upgrade head`` is safe.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0003_consent"
down_revision: Union[str, None] = "0002_patient_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'samples'"
        )
    ).all()
    if not rows:
        # samples table not present — nothing to do (defensive: the
        # baseline revision creates it on a fresh database).
        return
    cols = {r[0] for r in rows}
    if "consent" not in cols:
        op.execute("ALTER TABLE samples ADD COLUMN consent BOOLEAN")
    # Backfill: existing DMP rows become the placeholder False; non-DMP
    # rows stay NULL. Idempotent — a re-run matches zero rows.
    op.execute(
        "UPDATE samples SET consent = false "
        "WHERE consent IS NULL AND sample_type = 'DMP'"
    )


def downgrade() -> None:
    # Destructive: the backfilled placeholder values are lost.
    op.execute("ALTER TABLE samples DROP COLUMN IF EXISTS consent")
