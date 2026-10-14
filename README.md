# Syzygy — Sample Pairing Service

FastAPI + SQLAlchemy 2 service that pairs tumor and normal samples per
patient using pluggable pairing logic (TEMPO, IMPACT, and ACCESS are
seeded at startup).

## Setup

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt        # runtime
    pip install -r requirements-dev.txt    # runtime + test tooling

`requirements-dev.txt` includes the runtime requirements plus test
tooling (pytest, testcontainers). The Docker image installs only
`requirements.txt` (runtime deps).

Configuration comes from environment variables (see `Settings` in
`database.py`):

- `DATABASE_URL` — SQLAlchemy database URL
  (default `postgresql+psycopg2://postgres:postgres@localhost:5432/postgres`)
- `DEFAULT_PAIRING_LOGIC_NAME` — optional pairing logic applied
  automatically when samples are created

## Running

    uvicorn main:app --reload

At startup the app creates any missing tables and seeds the default
pairing logics. Note that `create_all` only creates — it never alters
existing tables. Schema changes are managed by Alembic (below).

## Migrations

- New database:

      alembic upgrade head

- Existing database whose schema was created by a previous `create_all`
  (matches the baseline):

      alembic stamp 0001_baseline
      alembic upgrade head

  This applies `0002_unique_latest_indexes`, which rebuilds the
  `uq_*_latest` indexes as unique partial indexes — required on
  databases that still carry the non-unique versions. If the database
  contains duplicate latest rows the migration will fail;
  deduplicate the data first.

- Existing database from before the pairing strategy layer
  (`pairing_logic` table lacks `strategy_name`/`config`):

      psql "$DATABASE_URL" -f migrations/0001_add_pairing_logic_strategy.sql
      alembic stamp 0001_baseline
      alembic upgrade head

  `migrations/0001_add_pairing_logic_strategy.sql` is the historical
  ad-hoc migration for that schema; once it has run, the schema
  matches the Alembic baseline. (It uses JSONB for `config`; the ORM's
  JSON type reads it fine via psycopg2.)

## API

- `GET /health` — liveness probe.
- `POST /v1/sample` — create a sample. Tumor samples are paired
  automatically against the latest matching normal; normal samples are
  paired against tumors that have no pairing yet. Optional
  `pairing_logic_id` / `pairing_logic_name` select the logic; otherwise
  the `DEFAULT_PAIRING_LOGIC_NAME` logic is used (inactive or missing
  logic skips auto-pairing).
- `GET /v1/sample?limit=&offset=` — list samples, newest first.
- `POST /v1/pairing/trigger` — create or override a pairing for a
  tumor. `trigger_type` is `manual` or `override`; optional `normal_id`
  pins a specific normal sample. An `override` without `normal_id`
  behaves like `manual` (re-pair against the latest matching normal).
- `GET /v1/pairing` — list pairings, newest first. Optional filters
  (combined with AND): `latest=true` restricts to current pairings;
  `sample_id` matches pairings where the sample is tumor or normal;
  sample fields `primary_id`, `cmo_sample_name`, `patient_id`,
  `sample_type`, `gene_panel`, `bait_set` match a sample on either side
  (when several are given, they must all match the same sample);
  `tumor_or_normal` scopes that match to the tumor or normal role;
  `pairing_logic_id` / `pairing_logic_name` select the logic;
  `created_after` / `created_before` (inclusive, ISO 8601) bound the
  pairing creation date; `limit` / `offset` paginate.
- `GET /v1/pairing-logic?limit=&offset=` — list pairing logics,
  newest first.
- `POST /v1/pairing-logic` — register a pairing logic. `strategy_name`
  must be a registered strategy (`default`, `IMPACT`, `TEMPO`, `ACCESS`).

## Tests

    pip install -r requirements-dev.txt
    pytest

Tests run against PostgreSQL via testcontainers when Docker is
available and fall back to an in-memory SQLite database otherwise.
