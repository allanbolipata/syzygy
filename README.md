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

At startup the app creates any missing tables and seeds, idempotently:
the default pairing logics (`ARGOS` → `argos`, `TEMPO` → `tempo`,
`ACCESS` → `default`), pooled-machine reference rows for the `argos`
pooled tier (`pairing_strategies.seed_pooled_machines`), and bait-set →
primary pairing logic assignments (`bait_sets.seed_bait_sets`; explicit
lists of known TEMPO and ACCESS bait sets). `models.py` defines the
complete schema; `create_all` builds it for new databases and never
alters existing tables.

## Setting up on a new machine

Self-contained walkthrough from a fresh checkout to a working app.
Prerequisites: Docker (engine + Compose plugin v2 — Docker Desktop on
macOS/Windows, or the CLI on Linux) and Python 3.11+.

### Full local environment (app + database + Keycloak)

1. Clone and configure:

       git clone git@github.com:allanbolipata/syzygy.git
       cd syzygy
       cp .env.example .env

2. Start the dev stack (app on **8000**, syzygy Postgres on
   **5435** — the default 5432 is commonly owned by the host's own
   Postgres — and dev Keycloak on **8080** with its own Postgres):

       docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

   The first run builds the app image and creates the shared
   `syzygy-dev` network plus two named volumes (`syzygy_db_data`,
   `syzygy_keycloak_db_data`). **That is where all local data
   persists** — `down`/`up` cycles keep it; only `down -v` wipes it.

