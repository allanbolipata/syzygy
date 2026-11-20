"""0008_cmo_sample_name_index: index samples.cmo_sample_name

Revision ID: 0008_cmo_sample_name_index
Revises: 0007_audit_view
Create Date: 2026-11-19

``samples.cmo_sample_name`` is an exact-match filter on ``GET /v1/sample/``
(the listing's ``cmo_sample_name=`` param) and the default resolution key
of the TEMPO pair importer (``scripts/import_tempo_pairs.py``,
``--id-field cmo_sample_name``): every pair row resolves its tumor and its
normal through that filter — two full table scans per row on an unindexed
column, which is what makes a full-file import hour-scale. The UI's name
filter benefits from the same index.

Adds one plain (non-unique) index:

    ix_samples_cmo_sample_name  ON samples (cmo_sample_name)

Plain, not unique: cmo_sample_name is not guaranteed unique across
ingests (duplicates are surfaced as ambiguity by the importer's
resolution rather than rejected), so uniqueness must not be enforced
here.

Idempotency contract (mirrors 0004/0005): a fresh database created via
``create_all`` from the model (which now declares ``index=True``) already
has the index — this revision skips it. Stamping an older database and
running ``upgrade head`` adds what is missing. IF NOT EXISTS guards make
re-application safe.

downgrade drops the index (destructive to query performance only, never
to data).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0008_cmo_sample_name_index"
down_revision: Union[str, None] = "0007_audit_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_samples_cmo_sample_name"


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
    if not _indexes(conn, "samples") and not conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'samples'"
        )
    ).scalar():
        # samples table not present — nothing to do (defensive: the
        # baseline revision creates it on a fresh database).
        return
    if INDEX_NAME not in _indexes(conn, "samples"):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON samples (cmo_sample_name)"
        )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
