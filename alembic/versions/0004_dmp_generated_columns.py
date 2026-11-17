"""0004_dmp_generated_columns: DMP sargable pairing-key generated columns

Revision ID: 0004_dmp_generated_columns
Revises: 0003_consent
Create Date: 2026-11-17

The Alembic twin of ``scripts/migrate_dmp_generated_columns.py`` (legacy
shim — the script stays functional for one release cycle). Adds the four
DMP sargable pairing-key columns to ``samples`` as
``GENERATED ALWAYS ... STORED`` (DMP bulk-pairing performance, item 1;
docs/dmp_reference_alignment.md section g) plus the partial index:

    dmp_patient_key   COALESCE(patient__dmp, group 1 of the reference
                      name parse) — the stored column wins, the name
                      parse fills the NULLs (spec g caveat 1). Because
                      the column is generated, it recomputes whenever
                      patient__dmp or primary_id is corrected later;
                      there is no backfill, no import hook, and it can
                      never go stale.
    dmp_ver_key       group 3 of the same name parse (the IM_ver)
    dmp_pool_id_key   additional_metadata pool_id (non-empty string)
                      else project_name (non-empty string)
    sample_type_norm  trim(upper(sample_type)) — the normalized DMP
                      group predicate, a plain column the partial index
                      can filter on

    CREATE INDEX ix_samples_dmp_patient_key
        ON samples (dmp_patient_key, tumor_or_normal)
        WHERE sample_type_norm = 'DMP';

Hand-written: Alembic autogenerate does NOT understand generated
columns, so the expressions below are copied verbatim from
``scripts/migrate_dmp_generated_columns.py`` and must stay in lockstep
with it. The name-parse pattern mirrors
``dmp_translator.remap_dmp_record`` exactly (re.search, leftmost,
unanchored — same leftmost-match semantics as ``regexp_match``). The
text arguments carry an explicit ``COLLATE "C"``: in a database whose
collation is not C/POSIX the planner treats upper/trim (and
regexp_match) as collation-dependent STABLE, which GENERATED columns
reject ("function upper(text) is not immutable"); forcing the C
collation makes every variant accepted. For this pattern the matching
is ASCII-literal (P-\\d+, \\w+ dash-delimited runs), so C-collation
results are identical to any other collation.

Idempotency contract (mirrors the script's information_schema guards):
a column already GENERATED is skipped; a plain column with one of these
names (e.g. from ``create_all`` on the create_all-only fallback state)
is dropped and re-added as generated; the index uses IF NOT EXISTS.
So stamping ``0001_baseline`` on a fully migrated database and then
``alembic upgrade head`` is safe, and a fresh database (where the
baseline already created the four columns as GENERATED) passes through
this revision unchanged.

patient__dmp itself is NEVER written by this revision.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0004_dmp_generated_columns"
down_revision: Union[str, None] = "0003_consent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The reference name pattern as a SQL string literal — the same pattern
#: dmp_translator.remap_dmp_record searches with (leftmost, unanchored).
#: Raw string: the value must carry exactly one backslash per escape
#: (verbatim from scripts/migrate_dmp_generated_columns.py).
_DMP_NAME_SQL = r"'(P-\d+)-(\w+)-(\w+)'"

#: column name -> immutable generated expression (spec g).
GENERATED_COLUMNS: dict = {
    "dmp_patient_key": (
        'COALESCE(patient__dmp, (regexp_match(primary_id COLLATE "C", '
        f"{_DMP_NAME_SQL}))[1])"
    ),
    "dmp_ver_key": (
        '((regexp_match(primary_id COLLATE "C", ' f"{_DMP_NAME_SQL}))[3])"
    ),
    "dmp_pool_id_key": (
        "CASE"
        " WHEN jsonb_typeof(additional_metadata::jsonb -> 'pool_id') = 'string'"
        "   AND additional_metadata ->> 'pool_id' <> ''"
        " THEN additional_metadata ->> 'pool_id'"
        " WHEN jsonb_typeof(additional_metadata::jsonb -> 'project_name') = 'string'"
        "   AND additional_metadata ->> 'project_name' <> ''"
        " THEN additional_metadata ->> 'project_name'"
        " END"
    ),
    "sample_type_norm": 'trim(upper(sample_type COLLATE "C"))',
}

EXPECTED_INDEX = "ix_samples_dmp_patient_key"


def _columns(conn, table: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def _generated_columns(conn, table: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "AND is_generated = 'ALWAYS'"
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
    gen = _generated_columns(conn, "samples")
    for name, expr in GENERATED_COLUMNS.items():
        if name in gen:
            continue
        if name in cols:
            # Plain column (create_all-only fallback state) — re-add as
            # generated, exactly as the legacy script does.
            op.execute(f"ALTER TABLE samples DROP COLUMN {name}")
        op.execute(
            f"ALTER TABLE samples ADD COLUMN {name} TEXT "
            f"GENERATED ALWAYS AS ({expr}) STORED"
        )
    if EXPECTED_INDEX not in _indexes(conn, "samples"):
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_samples_dmp_patient_key "
            "ON samples (dmp_patient_key, tumor_or_normal) "
            "WHERE sample_type_norm = 'DMP'"
        )
    # Fresh planner statistics for the new columns/index.
    op.execute("ANALYZE samples")


def downgrade() -> None:
    # Destructive: the generated columns (and the partial index) are
    # dropped; the DMP candidate query falls back to scanning.
    op.execute(f"DROP INDEX IF EXISTS {EXPECTED_INDEX}")
    for name in GENERATED_COLUMNS:
        op.execute(f"ALTER TABLE samples DROP COLUMN IF EXISTS {name}")
