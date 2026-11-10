"""One-shot migration of the ``samples`` table for the HARMONY DMP consent
column.

Adds the nullable ``consent`` BOOLEAN column and backfills existing DMP rows
to ``consent = false`` (the placeholder default — user decision 2026-11-05,
treat as non-consented until the DMP payload carries the real value).
Non-DMP rows stay NULL.

    python scripts/migrate_consent_column.py [DATABASE_URL]

Without an argument it uses the app's configured DATABASE_URL (the ``.env``
/ ``database.settings`` value). Every step is guarded with
information_schema checks, so the script is idempotent: safe to run twice,
and a no-op on a fresh database where ``Base.metadata.create_all`` already
produced the new shape.

Postgres-only (the local development database). No index is needed on
``consent`` (boolean, low cardinality, not a lookup key yet).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a plain script
# (``python scripts/migrate_consent_column.py``): sys.path[0] is this
# file's directory, not the repo root, so ``from database import ...``
# would fail without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

EXPECTED_COLUMNS = {"consent"}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def migrate(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        cols = _columns(conn, "samples")
        if not cols:
            print("samples table not present — nothing to do (fresh install).")
            engine.dispose()
            return
        if "consent" not in cols:
            print("ALTER TABLE samples ADD COLUMN consent BOOLEAN")
            conn.execute(text("ALTER TABLE samples ADD COLUMN consent BOOLEAN"))
        # Backfill: existing DMP rows become the placeholder False;
        # non-DMP rows stay NULL. Idempotent — on a re-run this matches
        # zero rows (DMP rows already carry consent = false).
        result = conn.execute(
            text(
                "UPDATE samples SET consent = false "
                "WHERE consent IS NULL AND sample_type = 'DMP'"
            )
        )
        print(f"backfilled {result.rowcount} DMP row(s) to consent = false")
        # Verify the end state before declaring success.
        cols = _columns(conn, "samples")
        missing = EXPECTED_COLUMNS - cols
        unbackfilled = conn.execute(
            text(
                "SELECT count(*) FROM samples "
                "WHERE sample_type = 'DMP' AND consent IS NULL"
            )
        ).scalar_one()
        if missing or unbackfilled:
            raise SystemExit(
                "migration incomplete: missing columns="
                f"{sorted(missing)}, DMP rows still with consent NULL={unbackfilled}"
            )
        print("samples table is in the expected state.")
    engine.dispose()


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        from database import settings

        url = settings.database_url
    migrate(url)


if __name__ == "__main__":
    main()
