# Syzygy — Sample Pairing Service

FastAPI + SQLAlchemy 2 service that pairs tumor and normal samples per
patient using pluggable pairing logic. The default strategy (used by
the seeded TEMPO, IMPACT, and ACCESS logics) pairs by request first,
then within the patient; the `argos` strategy (WES) adds run-mode and
preservation gating and a pooled-normal tier.

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
pairing logics. `models.py` defines the complete schema; `create_all`
builds it for new databases and never alters existing tables.

## API

- `GET /health` — liveness probe.
- `POST /v1/sample` — create a sample. Tumor samples are paired
  automatically against the latest matching normal; normal samples are
  paired against tumors that have no pairing yet. Optional
  `pairing_logic_id` / `pairing_logic_name` select the logic; otherwise
  the `DEFAULT_PAIRING_LOGIC_NAME` logic is used (inactive or missing
  logic skips auto-pairing). Sample fields include `request_id`,
  `run_mode` (raw sequencer string, e.g. "NovaSeq X 1000"),
  `preservation` and `sample_origin` (raw preservation data, e.g.
  `["FFPE"]` or `"Fresh or Frozen"`), and `sequencing_date`.
- `GET /v1/sample?limit=&offset=` — list samples, newest first.
- `POST /v1/pairing/trigger` — create or override a pairing for a
  tumor. `trigger_type` is `manual`, `automatic`, or `override`.
  `normal_id` pins a specific normal sample: with it, the normal is
  paired even when matching keys differ and the differences are
  recorded in the pairing's `mismatched_keys`. Without it,
  `override` behaves like `manual` (re-pair against the latest
  matching normal via the strategy cascade). `strict=true` rejects a
  pinned `normal_id` with 400 when the strategy reports any mismatch
  instead of recording it.
- `GET /v1/pairing` — list pairings, newest first. Optional filters
  (combined with AND): `latest` (`true`/`false`, default `false`)
  restricts to current pairings; `sample_id` matches pairings where the
  sample is tumor or normal; `request_id`, `run_mode` (case-insensitive
  exact match), and the sample fields `primary_id`, `cmo_sample_name`,
  `patient_id`, `sample_type`, `gene_panel`, `bait_set` match a sample
  on either side (when several are given, they must all match the same
  sample); `tumor_or_normal` scopes that match to the tumor or normal
  role; `pairing_logic_id` / `pairing_logic_name` select the logic;
  `created_after` / `created_before` (inclusive, ISO 8601) bound the
  pairing creation date; `limit` / `offset` paginate. Pairings carry
  `trigger_type`, `mismatched_keys`, and `notes` (per-tier selection
  notes describing candidates considered and why others were skipped).
- `GET /v1/pairing-logic?limit=&offset=` — list pairing logics,
  newest first.
- `POST /v1/pairing-logic` — register a pairing logic. `strategy_name`
  must be a registered strategy (`default`, `argos`; `IMPACT`,
  `TEMPO`, and `ACCESS` are aliases of `default`). Payload fields:
  `name`, `description` (optional), `active` (default `true`),
  `strategy_name`, and `config` (optional per-logic strategy settings,
  see Pairing strategies). Duplicate names are rejected with 409.

## Pairing strategies

Strategies are registered in `pairing_strategies.py` and selected per
pairing logic via `PairingLogic.strategy_name`.

- `default` — request-first cascade: candidates are first restricted to
  the tumor's own `request_id`, then widened to other requests of the
  same patient. Matching requires the same `sample_type`, `gene_panel`,
  and `bait_set`; the latest matching normal wins (tie-break on
  `sequencing_date`, then `id`). `IMPACT`, `TEMPO`, and `ACCESS`
  register as aliases of this strategy.
- `argos` — extends `default` with:
  - **run-mode gating** — raw `run_mode` strings are normalized to
    buckets (default map: "NovaSeq X" → `novaseq_x`, "Hiseq" →
    `hiseq`, "NovaSeq 6000" → `novaseq_6000`) and must be equal on
    both sides.
  - **preservation gating** — `preservation`/`sample_origin` are
    resolved to `ffpe`/`frozen` (fail-safe: unresolvable values make
    the candidate ineligible, per the investigators' "both empty means
    fail" rule), and must be equal on both sides.
  - **pooled-normal tier** — when no patient-level match exists,
    shared pooled normals (`sample_type` in `pooled_sample_types`,
    default `["Pooled Normal"]`) are considered across patients,
    matched on `gene_panel` + `bait_set` + resolved preservation;
    run mode is enforced only when the pooled sample carries one
    (run/machine data is not tracked for pooled samples).
  - Per-logic config keys: `run_mode_map` (raw substring → bucket,
    checked in order) and `pooled_sample_types`.

## Tests

    pip install -r requirements-dev.txt
    pytest

Tests run against PostgreSQL via testcontainers when Docker is
available and fall back to an in-memory SQLite database otherwise.
