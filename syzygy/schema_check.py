"""Startup schema-drift check.

The repo's migrations are standalone idempotent scripts under
``scripts/migrate_*.py``; nothing forces an operator to run them after
pulling a new version, and the failure mode of a missed migration is an
obscure runtime error (e.g. ``column samples.dmp_patient_key does not
exist`` surfacing as an HTTP 500). This module maps every column that an
existing database may be missing to the script that adds it, and the app
logs a loud warning for each gap at startup (see ``main.lifespan``) —
turning "mysterious 500 later" into "run this script now" at boot.

Scope and limits:
  * Postgres only. On SQLite (the documented zero-Docker fallback)
    ``create_all`` builds the full current schema and there is no
    migration history to drift from, so the check returns no findings.
  * Existence only, not shape: a fresh ``create_all`` Postgres database
    has the DMP key columns as plain (non-GENERATED) columns, which is
    the supported legacy-path fallback — not drift. Running the dmp
    migration additionally upgrades them to GENERATED ... STORED and
    adds the partial index (performance, not correctness).
  * Data migrations (e.g. ``migrate_logic_names.py`` renames) are not
    detectable by column presence and are intentionally not listed.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

#: (table, column) -> migration script that adds it. Keep in sync with
#: scripts/migrate_*.py; tests/test_schema_check.py asserts every listed
#: column exists in the current models (typo guard).
SCHEMA_REQUIREMENTS: dict[tuple[str, str], str] = {
    ("samples", "patient__dmp"): "migrate_patient_columns.py",
    ("samples", "anon_id"): "migrate_patient_columns.py",
    ("samples", "consent"): "migrate_consent_column.py",
    ("samples", "dmp_patient_key"): "migrate_dmp_generated_columns.py",
    ("samples", "dmp_ver_key"): "migrate_dmp_generated_columns.py",
    ("samples", "dmp_pool_id_key"): "migrate_dmp_generated_columns.py",
    ("samples", "sample_type_norm"): "migrate_dmp_generated_columns.py",
}


def find_missing_columns(engine: Engine) -> list[tuple[str, str, str]]:
    """Return ``[(table, column, migration_script), ...]`` for required
    columns absent from the database. Empty on non-Postgres dialects and
    when the schema is current."""
    if engine.dialect.name != "postgresql":
        return []
    tables = sorted({table for table, _ in SCHEMA_REQUIREMENTS})
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name = ANY(:tables)"
            ),
            {"tables": tables},
        ).all()
    present = {(r[0], r[1]) for r in rows}
    return [
        (table, column, script)
        for (table, column), script in SCHEMA_REQUIREMENTS.items()
        if (table, column) not in present
    ]
