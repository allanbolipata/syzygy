# AUDIT_LOG_TICKETS — operations & audit trail (branch `feat/audit-log`)

Goal (Allan 2026-11-16): a pro-user Operations section in the UI for
pairing activities (incl. TEMPO pair assignment, currently CLI-only via
`scripts/import_tempo_pairs.py`), with a complete, transparent,
**append-only audit log** of every user action (override, redact,
pairing, assignment) and every system event (NATS message ingested,
cohort result published, automatic pairing performed). Visibility is
scoped: users see activity for the pairing-logic groups they belong to;
admin-type accounts see everything. Auditable, and reversible where
reversal makes sense (compensating actions, never deletion).

This is a deliberate slow-burn feature: it lives on branch
`feat/audit-log` (created from `dev` at the first C-card) until Allan
merges it. Cards C1–C3 build on the redaction feature (its events must
be auditable), so the chain order is A → B → C.

## Ground truth (verified 2026-11-16, dev branch)

- Auth: Keycloak JWT via `syzygy/auth.py`; identity dict
  `{sub, email, preferred_username}` from `get_current_user`;
  `allowed_users` table gates login (see `scripts/manage_allowed_users.py`);
  dev token maps to `dev-admin`.
- Pairing write points (`syzygy/pairing_service.py`): `trigger_pairing`
  (manual/override/automatic + pinned normal), `trigger_pairing_for_normal`,
  `trigger_pairing_for_unpaired_tumors`/`_run_bulk_pair_pass`,
  `trigger_dmp_pairing`, `trigger_harmony_pairing`; `_mark_stale`
  retires rows. Routers in `routers/pairings.py` call these.
- Ingest write point: `syzygy/nats_ingest.py` (listener POSTs to
  `/v1/ingest/nats`); cohort outbox: `routers/publish_outbox.py` +
  listener flusher.
- Redaction (from REDACTION_TICKETS): redact/unredact endpoints +
  `redacted_by/at/reason` columns.
- Alembic exists (ALEMBIC_TICKETS): new schema = hand-written revision.
- Sandbox: no network; repo venv; factory bot commits with
  `Kanban-Card:` trailer.

## Design decisions (DECIDED — Allan may amend in chat; fold changes into
## card bodies before unblocking, do not invent alternatives in code)

