# Syzygy — Sample Pairing Service

FastAPI + SQLAlchemy 2 service that pairs tumor and normal samples per
patient using pluggable pairing logic (TEMPO, IMPACT, and ACCESS are
seeded at startup).

## Setup

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt

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

## Tests

    pytest

Tests run against PostgreSQL via testcontainers when Docker is
available and fall back to an in-memory SQLite database otherwise.
