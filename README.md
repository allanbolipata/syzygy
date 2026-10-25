# Syzygy — Sample Pairing Service

FastAPI + SQLAlchemy 2 service that pairs tumor and normal samples per
patient using pluggable pairing logic. The `argos` strategy (used by
the seeded ARGOS logic, which also serves IMPACT data via name alias)
is aligned with beagle's ARGOS v2.3.0 pairing logic (run-mode buckets
and a pooled-normal tier); the `tempo` strategy (seeded TEMPO logic,
which also serves CHRONOS data via name alias) is aligned with
beagle's tempo_patient.py (historical-pairing tier first, then
patient-wide bait/run-mode matching); and the `default` strategy
(seed: ACCESS) pairs by request first, then within the patient.

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
pairing logics (`ARGOS` → `argos`, `TEMPO` → `tempo`, `ACCESS` →
`default`; idempotent). `models.py` defines the complete schema;
`create_all` builds it for new databases and never alters existing
tables.

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
- `GET /v1/patient` — patients derived from samples (a patient is a
  distinct non-null `patient_id`), sorted by `patient_id`. Returns
  sample/tumor/normal counts, the patient's distinct `request_id`s, and
  patient-level `metadata` (species, sex, oncotreeCode, diagnosisDate)
  derived from the samples' `additional_metadata` (first non-null value
  per key; tumor samples take priority). `q` is a case-insensitive
  partial match on `patient_id`; `limit` / `offset` paginate.
- `GET /v1/request` — requests derived from samples (a request is a
  distinct non-null `request_id`), sorted by `request_id`. Returns
  sample/tumor/normal counts, the request's distinct `patient_id`s, and
  the distinct `sample_type`s and `gene_panel`s carried by its samples.
  `q` is a case-insensitive partial match on `request_id`; `limit` /
  `offset` paginate.
- `GET /v1/sample/{sample_id}/possible-matches` — the sample's current
  match (latest pairing for the selected logic) plus all potential
  matches. Candidates are same-patient normals/tumors, plus pooled
  normals for strategies with a pooled tier (`argos`); the `argos`
  strategy additionally lists same-patient DMP samples (tier `dmp`) —
  DMP pairing is manual, so they are always reported as mismatches and
  exist purely as override targets. Each candidate
  reports its cascade tier (`same_request`, `other_request`, `dmp`,
  `pooled`),
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
  must be a registered strategy (`default`, `argos`, `tempo`;
  `ACCESS` aliases `default`, `IMPACT` aliases `argos`, and
  `TEMPO`/`CHRONOS` alias `tempo`). Logic *names* also resolve with
  aliases: `IMPACT` resolves to the `ARGOS` row and `CHRONOS` to the
  `TEMPO` row when no row carries the given name (exact names always
  win). Payload fields:
  `name`, `description` (optional), `active` (default `true`),
  `strategy_name`, and `config` (optional per-logic strategy settings,
  see Pairing strategies). Duplicate names are rejected with 409.

## Front end

A basic static web UI (no build step, no extra dependencies) is served
at `/ui`; the root path `/` redirects there. It lets you:

- query and filter samples by the matching fields (patient, request,
  panel, bait set, run mode, sample type, role) with pagination;
- browse patients: each patient's metadata, sample counts, and all
  their samples with their current match per pairing logic
  (`GET /v1/patient` + `GET /v1/pairing?latest=true`);
- browse requests: each request's sample counts, distinct patients and
  sample types, and all its samples and patients — clicking a sample
  opens its matches & overrides and clicking a patient opens the
  patient's view (`GET /v1/request` + `GET /v1/pairing?latest=true`);
- view a sample's current match and all potential matches per pairing
  logic (via `possible-matches`), with cascade tier, match status,
  mismatch reasons, and the candidate the cascade would pick; clicking
  a sample (from the samples, patients, or requests view) opens this
  view, pre-selecting the logic the sample is currently paired under
  when known;
- submit overrides: pin a candidate normal as the tumor's current
  pairing for the selected logic (`POST /v1/pairing/trigger` with
  `trigger_type=override`), optionally in strict mode, with the
  recorded mismatches shown afterwards.

A back button returns to the previous screen (in-app navigation
history; disabled at the start of a session).

## Authentication (Keycloak)

All `/v1` API endpoints require a Keycloak token. The app reads
`KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID` and
`KEYCLOAK_CLIENT_SECRET` (see `Settings` in `database.py`); in
production `KEYCLOAK_URL` points at an externally managed Keycloak.
The `keycloak` service in `docker-compose.yml` is strictly for local
development.

