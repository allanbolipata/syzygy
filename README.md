# Syzygy — Sample Pairing Service

FastAPI + SQLAlchemy 2 service that pairs tumor and normal samples per
patient using pluggable pairing logic. The default strategy (used by
the seeded IMPACT and ACCESS logics) pairs by request first, then
within the patient; the `argos` strategy (WES) is aligned with
beagle's ARGOS v2.3.0 pairing logic (run-mode buckets and a
pooled-normal tier), and the `tempo` strategy (TEMPO_MPGEN / CHRONOS)
is aligned with beagle's tempo_patient.py (historical-pairing tier
first, then patient-wide bait/run-mode matching).

## Setup

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt        # runtime
    pip install -r requirements-dev.txt    # runtime + test tooling

`requirements-dev.txt` includes the runtime requirements plus test
tooling (pytest, testcontainers). The Docker image installs only
`requirements.txt` (runtime deps).

Configuration comes from environment variables or a local `.env` file
(gitignored; see `Settings` in `database.py`):

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
- `GET /v1/sample` — list samples, newest first. Optional filters
  (combined with AND): `q` matches any of `primary_id`,
  `cmo_sample_name`, `patient_id`, `request_id`; `primary_id`,
  `cmo_sample_name`, `patient_id`, `request_id`, `sample_type`,
  `gene_panel`, `bait_set`, `run_mode` are case-insensitive partial
  matches; `tumor_or_normal` is exact (`TUMOR`/`NORMAL`); `limit` /
  `offset` paginate.
- `GET /v1/sample/{sample_id}/possible-matches` — the sample's current
  match (latest pairing for the selected logic) plus all potential
  matches. Candidates are same-patient normals/tumors, plus pooled
  normals for strategies with a pooled tier (`argos`). Each candidate
  reports its cascade tier (`same_request`, `other_request`, `pooled`),
  whether it passes every matching key (`matched`), and the reasons it
  does not (`mismatched_keys`); for a tumor, `would_be_selected` marks
  the candidate the strategy cascade would pick. Supports manual
  pairing (e.g. pinning a DMP normal via `POST /v1/pairing/trigger`).
  Optional `pairing_logic_id` / `pairing_logic_name` selects the logic;
  otherwise the `DEFAULT_PAIRING_LOGIC_NAME` logic is used.
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
  must be a registered strategy (`default`, `argos`; `IMPACT`/`ACCESS`
  alias `default`, `TEMPO`/`CHRONOS` alias `tempo`). Payload fields:
  `name`, `description` (optional), `active` (default `true`),
  `strategy_name`, and `config` (optional per-logic strategy settings,
  see Pairing strategies). Duplicate names are rejected with 409.

## Front end

A basic static web UI (no build step, no extra dependencies) is served
at `/ui`; the root path `/` redirects there. It lets you:

- query and filter samples by the matching fields (patient, request,
  panel, bait set, run mode, sample type, role) with pagination;
- view a sample's current match and all potential matches per pairing
  logic (via `possible-matches`), with cascade tier, match status,
  mismatch reasons, and the candidate the cascade would pick;
- submit overrides: pin a candidate normal as the tumor's current
  pairing for the selected logic (`POST /v1/pairing/trigger` with
  `trigger_type=override`), optionally in strict mode, with the
  recorded mismatches shown afterwards.

## Pairing strategies

Strategies are registered in `pairing_strategies.py` and selected per
pairing logic via `PairingLogic.strategy_name`; a logic's `config`
dict customizes the strategy at use time.

- `default` — patient-scoped matching on hard keys
  (`patient_id`, `sample_type`, `gene_panel`, `bait_set`) with a
  request-first cascade: normals ingested under the tumor's own
  `request_id` are preferred, then all other normals of the same
  patient; the latest matching `sequencing_date` wins (tie-break on
  `created_date`, then `id`). `IMPACT` and `ACCESS` register as
  aliases of this strategy.
- `argos` — WES strategy aligned with beagle's ARGOS pairing logic
  (v2.3.0). Hard keys are (`patient_id`, `gene_panel`, `bait_set`);
  `sample_type` and preservation are not matching keys (beagle v2.3.0
  never gates on them; both remain stored on the sample). Extends
  `default` with:
  - **run-mode gating** — raw `run_mode` strings are normalized to
    buckets (default map: "novaseq x" → `novaseq_x`, "novaseq" →
    `novaseq`, "hiseq" → `hiseq`) and must be equal on both sides.
  - **pooled-normal tier** — when no patient-level match exists,
    shared pooled normals (`sample_type` in `pooled_sample_types`,
    default `["Pooled Normal"]`) are considered across patients,
    matched on `gene_panel` + `bait_set`; run mode is enforced only
    when the pooled sample carries one (run/machine data is not
    tracked for pooled samples).
  - Per-logic config keys: `run_mode_map` (raw substring → bucket,
    checked in order) and `pooled_sample_types`.
- `tempo` — TEMPO_MPGEN / CHRONOS strategy aligned with beagle's
  `tempo_patient.py`. A historical tier first: the tumor's existing
  latest pairing for the logic (syzygy's equivalent of beagle's
  `pre_pairing` / `pairing.tsv`) is retained by identity with
  bait/run-mode checks bypassed, and there is no fallback when the
  historical normal is missing. Then patient-wide matching on
  `bait_set` (case-insensitive) and run-mode bucket (default map:
  "novaseq" → `novaseq`, "hiseq" → `hiseq`; both "NovaSeq X 1000"
  and "NovaSeq 6000" land in `novaseq`); the latest `sequencing_date`
  wins. No request cascade, no pooled tier; `gene_panel` and
  `sample_type` are not keys. `TEMPO` and `CHRONOS` register as
  aliases of this strategy.

## Tests

    pip install -r requirements-dev.txt
    pytest

Tests run against PostgreSQL via testcontainers when Docker is
available and fall back to an in-memory SQLite database otherwise.
