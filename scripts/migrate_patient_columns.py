"""One-shot migration of the ``samples`` table for the DMP ticket.

Renames the patient column ``patient_id`` to ``patient__cmo`` and adds the
two DMP-only columns (``patient__dmp``, ``anon_id``) with their indexes.

    python scripts/migrate_patient_columns.py [DATABASE_URL]

Without an argument it uses the app's configured DATABASE_URL (the ``.env``
/ ``database.settings`` value). Every step is guarded with
information_schema / pg_indexes checks, so the script is idempotent: safe to
run twice, and a no-op on a fresh database where
``Base.metadata.create_all`` already produced the new shape.

Postgres-only (the local development database).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a plain script
# (``python scripts/migrate_patient_columns.py``): sys.path[0] is this
# file's directory, not the repo root, so ``from database import ...``
# would fail without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

EXPECTED_COLUMNS = {"patient__cmo", "patient__dmp", "anon_id"}
EXPECTED_INDEXES = {
    "ix_samples_patient__cmo",
    "ix_samples_patient__dmp",
    "ix_samples_anon_id",
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def _indexes(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = :t"
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
        if "patient_id" in cols:
            print("ALTER TABLE samples RENAME COLUMN patient_id TO patient__cmo")
            conn.execute(
                text("ALTER TABLE samples RENAME COLUMN patient_id TO patient__cmo")
            )
        if "patient__dmp" not in cols:
            print("ADD COLUMN patient__dmp VARCHAR(255)")
            conn.execute(text("ALTER TABLE samples ADD COLUMN patient__dmp VARCHAR(255)"))
        if "anon_id" not in cols:
            print("ADD COLUMN anon_id VARCHAR(255)")
            conn.execute(text("ALTER TABLE samples ADD COLUMN anon_id VARCHAR(255)"))
        # The old index follows the column rename automatically; rename it so
        # a migrated DB carries the same index names a fresh create_all does.
        indexes = _indexes(conn, "samples")
        if "ix_samples_patient_id" in indexes and "ix_samples_patient__cmo" not in indexes:
            print("ALTER INDEX ix_samples_patient_id RENAME TO ix_samples_patient__cmo")
            conn.execute(
                text("ALTER INDEX ix_samples_patient_id RENAME TO ix_samples_patient__cmo")
            )
        if "ix_samples_patient__dmp" not in indexes:
            print("CREATE INDEX IF NOT EXISTS ix_samples_patient__dmp ON samples (patient__dmp)")
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_samples_patient__dmp "
                    "ON samples (patient__dmp)"
                )
            )
        if "ix_samples_anon_id" not in indexes:
            print("CREATE INDEX IF NOT EXISTS ix_samples_anon_id ON samples (anon_id)")
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_samples_anon_id ON samples (anon_id)")
            )
        # Verify the end state before declaring success.
        cols = _columns(conn, "samples")
        indexes = _indexes(conn, "samples")
        missing_cols = EXPECTED_COLUMNS - cols
        missing_idx = EXPECTED_INDEXES - indexes
        if "patient_id" in cols or missing_cols or missing_idx:
            raise SystemExit(
                "migration incomplete: patient_id still present="
                f"{'patient_id' in cols}, missing columns={sorted(missing_cols)}, "
                f"missing indexes={sorted(missing_idx)}"
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
