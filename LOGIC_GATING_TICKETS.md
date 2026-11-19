# LOGIC_GATING_TICKETS.md — per-user pairing-logic visibility gating

Spec + ticket contract for the software-factory lane. Approved design direction:
Allan, 2026-11-16. Orchestrator: sevenofnine. Lane board: `syzygy-gating`.
Repo worktree: `/home/allan/workspace/syzygy-gating`, branch `feature/logic-gating`
(off `dev` @ `f73bf0a`).

## Goal

Specific users must be able to view ARGOS vs DMP vs HARMONY (and ACCESS/TEMPO)
data, properly auth-gated server-side. Today auth is binary: verified Keycloak
token + `allowed_users` email row → whole app. This lane adds a second
dimension: **which pairing logics' data an identity may see**, and separates
read from write.

## Design (decided — do not re-litigate in cards)

1. **Entitlement source: Keycloak groups → token claim.**
   Realm groups `syzygy/logic/argos`, `syzygy/logic/dmp`, `syzygy/logic/harmony`,
   `syzygy/logic/access`, `syzygy/logic/tempo`, plus `syzygy/admin`.
   A Keycloak protocol mapper emits a claim:
   `"syzygy_logics": ["ARGOS","DMP","HARMONY"]` (uppercase logic names; admin is
   a separate boolean claim `"syzygy_admin": true` OR membership of `syzygy/admin`
   mapped to a second claim — C1 implements claim parsing for BOTH shapes:
   list claim + boolean admin claim).
   **Claim names are CONFIGURATION, not code** (Allan's decision 2026-11-16,
   option A: the org's Keycloak approach is not yet decided):
   `Settings.logic_claim_name: str = "syzygy_logics"` and
   `Settings.logic_admin_claim: str = "syzygy_admin"` (envs `LOGIC_CLAIM_NAME`,
   `LOGIC_ADMIN_CLAIM`). When the org's Keycloak reality is known, adapting is
   a `.env` change — no code churn. The whole lane dark-launches behind
   `LOGIC_GATING_ENABLED=false` and may merge to `dev` inert.
2. **Rollout flag:** `Settings.logic_gating_enabled: bool = False`
   (env `LOGIC_GATING_ENABLED`). When false, behavior is exactly today's
   (everyone sees everything) — the whole lane is dark-launched.
3. **Fail-closed when enabled:** gating on + no `syzygy_logics` claim → empty
   set → no logic data (mirrors `NATS_INGEST_TOKEN` fail-closed precedent).
   Dev-token path (`DEV_AUTH_TOKEN`): dev-admin principal gets ALL logics +
   admin (synthetic local identity).
4. **Enforcement layers (server-side; UI is UX only):**
   - **Layer A — logic-scoped endpoints:** dependency factory
     `require_logic_access(param)` in `syzygy/auth.py`: reads the requested
     logic from the endpoint's existing `pairing_logic_name` path/query param,
     403 `detail="Logic not accessible"` when not in the principal's set.
   - **Layer B — data-level listing filter:** `GET /v1/sample/`, `/v1/patient/`,
     `/v1/request/`, `/v1/pairing/` listings must intersect the principal's
     allowed logics with row membership using the EXISTING
     `bait_sets.logic_membership_condition` (bait set → logic family, incl. the
     DMP null-bait rule). No new schema.
   - **Layer C — write/admin gate:** trigger/override/config endpoints require
     the admin entitlement, not mere visibility: `POST /v1/pairing/trigger*`,
     `/v1/pairing-logic/*` mutations. Read ≠ write.
5. **`/auth/me`** gains `"allowed_logics": [...]` and `"is_logic_admin": bool`
   (full set + true when gating disabled, so the UI needs no flag knowledge).
6. **Patients tab DMP-only view:** new group listing over
   `patient__dmp WHERE patient__cmo IS NULL` (the pure-DMP patients that are
   invisible today), shown only when the principal can see DMP. UI: logic
   selector + tabs gate on `/auth/me` values; DMP pair-report links keep the
   existing conditional behavior.

## Endpoint inventory (verified on dev @ f73bf0a; C2 must re-enumerate from source)

Logic-scoped reads (`pairing_logic_name` param): `/v1/pairing/report/`,
`/v1/pairing/report-stats/`, `/v1/pairing/export/`, `/v1/pairing/evaluate/`,
`/v1/pairing/unmatched/`, `/v1/pairing/unmatched-count/`, `/v1/pairing/harmony/`.
Writes (admin): `/v1/pairing/trigger`, `/v1/pairing/trigger-all`,
`/v1/pairing/trigger-dmp`, `/v1/pairing/trigger-harmony`, `/v1/pairing-logic/`
mutations. Listings (Layer B): `/v1/sample/`, `/v1/patient/`, `/v1/request/`,
`/v1/pairing/`. C2/C3 workers: enumerate the real signatures from
`syzygy/routers/*.py` — do not trust this table blindly.

## Verified lane facts (do not re-derive)

- Baseline suite on this branch (post-#30 rebase, b35b0ea+): **636 passed,
  17 skipped** in ~4m15s (`pytest -q` with the worktree `.venv`). Gate rule:
  **0 failed, skipped ≤ 17, `git status --porcelain` empty**. Passed count
  GROWS as cards add tests. (Original pre-#30 baseline was 612/17.)
- `tests/test_pairing_service.py` degrades to SQLite when testcontainers/Docker
  is unavailable — the suite is safe in the network-disabled worker sandbox.
- NATS e2e tests self-skip via `skipif` when env absent.
- Repo test convention: direct-call of endpoint functions with in-memory SQLite
  (`StaticPool`), `limit`/`offset` passed explicitly, `Response()` captures
  `X-Total-Count`. Auth in tests: override `get_current_user` dependency.
- Root `conftest.py` force-clears `settings.dev_auth_token` per test — new
  gating tests must follow the same monkeypatch discipline for
  `logic_gating_enabled` (default False keeps ALL existing tests green).
- `bait_sets.logic_membership_condition` exists (used by the `logic=` filter in
  `routers/samples.py`) — reuse it; do not re-implement bait→logic SQL.
- Sandbox redaction pitfall: never write literal `user:pass@` strings in
  commands that create files (they get rewritten to `***` before execution).
- `.venv` is pre-built in the worktree — use `/home/allan/workspace/syzygy-gating/.venv/bin/python`.

## Gate contract (every V-n card)

```
cd /home/allan/workspace/syzygy-gating
git status --porcelain            # must print NOTHING (code card must have committed)
.venv/bin/python -m pytest -q     # must exit 0; 0 failed; skipped <= 17
```
Verdict = exit code. Never edit code or tests. Never complete unless exit 0.

## Tickets

### C1 — auth core: claim → principal entitlements
**Build:** `Settings.logic_gating_enabled` (+ env `LOGIC_GATING_ENABLED`),
`Settings.logic_claim_name` (default `syzygy_logics`),
`Settings.logic_admin_claim` (default `syzygy_admin`); parse the list claim +
admin bool claim **by the configured names** in `verify_token`/`_resolve_user`; attach `allowed_logics: set[str]` +
`is_logic_admin: bool` to the principal dict; gating-disabled ⇒ full set;
dev-admin ⇒ full set + admin; unknown logic names in claim are ignored (log a
warning); `/auth/me` exposes both fields.
**Accept:** unit tests (direct-call, synthetic claims): claim present → set
exact; claim absent + gating on → empty set; gating off → full set; dev token
→ full + admin; `/auth/me` shape; existing suite green (612+).

### C2 — Layer A + C: endpoint gating dependencies
**Build:** `require_logic_access` dependency factory in `auth.py`; wire to all
logic-scoped read endpoints (403 when logic not in `allowed_logics`); separate
`require_logic_admin` for trigger/config writes (403 when not
`is_logic_admin`). Both no-ops when gating disabled.
**Accept:** per-endpoint 200/403 matrix tests via `app.dependency_overrides`
of `get_current_user` (at least: report, evaluate, one trigger, one
pairing-logic mutation); 403 body `detail` stable string; disabled-flag
passthrough test; existing suite green.

### C3 — Layer B: listing data filter
**Build:** in samples/patients/requests/pairings listings, when gating
enabled, AND the principal's allowed-logics membership into the query via
`logic_membership_condition` (OR across allowed logics; DMP null-bait rule
comes free from the helper). Principal arrives via `Depends(get_current_user)`.
**Accept:** tests: DMP-only sample invisible to ARGOS-only user, visible to
DMP user; X-Total-Count reflects the gated set (convention: totals are
pre-pagination); disabled-flag passthrough; existing suite green.

### C4 — Patients tab: DMP-only group view
**Build:** `/v1/patient/` gains `dmp_only: bool = False` param → groups by
`patient__dmp` where `patient__cmo IS NULL` (same row shape + X-Total-Count
convention); gated on DMP visibility when gating enabled; UI: Patients tab
toggle (CMO groups / DMP-only groups) shown only when `allowed_logics`
includes DMP; DMP-only rows link to Samples search prefilled with the dmp id
(works since dev @ 4504257: q now matches patient__dmp).
**Accept:** backend tests for the new grouping (totals, q filter, gating);
static-UI test asserting the toggle exists and is gated on the /auth/me field
(follow `tests/test_pair_report_links.py` static-convention); suite green.

### C5 — UI gating + docs
**Build:** app.js: logic selector options filtered by `/auth/me`
`allowed_logics`; gated views hidden (UX only); admin-only buttons (triggers)
hidden unless `is_logic_admin`. Docs: `docs/logic_gating_setup.md` — Keycloak
group + mapper walkthrough **written as a REFERENCE IMPLEMENTATION** (the
org's Keycloak approach is undecided; state that up front, show the
group-mapper recipe with the default claim names, and point at
`LOGIC_CLAIM_NAME`/`LOGIC_ADMIN_CLAIM` as the adaptation knob), env
flag, rollout order (populate groups → flip flag); README auth section pointer.
**Accept:** static-UI tests for selector filtering; docs complete and
copy-pasteable; suite green.

### R — final reviewer (parent = V5)
Judge C1–C5 against each Accept list + this spec. Verdict metadata
`{"verdict":"APPROVE"}` or `{"verdict":"ISSUES","issues":[...]}`. Never edits.

### H — human tail (parent = R, lands blocked, `review-required:`)
Allan-only steps: create the Keycloak groups + mapper (dev + prod), populate
test users, set `LOGIC_GATING_ENABLED=true` in dev, UAT the three personas
(ARGOS-only, DMP-only, admin), then merge `feature/logic-gating` → `dev`
(rebase onto current dev; #29 already merged). Card posts handoff comment and
blocks with `review-required:` prefix; completes only after verifying the merge
landed.

## Lane shape

`C1 → V1 → C2 → V2 → C3 → V3 → C4 → V4 → C5 → V5 → R → H`
Code cards: assignee `codingbot`, `--workspace dir:/home/allan/workspace/syzygy-gating`.
Gate cards: assignee `verify-worker`, scratch workspace, body = gate contract +
ticket's Accept list. R: `reviewer`. H: `sevenofnine`, blocked.
C1 is created `--initial-status blocked` — the go-gate; unblocking C1 is Allan's
go button.
