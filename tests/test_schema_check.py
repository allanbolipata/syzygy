"""Tests for syzygy/schema_check.py (startup schema-drift check).

Unit tests (any dialect): the requirements map must name real model
columns (typo guard) and the check must be silent on SQLite. Postgres
tests (skipped unless DATABASE_URL is Postgres — the test-postgres CI
job enforces the skip guard): a scratch database with the full
create_all schema reports no drift; dropping required columns is
detected with the right migration script named, and re-adding them
clears the finding.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from syzygy.database import Base, settings
import syzygy.models  # noqa: F401  (register all tables on Base.metadata)
from syzygy.schema_check import SCHEMA_REQUIREMENTS, find_missing_columns


# ---------------------------------------------------------------------------
# dialect-agnostic unit tests
# ---------------------------------------------------------------------------

def test_requirements_name_real_model_columns():
    """Every (table, column) in the map exists in the current models —
    a typo in SCHEMA_REQUIREMENTS would make the check blind."""
    tables = Base.metadata.tables
    for table, column in SCHEMA_REQUIREMENTS:
        assert table in tables, f"unknown table {table!r}"
        assert column in tables[table].columns, f"unknown column {table}.{column}"


def test_requirements_scripts_exist():
    import os
    from pathlib import Path
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    for script in set(SCHEMA_REQUIREMENTS.values()):
        assert os.path.isfile(scripts_dir / script), f"missing {script}"


@pytest.fixture()
def sqlite_engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_sqlite_never_reports_drift(sqlite_engine):
    """SQLite is the documented zero-Docker fallback: create_all builds
    the full schema and there is no migration history to drift from."""
    assert find_missing_columns(sqlite_engine) == []


# ---------------------------------------------------------------------------
# Postgres-backed tests (same bootstrap convention as
# test_dmp_generated_columns.py: skip the module unless DATABASE_URL is
# Postgres; the test-postgres CI job fails on any skip)
# ---------------------------------------------------------------------------

TEST_DB_NAME = "syzygy_schema_check_test"


def _psycopg2(url: str, dbname: str):
    import psycopg2
    from sqlalchemy.engine import make_url

    u = make_url(url)
    conn = psycopg2.connect(
        host=u.host or "localhost", port=u.port or 5432,
        user=u.username, password=u.password, dbname=dbname,
        connect_timeout=5,
    )
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def pg_url():
    url = settings.database_url or ""
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not a postgresql URL (SQLite fallback)")
    return url


@pytest.fixture()
def pg_scratch_engine(pg_url):
    """A throwaway database with the full current schema.

    NB: psycopg2's ``with conn:`` context manager resets autocommit on
    enter, which breaks DROP/CREATE DATABASE — manage these connections
    explicitly (autocommit stays set) and close them by hand.
    """
    from sqlalchemy.engine import make_url
    u = make_url(pg_url)
    admin_db = u.database or "postgres"
    admin = _psycopg2(pg_url, admin_db)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
            cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        admin.close()
    # rebuild via make_url — a naive str.replace on "/{database}" also
    # matches the user/database pair when they share a name (app/app).
    scratch_url = u.set(database=TEST_DB_NAME).render_as_string(hide_password=False)
    eng = create_engine(scratch_url)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    admin = _psycopg2(pg_url, admin_db)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    finally:
        admin.close()


def test_pg_current_schema_no_drift(pg_scratch_engine):
    assert find_missing_columns(pg_scratch_engine) == []


def test_pg_detects_dropped_columns_and_names_scripts(pg_scratch_engine):
    dropped = {
        "consent": "migrate_consent_column.py",
        "dmp_patient_key": "migrate_dmp_generated_columns.py",
        "patient__dmp": "migrate_patient_columns.py",
    }
    with pg_scratch_engine.begin() as conn:
        for column in dropped:
            conn.execute(text(f"ALTER TABLE samples DROP COLUMN {column}"))

    missing = find_missing_columns(pg_scratch_engine)
    found = {(table, column): script for table, column, script in missing}
    for column, script in dropped.items():
        assert ("samples", column) in found, f"{column} not detected"
        assert found[("samples", column)] == script
    # nothing else was flagged
    assert len(missing) == len(dropped)

    # re-adding clears the finding (mirrors what the migration scripts do)
    with pg_scratch_engine.begin() as conn:
        conn.execute(text("ALTER TABLE samples ADD COLUMN consent BOOLEAN"))
        conn.execute(text("ALTER TABLE samples ADD COLUMN patient__dmp VARCHAR(255)"))
        conn.execute(text("ALTER TABLE samples ADD COLUMN dmp_patient_key TEXT"))
    remaining = {(t, c) for t, c, _ in find_missing_columns(pg_scratch_engine)}
    assert remaining == set()