1. **Table** `audit_log` (append-only):
   `id UUID PK`, `occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
   `actor TEXT NOT NULL` (email, or `system`), `actor_kind TEXT NOT NULL`
   (`user|system`), `action TEXT NOT NULL` (dotted namespace, see list),
   `entity_type TEXT NOT NULL`, `entity_id TEXT NOT NULL`,
   `pairing_logic TEXT NULL` (visibility scope key; NULL = admin-only
   unless it is the actor's own action), `payload JSONB NOT NULL`
   (before/after snapshot + context), `reverse_of UUID NULL` (this event
   reverses that event), `request_id TEXT NULL`.
   Indexes: `(occurred_at)`, `(pairing_logic, occurred_at)`,
   `(entity_type, entity_id)`, `(actor, occurred_at)`.
2. **Append-only enforcement at the DB level**: `REVOKE UPDATE, DELETE ON
   audit_log FROM <app role>` + a `BEFORE UPDATE OR DELETE` guard trigger
   raising an exception. Application code gets a single writer helper
   `audit.record(...)` — no ORM update paths exist.
3. **Action namespace** (initial): `pairing.created` (payload carries
   trigger_type manual|override|automatic, tumor/normal sample names,
   mismatched_keys, previous pairing id when replaced), `pairing.retired`
   (from `_mark_stale`), `sample.created`, `sample.updated`,
   `sample.redacted`, `sample.unredacted`, `nats.ingest` (subject +
   sample/request ids, NOT full payload — no patient data in the log),
   `cohort.published`, `logic.created`, `logic.updated`,
   `machine.created`, `bait_set.assigned`, `pair_assignment.imported`
   (bulk file import, one event per file + per pair), `audit.reversed`.
   **Sanitization rule: audit payloads carry ids and names only — never
   variant calls, MRNs, or free-text clinical fields.**
4. **Actor attribution**: routers pass the authenticated identity down;
   service-layer automatic paths (bulk sweeps, DMP trigger, ingest,
   outbox flush) record `actor='system', actor_kind='system'`. Bulk
   passes write one summary event per pass PLUS one `pairing.created`
   per row created (bounded: cap per-pass detail at 500 rows, summarize
   beyond).
5. **Visibility model**: `allowed_users` gains `role TEXT NOT NULL
   DEFAULT 'user'` (`user|admin`) and `logic_groups JSONB NOT NULL
   DEFAULT '[]'` (list of pairing-logic names). Rules for
   `GET /v1/audit/`: admin → everything; user → events where
   `pairing_logic ∈ their groups` OR `actor == their email`. Users with
   no groups see their own actions only. Managed via
   `scripts/manage_allowed_users.py` subcommands (`set-role`,
   `set-groups`) — no UI admin panel in v1.
6. **Reversal**: `POST /v1/audit/{id}/reverse` implements compensating
   actions for reversible actions only: `pairing.created` (override) →
   restore the previously-latest pairing it replaced (re-trigger with the
   old normal), `sample.redacted` → unredact, `pair_assignment.imported`
   → per-pair reversal. The reversal executes the normal code path (so
   it is itself audited) and sets `reverse_of` on the new event.
   Irreversible actions (ingest, published cohort) → 400 with reason.
   Reversal requires the reverser to see the event (visibility rules) and
   be admin for cross-user reversals.
7. **Query API**: `GET /v1/audit/` filters `from,to,actor,action,
   entity_type,entity_id,pairing_logic,limit(≤500),offset`, newest
   first, visibility applied server-side; `GET /v1/audit/entity/{type}/{id}`
   convenience timeline. Response includes `reversible: bool` per row.
8. **Operations UI ("Ops" tab)**: visible only when the logged-in
   identity has role admin or ≥1 logic group (server-provided
   `/v1/audit/me/` returns role+groups). Sections:
   a. **Pair assignment** — paste/upload the same `NORMAL_ID<TAB>TUMOR_ID`
      TSV; server-side endpoint `POST /v1/pairing/import` (logic select,
      dry-run flag, strict flag) reusing the exact resolution + trigger
      semantics of `scripts/import_tempo_pairs.py` (port its logic into
      a shared module so script and API cannot drift); result table =
      the script's per-row statuses; pairing log downloadable.
   b. **Audit log viewer** — filter bar (time range, actor, action,
      logic), rows with payload expanders, Reverse button where
      reversible, CSV export (respecting visibility).
   c. **Redaction controls** — search samples, redact/unredact (reuses
      R-feature endpoints).
9. **TEMPO CLI script stays**: `scripts/import_tempo_pairs.py` refactored
   to call the shared import module (single source of truth), CLI UX
   unchanged, its tests still green.

## Ticket AL1 — audit spine + write hooks (codingbot, branch `feat/audit-log`)

Build: Alembic revision (audit_log table + revokes + guard trigger);
`syzygy/audit.py` writer helper (`audit.record(db, actor, action, ...)`,
never raises — audit failure logs and continues, pairing must not break
because audit broke); hooks at all write points listed above (pairing
service incl. bulk passes, sample create/update/redact/unredact, nats
ingest, cohort publish, logic/machine/bait-set mutations); tests
`tests/test_audit_log.py`: append-only enforcement (UPDATE/DELETE raise),
event emitted for each action class with correct actor attribution
(user vs system), sanitization (payload has no forbidden keys), bulk
summary+detail shape.
Accept: every mutation path produces exactly one audit event (test per
path); guard trigger proven; suite green.

## Ticket AL2 — visibility, query & reversal API (codingbot, branch `feat/audit-log`)

Build: allowed_users role/logic_groups columns (revision) + CLI
subcommands; `routers/audit.py` (`GET /`, `GET /entity/{type}/{id}`,
`GET /me/`, `POST /{id}/reverse`) with the visibility rules; reversal
implementations per decision 6; tests: visibility matrix (admin/user/
group-member/own-actions), reversal happy + 400 paths, filters +
pagination.
Accept: matrix tests prove a group user cannot read another group's
events; reversal of an override restores the old pairing AND writes the
linked audit event; suite green.

## Ticket AL3 — Operations UI (codingbot, branch `feat/audit-log`)

Build: Ops tab per decision 8 (a/b/c), `POST /v1/pairing/import`
endpoint + shared import module refactor of the CLI script (decision 9);
UI hidden for users without role/groups; tests for the import endpoint
(dry-run, mismatch rows, log download) + existing CLI tests green.
Accept: TSV import through the UI reproduces the CLI's exact statuses on
the same fixture (receipt in commit message); log viewer filters work;
Ops tab visibility proven by test or documented manual check; suite green.

## Final reviewer card (reviewer profile, parent = last gate)

Judge against each ticket's Accept list + this spec; verdict metadata
APPROVE/ISSUES; on APPROVE do NOT complete — post handoff comment (branch
ready, merge checklist: review `feat/audit-log` diff vs dev, run full
suite, then Allan merges) and `kanban_block` with reason prefixed
`review-required:` (human tail — Allan's merge is the go).

## Gate (every code card)

Clean `git status --porcelain` + `.venv/bin/python -m pytest -q` exit
code in `/home/allan/workspace/syzygy-dev` (branch as card states).
Env problems → block with `env:` prefix; never edit code or tests.