3. One-time Keycloak setup at <http://localhost:8080> (**master**
   realm, `admin`/`admin`): create the confidential client `syzygy`
   with redirect URI `http://localhost:8000/auth/callback` and web
   origin `http://localhost:8000`; copy its client secret into
   `.env` (`KEYCLOAK_CLIENT_SECRET`); create your user. Then
   **log out of Keycloak** (http://localhost:8080/realms/master/account)
   — the browser otherwise keeps the `admin` session, and app logins
   silently re-authenticate as admin (no email → `403 User not
   allowed`) without ever showing the login form.

4. Run the app **on the host**, not in the compose `app` container
   (the token's `iss` claim must equal the browser-side Keycloak
   URL, which the container cannot use — see Authentication below):

       python3 -m venv .venv && source .venv/bin/activate
       pip install -r requirements.txt
       docker compose -f docker-compose.yml -f docker-compose.dev.yml stop app
       uvicorn main:app --reload

5. Allow-list your user and log in:

       python scripts/manage_allowed_users.py add --email you@example.com
       # then open http://localhost:8000/ui

   To run the test suite: `pip install -r requirements-dev.txt` then
   `pytest tests/` (it spins up its own Postgres via testcontainers,
   independent of the compose DB).

### Against an existing Postgres (no local database)

Per the Running section, the app builds the schema on startup
(`create_all`) and seeds idempotently — **an empty database is fine,
no migration step is needed on this branch** (alembic lives on a
separate branch). The connecting role just needs `CREATE` privilege
on the database. Point `DATABASE_URL` at it:

    DATABASE_URL=postgresql+psycopg2://user:pass@db-host:5432/dbname \
    KEYCLOAK_URL=... KEYCLOAK_CLIENT_SECRET=*** \
    docker compose -f docker-compose.yml up -d

The production base file bundles no Postgres and no Keycloak, so this
is also the production deployment shape. For *local development*
against an existing database, drop the `db` service (and the `app`'s
`depends_on` on it) from `docker-compose.dev.yml` and set
`DATABASE_URL` there instead — the dev Keycloak still comes from the
override file.

## Docker compose

The deployment is split into a base file and a local-development
override:

| File | Services | Role |
|------|----------|------|
| `docker-compose.yml` | `app` (**8000**) only | **Production base.** Wires the app to an external database (`DATABASE_URL`, required) and an external Keycloak (`KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`). It bundles neither. |
| `docker-compose.dev.yml` | adds `db` (syzygy Postgres, published on **5435**), `keycloak` (**8080**) and `keycloak-db` (Keycloak's own Postgres, not published on the host) | **Local development override.** Layered on the base: adds the two services the app needs locally and overrides the app's env with dev values. |

Both files declare the project name `syzygy` and share one network,
`syzygy-dev`, so every command passes both files.

**Local development:**

    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

Tear down (add `-v` to also delete the dev Postgres data volumes):

    docker compose -f docker-compose.yml -f docker-compose.dev.yml down

**Production:** run the base file alone, with the required variables
set on the command line (shell environment wins over any `.env` in
this directory):

    DATABASE_URL=postgresql+psycopg2://user:pass@db-host:5432/app \
    KEYCLOAK_URL=https://keycloak.example.org \
    KEYCLOAK_CLIENT_SECRET=*** \
    docker compose -f docker-compose.yml up -d

Point `KEYCLOAK_URL` at the externally managed org Keycloak and
`DATABASE_URL` at the deployment's Postgres. Every value in the base
file's `app` environment is overridable, so deployment-specific
values can be injected the same way without editing the compose
files.

### Pointing Postgres at a database

The dev Postgres services (`db` and `keycloak-db`) store their data
in `/var/lib/postgresql/data`. Three ways to point that at a
database:

1. **Named volume (the default).** The dev override declares
   top-level volumes (`db_data`, `keycloak_db_data`) and mounts them,
   e.g. `- db_data:/var/lib/postgresql/data`. Docker/Podman keeps the
   volume outside the repo and it survives container recreation;
   `docker compose down -v` deletes it.
2. **An existing directory on the host (bind mount).** Replace the
   named volume with a host path:

       volumes:
         - ./pgdata:/var/lib/postgresql/data
         # or an absolute path:
         # - /var/lib/postgres/syzygy/app:/var/lib/postgresql/data

   Relative paths resolve against the compose file's directory. The
   directory may be empty (Postgres initializes it on first start) or
   already hold a cluster built by a compatible PostgreSQL (the
   container adopts it as-is). The cluster data is written as the
   `postgres` user (uid 999): on a Linux host, chown the directory
   first (`chown -R 999:999 <dir>` as root) so the container's
   entrypoint does not have to re-chown it. On macOS (Docker
   Desktop), bind mounts to the data directory do not work: the VM
   mount rejects `chown` and macOS will not chown to uid 999 without
   root, so the entrypoint aborts with `chown: Permission denied` —
   use the named volume instead. Point at most one stack at a given
   directory.
3. **An existing database, no compose Postgres.** The production base
   file does exactly this: `docker-compose.yml` declares no `db`
   service at all — set `DATABASE_URL` on the command line (or in the
   `app` service's `environment` block) to point anywhere: a host
   Postgres, a managed cluster, or another container. To run the dev
   stack against an existing database instead, drop the `db` service
   (and the `app` service's `depends_on` on it) from
   `docker-compose.dev.yml` and set `DATABASE_URL` there instead. If
   that database's container sits on `syzygy-dev`, reference it by
   its service or container name in the URL.

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
  `["FFPE"]` or `"Fresh or Frozen"`), `sequencing_date`, and
  `file_group` (provenance: the source beagle file group for imported
  rows).
- `GET /v1/sample` — list samples, newest first. Optional filters
  (combined with AND): `q` matches any of `primary_id`,
  `cmo_sample_name`, `patient_id`, `request_id`; `primary_id`,
  `cmo_sample_name`, `patient_id`, `request_id`, `sample_type`,
  `gene_panel`, `bait_set`, `run_mode`, `file_group` are
  case-insensitive partial matches; `tumor_or_normal` is exact
  (`TUMOR`/`NORMAL`); `no_patient=true` restricts to samples with a
  null `patient_id` (e.g. pooled normals); `limit` / `offset`
  paginate.
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
  match(es) plus all potential matches: `current_matches` lists every
  current pairing (`current_match` keeps the first one, for backward
  compatibility; a normal can be currently paired to several tumors).
  Candidates are same-patient normals/tumors, plus pooled normals for
  strategies with a pooled tier (`argos`); the `argos` strategy
  additionally lists same-patient DMP samples (tier `dmp`) — DMP
  pairing is manual, so they are always reported as mismatches and
  exist purely as override targets. Each candidate reports its cascade
  tier (`same_request`, `other_request`, `dmp`, `pooled`), whether it
  passes every matching key (`matched`), and the reasons it does not
  (`mismatched_keys`); for a tumor, `would_be_selected` marks the
  candidate the strategy cascade would pick. `other_matches` is a
  bounded page (`limit`/`offset`; default `limit` 500, max 5000);
  `other_matches_total` / `other_matches_matched` /
  `other_matches_tiers` / `other_matches_breakdown` describe the full
  candidate scope regardless of the page. Supports manual pairing (e.g.
  pinning a DMP normal via `POST /v1/pairing/trigger`). Optional
  `pairing_logic_id` / `pairing_logic_name` selects the logic; when
  neither is given, the sample's bait-set-assigned primary logic is
  used (see the bait-set endpoints below) and `DEFAULT_PAIRING_LOGIC_NAME`
  only when no bait-set assignment exists.
- `POST /v1/pairing/trigger` — create or override a pairing for a
  tumor. `trigger_type` is `manual`, `automatic`, or `override`.
  `normal_id` pins a specific normal sample: with it, the normal is
  paired even when matching keys differ and the differences are
  recorded in the pairing's `mismatched_keys`. Without it, `automatic`
  keeps the tumor's existing pairing when one exists and runs the
  strategy cascade only when it does not; `manual` (and `override`,
  which behaves like `manual`) always re-run the cascade against the
  latest matching normal. `strict=true` rejects a pinned `normal_id`
  with 400 when the strategy reports any mismatch instead of
  recording it.
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
- `GET /v1/machines` — list pooled-machine reference rows
  (`machine_name`, `machine_class`, `machine_type`), ordered by
  machine name.
- `POST /v1/machines` — create or update a pooled-machine row (upsert
  on `machine_name`, case-insensitive; `machine_name` and
  `machine_class` required, `machine_type` optional; 400 when a
  required field is blank). These rows resolve a pooled normal's raw
  `run_mode` (its machine name) to a machine class (`novaseq`/`hiseq`)
  for the `argos` pooled tier's run-mode check (see Pairing
  strategies). Seeded at startup by `seed_pooled_machines` (idempotent;
  existing rows are left untouched).
- `GET /v1/bait-sets` — list bait-set → primary pairing logic
  assignments, ordered by bait-set name.
- `GET /v1/bait-sets/lookup?name=...` — the assigned logic for a bait
  set. Unregistered names matching a naming rule (`ACCESS` in the name
  → ACCESS; `HEMEPACT` or `IMPACT` in the name → ARGOS) are registered
  on first lookup; returns `null` when no assignment exists.
- `POST /v1/bait-sets` — create or reassign a bait set's primary
  pairing logic (`pairing_logic_name`; 404 when no logic carries that
  name). Bait-set assignments decide the logic `possible-matches` uses
  when no logic is passed explicitly. Seeded at startup by
  `seed_bait_sets` from explicit lists of known TEMPO and ACCESS bait
  sets (idempotent; existing rows are left untouched).

## Front end

A basic static web UI (no build step, no extra dependencies) is served
at `/ui`; the root path `/` redirects there. It has tabs for samples,
patients, requests, pooled normals, pairs by logic, and matches &
overrides, and lets you:

- query and filter samples (search plus per-field filters on
  primary_id, cmo_sample_name, patient, request, sample type, gene
  panel, bait set, run mode, file group, and role) with pagination;
- browse patients: each patient's metadata, sample counts, and all
  their samples with their current match per pairing logic
  (`GET /v1/patient` + `GET /v1/pairing?latest=true`);
- browse requests: each request's sample counts, distinct patients and
  sample types, and all its samples and patients — clicking a sample
  opens its matches & overrides and clicking a patient opens the
  patient's view (`GET /v1/request` + `GET /v1/pairing?latest=true`);
- browse pooled normals: the shared patient-less pooled normals
  (`sample_type` "Pooled Normal", null `patient_id` — the ARGOS
  cascade's last-resort tier) with search and field filters (gene
  panel, bait set, run mode, file group) and pagination (`GET
  /v1/sample` with `no_patient=true`); the same view shows the
  pooled-machine reference table and lets you add or edit machines
  (`GET /v1/machines` + `POST /v1/machines`, upsert on machine name);
- view a sample's current match and all potential matches per pairing
  logic (via `possible-matches`), with cascade tier, match status,
  mismatch reasons, and the candidate the cascade would pick; the
  Matches & Overrides tab picks a sample by search, and clicking a
  sample (from the samples, patients, requests, or pooled view) opens
  it directly, pre-selecting the logic the sample is currently paired
  under when known;
- browse pairs by logic: pick a pairing logic to list its current
  pairs (tumor `primary_id`, normal `primary_id`, bait set —
  `GET /v1/pairing?latest=true`) and download the table as CSV or TSV;
- submit overrides: pin a candidate normal as the tumor's current
  pairing for the selected logic (`POST /v1/pairing/trigger` with
  `trigger_type=override`), optionally in strict mode, with the
  recorded mismatches shown afterwards.

Navigation state lives in the URL hash (`#/samples`,
`#/patients/<patient_id>`, `#/requests/<request_id>`,
`#/matches/<primary_id>`, `#/pooled`, `#/pairs`), so the browser
Back/Forward buttons, page reloads, and shared links all work; every
navigation pushes a history entry.

## Authentication (Keycloak)

All `/v1` API endpoints require a Keycloak token. The app reads
`KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID` and
`KEYCLOAK_CLIENT_SECRET` (see `Settings` in `database.py`); in
production `KEYCLOAK_URL` points at an externally managed Keycloak.
The dev Keycloak in `docker-compose.dev.yml` is strictly for local
development.

**Flow (browser):** a browser without a session lands on the `/login`
page, which redirects through `/auth/redirect` (adds a `state`
parameter) to Keycloak's authorize endpoint. After authenticating,
Keycloak returns to `/auth/callback`, which exchanges the
authorization code for a token, stores it in an `HttpOnly`
`auth_token` cookie, and sends the browser to `/ui`. API clients skip
the browser dance and send `Authorization: Bearer <token>` instead.
`GET /auth/logout` clears the cookie and returns to `/login`;
`GET /auth/me` returns the current user (`sub`, `email`,
`display_name`) for a valid token — the UI uses it to show the
signed-in user and sign out (it is also mounted at `/v1/auth/me`).

**Allow-list:** only emails that have an *active* row in
`allowed_users` may log in; a valid Keycloak token for anyone else
gets `403`. Provision users with:

    python scripts/manage_allowed_users.py add --email name@example.com

**Local development:**

1. Start the dev stack (app on **8000**, Keycloak on the default port
   **8080** with admin credentials `admin`/`admin`, syzygy Postgres
   on **5435** — the host's own Postgres occupies the default
   5432 — and Keycloak on its own Postgres):

       docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

2. One-time setup at <http://localhost:8080>, in the **master** realm:
   - **Clients** → create client `syzygy`: Client authentication
     **ON** (confidential), Standard flow **ON**, Valid redirect URI
     `http://localhost:8000/auth/callback`, Web Origins
     `http://localhost:8000`; then copy the client secret.
   - **Users** → create a user with a real email (e.g.
     `you@example.com`) and set a password.
   - **Log out of Keycloak** afterwards (top-right of the admin
     console, or <http://localhost:8080/realms/master/account>). The
     browser keeps the `admin` session, and app logins silently
     re-authenticate as admin (no email → `403 User not allowed`)
     without ever showing the login form. If it happens anyway, the
     login page shows the reason (e.g. *"'admin' has no email
     address — the allow-list is per-email"*); log out of Keycloak
     and sign in again.

   Keycloak stores this setup in its own Postgres (volume
   `keycloak_db_data`), so it survives `docker compose down`; only
   `down -v` wipes it.

3. Configure and run the app **on the host**, not in the compose
   `app` container — the token's `iss` claim is stamped from the
   URL the *browser* uses to reach Keycloak, so the app's
   `KEYCLOAK_URL` must equal that browser-side URL, which is
   unreachable from inside the app container and the code exchange
   would fail. Stop the compose app first so the host app owns
   port 8000:

       docker compose -f docker-compose.yml -f docker-compose.dev.yml stop app
       cp .env.example .env   # then set KEYCLOAK_CLIENT_SECRET
       source .venv/bin/activate
       uvicorn main:app --reload

   The `.env.example` defaults already match the compose ports.

4. Allow-list the user:

       python scripts/manage_allowed_users.py add --email you@example.com

5. Open <http://localhost:8000/ui> and log in. In production, point
   `KEYCLOAK_URL` at the externally managed org Keycloak instead
   (see Docker compose).

`/health`, `/`, `/login`, `/ui`, `/docs`, `/redoc` and
`/openapi.json` stay public, as do the OAuth dance paths
`/auth/redirect`, `/auth/callback` and `/auth/logout`; `/auth/me`
requires a token.

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
