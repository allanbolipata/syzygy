# ALEMBIC_TICKETS — versioned schema migrations

Goal: replace the "run the migrate scripts and hope" model with Alembic
(ordered revisions + a version table), while keeping the existing
idempotent scripts working during the transition. Motivated 2026-11-16:
staging ran a schema a migration behind and surfaced it as opaque 500s.

## Ground truth (verified 2026-11-16, dev branch @ 5bde2a1)

- Schema today: `Base.metadata.create_all` at app startup
  (`syzygy/main.py` lifespan) + standalone idempotent scripts
  `scripts/migrate_{patient_columns,consent_column,dmp_generated_columns,logic_names}.py`.
- `syzygy/schema_check.py` maps migration-added columns to scripts and
  the app logs boot warnings for missing ones (`tests/test_schema_check.py`).
- DB access: `syzygy/database.py` `settings.database_url`
  (pydantic-settings, `.env`); Postgres in dev/staging/prod, SQLite is
  the documented zero-Docker fallback (create_all only).
- The DMP key columns are `GENERATED ALWAYS ... STORED` on Postgres —
  Alembic autogenerate does NOT understand generated columns; those
  revisions must be hand-written (copy the expressions from
  `scripts/migrate_dmp_generated_columns.py`).
- Postgres-backed tests need a role with CREATEDB; the dev `app` role
  has it. Full local suite ≈ 680 tests / ~4.5 min.
- Sandbox note for workers: no network in the worker sandbox; the repo
  venv at `/home/allan/workspace/syzygy-dev/.venv` already has
  requirements + requirements-dev installed. Do NOT pip install.

## Ticket A1 — Alembic scaffold + baseline (codingbot, branch `dev`)

Build:
1. `pip`-free: add `alembic` to `requirements.txt` (pin the version
   already importable in the venv; if absent, `pip install alembic`
   locally is NOT possible in-sandbox — instead vendor nothing: check
   `.venv/bin/pip list | grep -i alembic` first; if missing, this card
   BLOCKS with reason `env:alembic-missing` rather than guessing).
2. `alembic init` into `alembic/` at repo root; wire `alembic/env.py` to
   `syzygy.database.settings.database_url` and `Base.metadata` (no
   hard-coded URL; target_metadata = Base.metadata).
3. Hand-write the **baseline revision** `0001_baseline` that represents
   the CURRENT full schema (all tables as create_all produces them on
   Postgres, including the four GENERATED columns and all indexes from
   `models.py` `__table_args__`). It must be idempotent-safe: baseline is
   meant to be *stamped*, not run, on existing databases.
4. `scripts/alembic_bootstrap.py`: for an existing database, stamp
   `0001_baseline` if `alembic_version` table is absent and the schema
   matches (spot-check via information_schema); for a fresh database,
   `upgrade head`. Idempotent, prints what it did.
5. README: extend the "Schema migrations" section with the Alembic
   workflow (new change = `alembic revision -m "..."`, review by hand,
   `alembic upgrade head`; migrate scripts remain for legacy DBs).
6. Tests `tests/test_alembic.py`: fresh scratch PG db (pattern:
   `tests/test_schema_check.py` fixture) → `upgrade head` produces a
   schema whose `information_schema.columns` snapshot equals
   `create_all`'s; stamp-then-upgrade on a create_all db is a no-op.
   Skip module when DATABASE_URL is not Postgres. Add the file to the
   `test-postgres` job list in `.github/workflows/tests.yml`.

Accept:
- `alembic upgrade head` on a fresh PG db == create_all schema (test).
- `python scripts/alembic_bootstrap.py` idempotent on both fresh and
  existing dbs (test or demonstrated in commit message with receipts).
- Existing `scripts/migrate_*.py` untouched and still passing their
  behavior; `tests/test_schema_check.py` still green.
- Full pytest suite green; commit trailer `Kanban-Card: <id>`.

## Ticket A2 — convert legacy scripts to revisions (codingbot, branch `dev`)

Build:
1. Add revisions `0002_patient_columns`, `0003_consent`,
   `0004_dmp_generated_columns` (hand-written; logic_names is a DATA
   migration — leave it as a script, note why in its docstring).
   Each revision's upgrade() must be a no-op when the change is already
   present (mirror the scripts' information_schema guards), so
   stamping-then-upgrading an already-migrated db is safe.
2. `scripts/alembic_bootstrap.py` extended: after stamping baseline on a
   legacy db, `upgrade head` applies only what is missing.
3. README: mark the four scripts as legacy shims ("equivalent Alembic
   revision: 000N") — scripts stay functional for one release cycle.

Accept:
- Legacy db (create_all + all scripts run) → bootstrap → `current`
  shows head; `upgrade head` no-op.
- Fresh db → `upgrade head` → schema snapshot equals create_all
  (extend tests/test_alembic.py).
- Full suite green; trailer on every commit.

## Gate (every code card)

`verify-worker` runs, in `/home/allan/workspace/syzygy-dev`:
`git status --porcelain` must be empty (worker committed), then
`.venv/bin/python -m pytest -q` — exit code is the verdict. If the venv
is broken (missing modules), the gate BLOCKS with `env:venv-broken`
instead of editing anything.
