"""Tests for the Alembic scaffold (ticket A1), the A2 legacy-script
revisions, and scripts/alembic_bootstrap.py.

Postgres-backed: a fresh scratch database upgraded to ``head`` must
produce the same column schema (the ``information_schema.columns``
snapshot) as ``Base.metadata.create_all`` — the baseline additionally
carries the four DMP key columns as ``GENERATED ... STORED``, which
create_all cannot express and is asserted separately. A database with
the create_all schema must accept a stamp of ``0001_baseline`` with a
no-op ``upgrade head`` afterwards (head is ``0004_dmp_generated_columns``
since ticket A2 — the later revisions are all guarded no-ops on an
already-migrated schema). A legacy database (create_all + all the
migrate scripts) must land at head after the bootstrap, with a no-op
``upgrade head``. Skipped when DATABASE_URL is not a reachable
Postgres (the test-postgres CI job fails on any skip — same convention
as tests/test_schema_check.py).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from syzygy.database import Base, settings
import syzygy.models  # noqa: F401  (register all tables on Base.metadata)
from scripts.alembic_bootstrap import BASELINE_REVISION, HEAD_REVISION, bootstrap

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_PREFIX = "syzygy_alembic"

#: The four DMP key columns the baseline carries as GENERATED ... STORED.
EXPECTED_GENERATED = {
    "dmp_patient_key",
    "dmp_ver_key",
    "dmp_pool_id_key",
    "sample_type_norm",
}


# ---------------------------------------------------------------------------
# scratch-database plumbing (same convention as tests/test_schema_check.py)
# ---------------------------------------------------------------------------

def _psycopg2(url: str, dbname: str):
    import psycopg2

    u = make_url(url)
    conn = psycopg2.connect(
        host=u.host or "localhost", port=u.port or 5432,
        user=u.username, password=u.password, dbname=dbname,
        connect_timeout=5,
    )
    conn.autocommit = True
    return conn


def _make_scratch_db(pg_url: str, name: str) -> str:
    """CREATE a fresh empty database; returns its SQLAlchemy URL.

    NB: psycopg2's ``with conn:`` context manager resets autocommit on
    enter, which breaks DROP/CREATE DATABASE — manage these connections
    explicitly and close them by hand.
    """
    u = make_url(pg_url)
    admin = _psycopg2(pg_url, u.database or "postgres")
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    # rebuild via make_url — a naive str.replace on "/{database}" also
    # matches the user/database pair when they share a name (app/app).
    return u.set(database=name).render_as_string(hide_password=False)


def _drop_scratch_db(pg_url: str, name: str) -> None:
    admin = _psycopg2(pg_url, make_url(pg_url).database or "postgres")
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.close()


def _alembic(url: str, *args) -> None:
    """Run an alembic command against *url*, e.g. ``("upgrade", "head")``
    or ``("stamp", "0001_baseline")``, using the repo's alembic.ini."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    getattr(command, args[0])(cfg, *args[1:])


def _column_snapshot(url: str) -> set:
    """The information_schema.columns shape snapshot: (table, column,
    data_type, max_length, nullable) for every column in current_schema.
    ``column_default`` and ``is_generated`` are deliberately excluded —
    the four DMP key columns are generated (with an expression default)
    on the alembic side and plain on the create_all side, and that
    divergence is asserted separately; ``alembic_version`` is alembic
    bookkeeping, not schema. Engines are disposed explicitly: a pooled
    connection left idle would block the fixture's DROP DATABASE
    (ObjectInUse)."""
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, "
                    "COALESCE(character_maximum_length::text, ''), is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name <> 'alembic_version'"
                )
            ).all()
    finally:
        eng.dispose()
    return {(r[0], r[1], r[2], r[3], r[4]) for r in rows}


def _generated_columns(url: str) -> set:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'samples' AND is_generated = 'ALWAYS'"
                )
            ).all()
    finally:
        eng.dispose()
    return {r[0] for r in rows}


def _version_rows(url: str) -> list:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).all()
    finally:
        eng.dispose()
    return [r[0] for r in rows]


def _has_alembic_version(url: str) -> bool:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            return conn.execute(
                text("SELECT to_regclass('alembic_version') IS NOT NULL")
            ).scalar()
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def pg_url():
    url = settings.database_url or ""
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not a postgresql URL (SQLite fallback)")
    # The app's *default* DATABASE_URL is a Postgres URL even when no
    # server exists (plain CI jobs) — probe like test_schema_check does,
    # and skip rather than error when unreachable.
    try:
        conn = _psycopg2(url, make_url(url).database or "postgres")
        conn.close()
    except Exception as exc:  # psycopg2.OperationalError and friends
        pytest.skip(f"Postgres unreachable: {exc}")
    return url


