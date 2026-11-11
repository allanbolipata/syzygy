# NATS topics (MDB_STREAM)

Canonical list of the NATS JetStream subjects Syzygy consumes and publishes,
all on the single stream **`MDB_STREAM`**.

Naming convention: `MDB_STREAM.<server|consumers>.<source>.<event>`

- `server.*` — inbound gateway events destined for the app
- `consumers.*` — consumer-facing topics (the cohort flow)

Status legend:

- **live** — implemented on `dev`, e2e-tested (`scripts/e2e_nats.py`,
  `scripts/mock_smile.py`)
- **branch** — implemented on `feature/cohort-tempo`
- **pending** — agreed-in-principle but the route/naming is not settled

## Inbound — SMILE (cpt-gateway)

Live on `dev`. Consumed by the smile-listener (durable `syzygy` on
`MDB_STREAM.server.cpt-gateway.>`), POSTed to the app's internal
`POST /v1/ingest/nats`.

- **`MDB_STREAM.server.cpt-gateway.cmo-new-request`** — register a new CMO
  request: inserts the request and its samples (`file_group = SMILE`,
  parses `requestJson` into `requestMetadata` / `requestStatus`); triggers
  automatic pairing when a TUMOR is pairable with an existing NORMAL.
  Handler: `handle_new_request` in `nats_ingest.py`.
- **`MDB_STREAM.server.cpt-gateway.cmo-request-update`** — update an existing
  request (new `requestJson` / status) in place. Handler:
  `handle_request_update`.
- **`MDB_STREAM.server.cpt-gateway.cmo-sample-update`** — update a single
  sample (e.g. flip to TUMOR, update `sampleMetadataList`); can trigger
  pairing. Handler: `handle_sample_update`.

## Inbound — DMP sample (source: Acela) — ⚠️ pending

- **`MDB_STREAM.server.dmp-gateway.dmp-sample`** — upsert DMP pair-sample
  records as-is (`dmp_translator.to_dmp_sample_kwargs`).
  Handler: `handle_dmp_sample` in `nats_ingest.py`.
  **The actual source service is Acela, not a "dmp-gateway" — this route
  still needs to be established with them**, so the subject name (and the
  source tier in it) are open for change once agreed.

## Inbound — tempo (cohort flow)

On `feature/cohort-tempo`. Consumed by the listener's tempo client
(durable `syzygy-tmp` on `MDB_STREAM.consumers.tempo.>`).

- **`MDB_STREAM.consumers.tempo.cohort-validate`** — single cohort topic
  (consolidates the earlier `new-cohort-submit` + `cohort-update` split).
  Cohort request, new or update: JSON-schema validate → classify against the
  DB → write CRF only on a clean pass → stage the validation report.
  - The CRF is **never** written unless every sample passes (fail = report
    only, no CRF).
  - **CRF is locked in once made:** if the cohort's CRF already exists, the
    event is a header-only merge (replace the `#` header lines, keep the
    already-validated data rows verbatim — rows are never rewritten, even if
    the new payload's classification would differ). If it does not exist:
    full write. One topic suffices because an existing CRF is by
    construction fully validated, so a re-send can never resurrect bad rows.

## Outbound — syzygy → consumers

On `feature/cohort-tempo`. Published by the listener (fast path) plus a
background outbox flusher for retries; report carries `Nats-Msg-Id` =
cohort id (ccs_cohort_utils idempotency convention) and a
`Nats-Msg-Subject` header for legacy parity.

- **`MDB_STREAM.consumers.tempo.cohort-result`** — cohort validation report
  (pass, or problem-sample findings). **Placeholder name pending the
  meeting agreement.** Centralized in one place: the
  `x-cohort-result-subject` alias in `docker-compose.dev.yml` feeds both the
  app setting (`NATS_COHORT_RESULT_SUBJECT`) and the listener
  (`NATS_RESULT_SUBJECT`), so a rename is a one-line change.

## Durables

- `syzygy` — `MDB_STREAM.server.cpt-gateway.>` (legacy SMILE path)
- `syzygy-tmp` — `MDB_STREAM.consumers.tempo.>` (cohort path)

Both run in one listener process (`listener/run.py`), independently
supervised with restart backoff; separate JetStream fan-outs, so each keeps
its own redelivery position (shadow mode).

## Stream bootstrap

`listener/init_nats_stream.py` must create/update `MDB_STREAM` with **all 6
subjects** above.

> Resolved on `feature/cohort-tempo` (dev merge + topic collapse):
> `init_nats_stream.py` bootstraps all 6 subjects — 3 cmo +
> `dmp-sample` + `cohort-validate` + `cohort-result`. A stream that
> predates a subject is updated in place (the bootstrap adds missing
> subjects instead of recreating the stream).

## Open items

1. **Acela route (DMP sample):** establish the route with the Acela service;
   decide whether `MDB_STREAM.server.dmp-gateway.dmp-sample` is renamed to
   reflect the real source.
2. **`cohort-result` subject name:** confirm at the meeting; currently a
   placeholder (see single rename point above).

## Settled

- **Cohort topic:** one topic, `MDB_STREAM.consumers.tempo.cohort-validate`
  (was `new-cohort-submit` + `cohort-update`). The two settings
  (`nats_cohort_submit_subject` / `nats_cohort_update_subject`) collapse to
  one, and the two handlers become one.
- **CRF re-send semantics:** locked in — once a CRF is made, its data rows
  are never rewritten; later `cohort-validate` events only refresh the
  header (merge).
- **Mock coverage:** `scripts/mock_smile.py` pub/sub covers the 3 cmo +
  2 cohort subjects, plus a `cohort-validate` mode that builds a synthetic cohort
  request (locally validated against `schemas/cohort-request.schema.json`)
  and publishes it — pair with `sub cohort-result` to watch the report
  round-trip through the dev stack.
