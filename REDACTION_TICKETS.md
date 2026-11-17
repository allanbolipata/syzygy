# REDACTION_TICKETS — sample redaction (pairing exclusion)

Goal: samples can be **redacted** (flagged as excluded) and redacted
samples are ignored by pairing — automatically and by explicit trigger —
while still appearing in candidate lists, visibly marked as redacted.
Requested by Allan 2026-11-16 after ambiguous-normal pairing failures.

## Ground truth (verified 2026-11-16, dev branch)

- Strategies: `syzygy/pairing_strategies.py` — `DefaultPairingStrategy`
  (ACCESS), `ArgosPairingStrategy`, `TempoPairingStrategy` (subclass of
  Default), `DmpPairingStrategy` (multi-pair fan-out). Candidate
  selection entry point per strategy: `select_latest_normal(db, tumor,
  logic)`; DMP bulk path uses the sargable generated-key query.
- Service: `syzygy/pairing_service.py` — `trigger_pairing` (single,
  incl. pinned-normal override), `trigger_pairing_for_normal`,
  `trigger_pairing_for_unpaired_tumors` + `_run_bulk_pair_pass` (bulk),
  `trigger_dmp_pairing`, `trigger_harmony_pairing`, `possible_matches`
  (candidate listing, ~line 1092), `evaluate_logic`.
- Routers: `syzygy/routers/samples.py` (list filters incl. substring
  fields; `SampleRead` in `syzygy/schemas.py`), `pairings.py`
  (`/trigger`, `/trigger-all`, `/trigger-dmp`, `/trigger-harmony`,
  `/evaluate/`, `/possible-matches` — check exact route names in file).
- UI: static vanilla-JS front end under `static/` (samples list, sample
  detail, pair report). Follow existing patterns; no framework.
- Schema change ships as an Alembic revision (chain runs AFTER
  ALEMBIC_TICKETS A2): revision `0005_redaction`.
- Sandbox: no network; use `/home/allan/workspace/syzygy-dev/.venv`.

## DECIDED semantics (do not re-litigate in code; ask via block if stuck)

- New columns on `samples`: `redacted BOOLEAN NOT NULL DEFAULT FALSE`,
  `redacted_at TIMESTAMPTZ NULL`, `redacted_by TEXT NULL` (actor email),
  `redaction_reason TEXT NULL`.
- Redacted TUMOR: never auto-paired (bulk sweeps, trigger-all, DMP/HARMONY
  bulk must skip it). Explicit `POST /v1/pairing/trigger` on a redacted
  tumor → 400 "sample is redacted".
- Redacted NORMAL: never offered as a candidate by any strategy
  (automatic selection). Explicit pin (`normal_id` on trigger) of a
  redacted normal → 400. Un-redacting restores eligibility; existing
  pairings are NEVER auto-retired by redaction (pairing history is
  immutable) — the UI badges them instead.
- Candidate listing (`possible_matches`, `/evaluate/`): redacted normals
  ARE still listed, each carrying `redacted: true` (+ reason), per Allan:
  "can still show up as potential candidates, but shown as redacted".
- API: `POST /v1/sample/{id}/redact` body `{reason?: str}` and
  `POST /v1/sample/{id}/unredact`; idempotent (redacting a redacted
  sample returns 200 with current state). `SampleRead` gains the four
  fields; `GET /v1/sample/` gains filter `redacted=true|false`.
- Every redact/unredact call records an audit event IF the audit-log
  feature exists on the branch; on this branch (pre-audit) the columns
  `redacted_by`/`redacted_at` are the trail.

## Ticket R1 — backend (codingbot, branch `dev`)

Build:
1. Alembic revision `0005_redaction` (four columns; idempotent guard).
2. Model + `SampleRead`/schemas; list filter `redacted`.
3. `redact`/`unredact` endpoints in `routers/samples.py` (set
   `redacted_by` from the authenticated identity, same way other
   routers get the user).
4. Pairing exclusion: single choke point helper `is_pairable(sample)` in
   `pairing_service.py`; enforce in strategy candidate queries (add
   `redacted IS NOT NULL AND redacted = false`-style filter to each
   strategy's normal-candidate query and tumor bulk sweeps) AND in
   `trigger_pairing` for explicit pins (400 paths above).
5. `possible_matches`/`evaluate` include redacted candidates flagged.
6. Tests `tests/test_redaction.py`: per-strategy exclusion (default,
   argos, tempo, dmp), bulk sweep skips redacted tumors, explicit pin
   400s, redact/unredact round trip + idempotency, candidate listing
   shows redacted:true, existing pairing survives redaction.
7. README pairing section: one paragraph on redaction semantics.

Accept: full suite green; a redacted normal can never win automatic or
pinned pairing (test-proven); candidate lists still show it flagged;
trailer `Kanban-Card: <id>` on every commit.

## Ticket R2 — UI (codingbot, branch `dev`)

Build:
1. Samples list + detail: redaction badge (distinct, e.g. strikethrough
   id + "REDACTED" chip) and Redact/Un-redact control with reason input
   on detail; confirm dialog.
2. Candidate/pairing UI wherever `possible_matches`/evaluate results
   render: badge redacted candidates, disable the pair button for them.
3. Pair report rows: badge either side if redacted.
4. Follow existing static/ JS patterns; no new framework; keep it
   keyboard/click minimal.

Accept: badge visible in all three surfaces (manual check noted in
commit message); pair actions on redacted samples blocked in UI AND
still enforced server-side; suite green; trailer on commits.

## Gate (every code card)

Same as ALEMBIC_TICKETS gate: clean `git status --porcelain` +
`.venv/bin/python -m pytest -q` exit code; env problems → block with
`env:` prefix, never edit code.
