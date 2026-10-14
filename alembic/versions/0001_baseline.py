"""baseline schema

Initial schema: pairing_logic, samples, and pairing_table, including the
two unique partial "latest" indexes on pairing_table.

For databases already created by Base.metadata.create_all, stamp this
revision instead of applying it:  alembic stamp 0001_baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pairing_logic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pairing_logic_name", "pairing_logic", ["name"], unique=True)

    op.create_table(
        "samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("primary_id", sa.String(length=255), nullable=False),
        sa.Column("cmo_sample_name", sa.String(length=255), nullable=False),
        sa.Column("patient_id", sa.String(length=255), nullable=False),
        sa.Column("sample_type", sa.String(length=255), nullable=False),
        sa.Column("gene_panel", sa.String(length=255), nullable=False),
        sa.Column("bait_set", sa.String(length=255), nullable=False),
        sa.Column("tumor_or_normal", sa.Enum("TUMOR", "NORMAL", name="tumor_or_normal"), nullable=False),
        sa.Column("additional_metadata", sa.JSON(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("sequencing_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modified_date", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_samples_primary_id", "samples", ["primary_id"], unique=False)
    op.create_index("ix_samples_patient_id", "samples", ["patient_id"], unique=False)

    op.create_table(
        "pairing_table",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pairing_logic_id", sa.Uuid(), nullable=False),
        sa.Column("tumor_id", sa.Uuid(), nullable=False),
        sa.Column("normal_id", sa.Uuid(), nullable=False),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modified_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["pairing_logic_id"], ["pairing_logic.id"]),
        sa.ForeignKeyConstraint(["tumor_id"], ["samples.id"]),
        sa.ForeignKeyConstraint(["normal_id"], ["samples.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pairing_table_pairing_logic_id", "pairing_table", ["pairing_logic_id"], unique=False)
    op.create_index("ix_pairing_table_tumor_id", "pairing_table", ["tumor_id"], unique=False)
    op.create_index("ix_pairing_table_normal_id", "pairing_table", ["normal_id"], unique=False)
    # Unique partial indexes: at most one latest pairing per
    # (tumor, pairing_logic) and per (tumor, normal, pairing_logic).
    op.create_index(
        "uq_pairing_table_tumor_pairing_logic_latest",
        "pairing_table",
        ["tumor_id", "pairing_logic_id"],
        unique=True,
        postgresql_where=sa.text("latest"),
        sqlite_where=sa.text("latest"),
    )
    op.create_index(
        "uq_pairing_table_tumor_normal_pairing_logic_latest",
        "pairing_table",
        ["tumor_id", "normal_id", "pairing_logic_id"],
        unique=True,
        postgresql_where=sa.text("latest"),
        sqlite_where=sa.text("latest"),
    )


def downgrade() -> None:
    op.drop_index("uq_pairing_table_tumor_normal_pairing_logic_latest", table_name="pairing_table")
    op.drop_index("uq_pairing_table_tumor_pairing_logic_latest", table_name="pairing_table")
    op.drop_index("ix_pairing_table_normal_id", table_name="pairing_table")
    op.drop_index("ix_pairing_table_tumor_id", table_name="pairing_table")
    op.drop_index("ix_pairing_table_pairing_logic_id", table_name="pairing_table")
    op.drop_table("pairing_table")
    op.drop_index("ix_samples_patient_id", table_name="samples")
    op.drop_index("ix_samples_primary_id", table_name="samples")
    op.drop_table("samples")
    op.drop_index("ix_pairing_logic_name", table_name="pairing_logic")
    op.drop_table("pairing_logic")
