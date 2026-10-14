"""ensure unique partial indexes on latest pairings

Databases created before the uniqueness fix carry non-unique
uq_*_latest indexes (the postgresql_where clause is silently ignored on
SQLite, which is why uniqueness had to be dropped). This migration
rebuilds both indexes as unique partial indexes on PostgreSQL and
SQLite alike, so the "one latest pairing per (tumor, logic)" guarantee
holds on existing databases.

If the database contains duplicate latest rows, CREATE UNIQUE INDEX
will fail; deduplicate the data first.

Revision ID: 0002_unique_latest_indexes
Revises: 0001_baseline
Create Date: 2026-10-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_unique_latest_indexes"
down_revision: str = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    (
        "uq_pairing_table_tumor_pairing_logic_latest",
        ["tumor_id", "pairing_logic_id"],
    ),
    (
        "uq_pairing_table_tumor_normal_pairing_logic_latest",
        ["tumor_id", "normal_id", "pairing_logic_id"],
    ),
)


def _recreate_unique(name: str, columns: list) -> None:
    op.execute(f"DROP INDEX IF EXISTS {name}")
    op.create_index(
        name,
        "pairing_table",
        columns,
        unique=True,
        postgresql_where=sa.text("latest"),
        sqlite_where=sa.text("latest"),
    )


def _recreate_non_unique(name: str, columns: list) -> None:
    op.execute(f"DROP INDEX IF EXISTS {name}")
    op.create_index(
        name,
        "pairing_table",
        columns,
        unique=False,
        postgresql_where=sa.text("latest"),
        sqlite_where=sa.text("latest"),
    )


def upgrade() -> None:
    for name, columns in _INDEXES:
        _recreate_unique(name, columns)


def downgrade() -> None:
    # Restore the pre-fix (non-unique) state.
    for name, columns in _INDEXES:
        _recreate_non_unique(name, columns)