**Flow (browser):** a browser without a session lands on the `/login`
page, which redirects through `/auth/redirect` (adds a `state`
parameter) to Keycloak's authorize endpoint. After authenticating,
Keycloak returns to `/auth/callback`, which exchanges the
authorization code for a token, stores it in an `HttpOnly`
`auth_token` cookie, and sends the browser to `/ui`. API clients skip
the browser dance and send `Authorization: Bearer <token>` instead.
`GET /auth/logout` clears the cookie and returns to `/login`.

**Allow-list:** only emails that have an *active* row in
`allowed_users` may log in; a valid Keycloak token for anyone else
gets `403`. Provision users with:

    python scripts/manage_allowed_users.py add --email name@example.com

**Local development:**

1. `docker compose up -d db keycloak` — starts the dev-only Keycloak
   on **8081** (admin credentials `admin`/`admin`) and Postgres on
   **5435** (ports chosen to avoid local clashes).

2. One-time setup at <http://localhost:8081>, in the **master** realm:
   - **Clients** → create client `syzygy`: Client authentication
     **ON** (confidential), Standard flow **ON**, Valid redirect URI
     `http://localhost:8000/auth/callback`, Web Origins
     `http://localhost:8000`; then copy the client secret.
   - **Users** → create a user with a real email (e.g.
     `you@example.com`) and set a password.

   Keycloak runs in `start-dev` (in-memory) mode: a full
   `docker compose down` wipes the client, users, and secret, so
   repeat this step after a teardown.

3. Configure and run the app **on the host**, not in the compose
   `app` container — the token's `iss` claim is stamped from the
   URL the *browser* uses to reach Keycloak, so the app's
   `KEYCLOAK_URL` must equal that browser-side URL, which is
   unreachable from inside the app container and the code exchange
   would fail:

       cp .env.example .env   # then set KEYCLOAK_CLIENT_SECRET
       source .venv/bin/activate
       uvicorn main:app --reload

   The `.env.example` defaults already match the compose ports.

4. Allow-list the user:

       python scripts/manage_allowed_users.py add --email you@example.com

5. Open <http://localhost:8000/ui> and log in. In production, point
   `KEYCLOAK_URL` at the externally managed org Keycloak instead.

`/health`, `/`, `/ui`, `/docs` and `/openapi.json` stay public.

## Pairing strategies

Strategies are registered in `pairing_strategies.py` and selected per
pairing logic via `PairingLogic.strategy_name`; a logic's `config`
dict customizes the strategy at use time.

- `default` — patient-scoped matching on hard keys
  (`patient_id`, `gene_panel`, `bait_set`) with a
  request-first cascade: normals ingested under the tumor's own
  `request_id` are preferred, then all other normals of the same
  patient; the latest matching `sequencing_date` wins (tie-break on
  `created_date`, then `id`). `sample_type` is not a matching key
  (aligned with beagle v2.3.0). `ACCESS` registers as an alias of
  this strategy.
- `argos` — WES strategy aligned with beagle's ARGOS pairing logic
  (v2.3.0). Hard keys are (`patient_id`, `gene_panel`, `bait_set`);
  `sample_type` is not a matching key (beagle v2.3.0 never gates on
  it; it remains stored on the sample). Extends `default` with:
  - **run-mode gating** — raw `run_mode` strings are normalized to
    buckets (default map: "novaseq x" → `novaseq_x`, "novaseq" →
    `novaseq`, "hiseq" → `hiseq`) and must be equal on both sides; in
    the pooled tier the pool's machine name resolves through
    `pooled_machines` (machine class `novaseq`/`hiseq`), which must
    match the tumor's run-mode family (a NovaSeq X run is a NovaSeq
    machine).
  - **preservation gating** — each side's `preservation` /
    `sample_origin` must resolve to the same category, fuzzily
    (case-insensitive): preservation `FFPE` → ffpe, anything else →
    frozen; `sample_origin` (checked when preservation is empty)
    resolves `FFPE` → ffpe and `Fresh or Frozen` / `frozen` →
    frozen. When both fields are present they must agree; a sample
    that is unresolvable, or that disagrees with the other side,
    blocks the pair — including in the pooled tier.
  - **pooled-normal tier** — when no patient-level match exists,
    shared pooled normals (`sample_type` in `pooled_sample_types`,
    default `["Pooled Normal"]`) are considered across patients,
    matched on `gene_panel` + `bait_set`; run mode is ignored for
    pooled matches (run/machine data is not tracked), while the
    preservation gate still applies.
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
