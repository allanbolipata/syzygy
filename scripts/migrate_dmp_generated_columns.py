r"""Idempotently add the DMP sargable pairing-key columns to ``samples``
(DMP bulk-pairing performance, item 1).

Postgres GENERATED ... STORED columns that materialize the exact key
expressions ``remap_dmp_record`` computes (docs/dmp_reference_alignment.md
section g), so the DMP candidate query can filter entirely in SQL on a
partial index instead of scanning the table and re-filtering in Python:

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

plus the partial index:

    CREATE INDEX ix_samples_dmp_patient_key
        ON samples (dmp_patient_key, tumor_or_normal)
        WHERE sample_type_norm = 'DMP';

The expressions are immutable over the stored column types (COALESCE,
regexp_match, trim/upper, jsonb_typeof, ->, ->>). The text arguments carry an
explicit COLLATE "C": in a database whose collation is not C/POSIX the
planner treats upper/trim (and regexp_match) as collation-dependent
STABLE, which GENERATED columns reject ("function upper(text) is not
immutable"); forcing the C collation makes every variant accepted. For
this pattern the matching is ASCII-literal (P-\d+, \w+ dash-delimited
runs), so C-collation results are identical to any other collation.

The name-parse pattern mirrors dmp_translator.remap_dmp_record exactly
(re.search, leftmost, unanchored — same leftmost-match semantics as
regexp_match). Note: Postgres regexp_match has identical group results
to Python re.search for this pattern on ASCII ids (the greedy runs are
dash-delimited and therefore forced). One documented theoretical
divergence: remap_dmp_record keeps a truthy non-string pool_id (JSON
number/bool/dict) verbatim, while the generated column falls through to
project_name for any value that is not a non-empty string. For that
shape (a numeric pool_id whose text form collides with the stored
project_name) the legacy Python remap path remains authoritative — the
strategy's isinstance guard already routes non-string tumor pool keys
to it; a non-string pool on the normal side would require the
theoretical collision above to change a result. Real DMP
pool_id / project_name values are strings, so the row sets are
identical on real data and on the fixture corpus.

patient__dmp itself is NEVER written by this migration or the pairing
code.

    python scripts/migrate_dmp_generated_columns.py
    python scripts/migrate_dmp_generated_columns.py <database-url>

Without an argument it uses the app's configured DATABASE_URL (the
``.env`` / ``database.settings`` value). Every step is guarded with
information_schema / pg_indexes checks, so the script is idempotent:
safe to run twice. A plain column with one of these names (e.g. created
by ``Base.metadata.create_all`` on a fresh Postgres database) is
dropped and re-added as generated. Postgres-only (the local development
database; the SQLite test fallback has no generated columns and uses
the legacy path).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a plain script
# (``python scripts/migrate_dmp_generated_columns.py``): sys.path[0] is
# this file's directory, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

#: The reference name pattern as a SQL string literal — the same pattern
#: dmp_translator.remap_dmp_record searches with (leftmost, unanchored).
_DMP_NAME_SQL = "'(P-\\d+)-(\\w+)-(\\w+)'"

#: column name -> immutable generated expression (spec g).
GENERATED_COLUMNS: dict[str, str] = {
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

EXPECTED_COLUMNS = frozenset(GENERATED_COLUMNS)
EXPECTED_INDEX = "ix_samples_dmp_patient_key"
INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_samples_dmp_patient_key "
    "ON samples (dmp_patient_key, tumor_or_normal) "
    "WHERE sample_type_norm = 'DMP'"
)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0] for r in rows}


def _generated_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "AND is_generated = 'ALWAYS'"
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
        gen = _generated_columns(conn, "samples")
        for name, expr in GENERATED_COLUMNS.items():
            if name in gen:
                print(f"{name}: already GENERATED — skipping")
                continue
            if name in cols:
                print(f"DROP COLUMN {name} (plain column — re-adding as GENERATED)")
                conn.execute(text(f"ALTER TABLE samples DROP COLUMN {name}"))
            print(f"ADD COLUMN {name} TEXT GENERATED ALWAYS AS ({expr}) STORED")
            conn.execute(
                text(
                    f"ALTER TABLE samples ADD COLUMN {name} TEXT "
                    f"GENERATED ALWAYS AS ({expr}) STORED"
                )
            )
        indexes = _indexes(conn, "samples")
        if EXPECTED_INDEX in indexes:
            print(f"{EXPECTED_INDEX}: already present — skipping")
        else:
            print(INDEX_DDL)
            conn.execute(text(INDEX_DDL))
        # Fresh planner statistics for the new columns/index.
        conn.execute(text("ANALYZE samples"))
        # Verify the end state before declaring success.
        cols = _columns(conn, "samples")
        gen = _generated_columns(conn, "samples")
        indexes = _indexes(conn, "samples")
        missing = EXPECTED_COLUMNS - gen
        if missing or EXPECTED_INDEX not in indexes:
            raise SystemExit(
                "migration incomplete: missing generated columns="
                f"{sorted(missing)}, "
                f"index present={EXPECTED_INDEX in indexes}"
            )
        print("samples table is in the expected state.")
    engine.dispose()


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        from syzygy.database import settings

        url = settings.database_url
    migrate(url)


if __name__ == "__main__":
    main()