@pytest.fixture()
def scratch_db(pg_url):
    """A fresh, empty scratch database; dropped after each test."""
    name = f"{TEST_DB_PREFIX}_{uuid.uuid4().hex[:12]}"
    url = _make_scratch_db(pg_url, name)
    yield url
    _drop_scratch_db(pg_url, name)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_pg_upgrade_head_matches_create_all(pg_url, scratch_db):
    """Accept: ``alembic upgrade head`` on a fresh PG db produces a schema
    whose information_schema.columns snapshot equals create_all's."""
    _alembic(scratch_db, "upgrade", "head")

    ca_name = f"{TEST_DB_PREFIX}_ca_{uuid.uuid4().hex[:12]}"
    ca_url = _make_scratch_db(pg_url, ca_name)
    try:
        eng = create_engine(ca_url)
        Base.metadata.create_all(eng)
        eng.dispose()
        assert _column_snapshot(scratch_db) == _column_snapshot(ca_url)
        # The baseline carries the four DMP key columns as GENERATED
        # (create_all cannot express them — the documented divergence).
        assert _generated_columns(scratch_db) == EXPECTED_GENERATED
        assert _generated_columns(ca_url) == set()
        # head is 0004_dmp_generated_columns (ticket A2); 0002-0004 are
        # guarded no-ops on the schema the baseline just built.
        assert _version_rows(scratch_db) == [HEAD_REVISION]
    finally:
        _drop_scratch_db(pg_url, ca_name)


def test_pg_stamp_then_upgrade_is_noop(pg_url, scratch_db):
    """Accept: stamping the baseline on a create_all database and then
    running ``upgrade head`` changes nothing (0002-0004 are all guarded
    no-ops on the baseline schema; the version row ends at head)."""
    eng = create_engine(scratch_db)
    Base.metadata.create_all(eng)
    eng.dispose()
    before = _column_snapshot(scratch_db)

    _alembic(scratch_db, "stamp", BASELINE_REVISION)
    _alembic(scratch_db, "upgrade", "head")

    assert _column_snapshot(scratch_db) == before
    assert _version_rows(scratch_db) == [HEAD_REVISION]


def test_pg_bootstrap_fresh_is_idempotent(pg_url, scratch_db):
    """Accept: bootstrap on a fresh db runs ``upgrade head``; a second
    run changes nothing."""
    assert bootstrap(scratch_db).startswith("fresh database")
    assert _version_rows(scratch_db) == [HEAD_REVISION]
    snapshot = _column_snapshot(scratch_db)

    assert bootstrap(scratch_db).startswith("already under alembic")
    assert _column_snapshot(scratch_db) == snapshot
    assert _version_rows(scratch_db) == [HEAD_REVISION]


def test_pg_bootstrap_existing_is_idempotent(pg_url, scratch_db):
    """Accept: bootstrap on a legacy database (create_all + the dmp
    generated-column migration — the production state) stamps the
    baseline and lands at head; a second run changes nothing."""
    eng = create_engine(scratch_db)
    Base.metadata.create_all(eng)
    eng.dispose()
    from scripts.migrate_dmp_generated_columns import migrate as migrate_dmp

    migrate_dmp(scratch_db)

    assert bootstrap(scratch_db).startswith("existing database")
    assert _version_rows(scratch_db) == [HEAD_REVISION]
    snapshot = _column_snapshot(scratch_db)

    assert bootstrap(scratch_db).startswith("already under alembic")
    assert _column_snapshot(scratch_db) == snapshot
    assert _version_rows(scratch_db) == [HEAD_REVISION]


def test_pg_bootstrap_legacy_db_reaches_head(pg_url, scratch_db):
    """Accept (A2): a legacy db (create_all + all the migrate scripts
    run) → bootstrap → ``current`` shows head; ``upgrade head`` is a
    no-op. migrate_logic_names is a data-only migration bound to the
    app's own DATABASE_URL (no DDL, no revision) and is not part of
    the schema state here."""
    eng = create_engine(scratch_db)
    Base.metadata.create_all(eng)
    eng.dispose()
    from scripts.migrate_consent_column import migrate as migrate_consent
    from scripts.migrate_dmp_generated_columns import migrate as migrate_dmp
    from scripts.migrate_patient_columns import migrate as migrate_patient

    migrate_patient(scratch_db)
    migrate_consent(scratch_db)
    migrate_dmp(scratch_db)
    before = _column_snapshot(scratch_db)

    assert bootstrap(scratch_db).startswith("existing database")
    assert _version_rows(scratch_db) == [HEAD_REVISION]

    _alembic(scratch_db, "upgrade", "head")
    assert _column_snapshot(scratch_db) == before
    assert _version_rows(scratch_db) == [HEAD_REVISION]


def test_pg_bootstrap_refuses_plain_column_db(pg_url, scratch_db):
    """A create_all-only database (plain DMP key columns, the
    create_all-only fallback state) does not match the baseline — the
    bootstrap must refuse to stamp instead of recording a schema it does
    not have."""
    eng = create_engine(scratch_db)
    Base.metadata.create_all(eng)
    eng.dispose()

    with pytest.raises(SystemExit, match="not GENERATED"):
        bootstrap(scratch_db)
    assert not _has_alembic_version(scratch_db)
