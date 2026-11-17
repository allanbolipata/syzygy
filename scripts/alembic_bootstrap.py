"""Bring a syzygy database under Alembic management (ticket A1).

Two entry states:

* **fresh database** (no ``samples`` table) — ``alembic upgrade head``
  runs the ``0001_baseline`` revision, which builds the full schema;
* **existing database** (app tables present, no ``alembic_version``) —
  the schema is spot-checked via ``information_schema`` (every
  ``SCHEMA_REQUIREMENTS`` column present; on Postgres the four DMP key
  columns are ``GENERATED ... STORED`` — a database still in the
  plain-column create_all-only fallback state must run
  ``scripts/migrate_dmp_generated_columns.py`` first), and if it
  matches the baseline, ``0001_baseline`` is **stamped** — never run,
  since it must not re-create what is already there.

A database that already has an ``alembic_version`` table is simply
brought to ``head`` (a no-op while the baseline is the head; later
revisions apply through this branch). The script is idempotent: a
second run prints what it found and changes nothing.

    python scripts/alembic_bootstrap.py
    python scripts/alembic_bootstrap.py <database-url>

Postgres-only (like scripts/migrate_dmp_generated_columns.py): the
SQLite zero-Docker fallback is create_all-only and is never
alembic-managed.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a plain script
# (``python scripts/alembic_bootstrap.py``): sys.path[0] is this file's
# directory, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The revision stamped on existing databases (the chain's base).
BASELINE_REVISION = "0001_baseline"

#: The four DMP key columns the baseline carries as GENERATED ... STORED
#: (must stay in lockstep with alembic/versions/0001_baseline.py and
#: scripts/migrate_dmp_generated_columns.py).
EXPECTED_GENERATED = (
    "dmp_patient_key",
    "dmp_ver_key",
    "dmp_pool_id_key",
    "sample_type_norm",
)


def _alembic_config(database_url: str | None):
    """An alembic Config for the repo's alembic.ini; when given, pin the
    URL so alembic/env.py uses it instead of the app settings."""
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def bootstrap(database_url: str | None = None) -> str:
    """Bring the database at *database_url* (default: the app's
    configured DATABASE_URL) under Alembic management. Returns a
    human-readable summary of what was done (also printed by main())."""
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from syzygy.database import settings
    from syzygy.schema_check import find_missing_columns

    url = database_url or settings.database_url
    engine = create_engine(url)
    try:
        if engine.dialect.name != "postgresql":
            raise SystemExit(
                "alembic_bootstrap is Postgres-only (the SQLite fallback "
                f"is create_all-only and never alembic-managed); "
                f"got dialect {engine.dialect.name!r}"
            )

        tables = set(inspect(engine).get_table_names())
        if "alembic_version" in tables:
            command.upgrade(_alembic_config(url), "head")
            return "already under alembic management: ran alembic upgrade head"

        if "samples" not in tables:
            command.upgrade(_alembic_config(url), "head")
            return "fresh database: ran alembic upgrade head (built the schema)"

        # Existing non-alembic database: spot-check the schema, then stamp.
        missing = find_missing_columns(engine)
        if missing:
            raise SystemExit(
                "schema does not match the baseline — missing "
                + ", ".join(
                    f"{table}.{column} (run scripts/{script})"
                    for table, column, script in missing
                )
            )
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'samples' AND is_generated = 'ALWAYS'"
                )
            ).all()
        plain = [c for c in EXPECTED_GENERATED if c not in {r[0] for r in rows}]
        if plain:
            raise SystemExit(
                "schema does not match the baseline — samples DMP key "
                f"columns are plain, not GENERATED ({', '.join(plain)}): "
                "run scripts/migrate_dmp_generated_columns.py first, then "
                "re-run this bootstrap"
            )
        command.stamp(_alembic_config(url), BASELINE_REVISION)
        return "existing database: schema matches the baseline — stamped " + BASELINE_REVISION
    finally:
        engine.dispose()


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else None
    print(bootstrap(url))


if __name__ == "__main__":
    main()
