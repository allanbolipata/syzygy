# Dev NATS ingestion fixtures

Self-consistent, synthetic payloads for exercising the full local ingest
chain (NATS listener → `POST /v1/ingest/nats`) with one command:

```bash
# 0. one-time: seed the five pairing logics (card 1/4) so ingest triggers
#    have a logic to pair under — otherwise samples are created but every
#    sample is reported in the response's `uncovered` list
python scripts/seed_dev_data.py

# 1. publish the whole fixture directory in ingest order
python scripts/mock_smile.py pub-dir tests/fixtures/nats/dev
```

Conventions: request ids `991xx` (the DB seeder owns `990xx`), synthetic
patients `C-DEVxx`, `example.org` emails only, 2 samples per request.

`pub-dir` order (deterministic, ingest-safe): new-requests (alphabetical)
→ sample-update → request-update → dmp-sample → cohort-validate, 1 s
between publishes (`--delay`, `--dry-run` prints the plan without
connecting).

## Fixtures

| Fixture | Subject | Expected ingest result |
|---|---|---|
| `dev_argos_new_request.json` | `MDB_STREAM.server.cpt-gateway.cmo-new-request` | Request **99101** (ARGOS logic, IMPACT505 / IMPACT505_BAITS) + samples **99101_1** (Tumor) + **99101_2** (Normal). Same run mode (NovaSeq 6000) + preservation + bait set → **1 automatic pairing** 99101_1 ↔ 99101_2. |
| `dev_access_new_request.json` | `MDB_STREAM.server.cpt-gateway.cmo-new-request` | Request **99102** (ACCESS logic, MSK-ACCESS-v2_0-probesAllwFP) + **99102_1** (Tumor) + **99102_2** (Normal). Tumor + normal of one request → pairing via the **request-first tier** (1 row). |
| `dev_tempo_new_request.json` | `MDB_STREAM.server.cpt-gateway.cmo-new-request` | Request **99103** (TEMPO logic, IDT_Exome_v2_FP_BAITS) + **99103_1** (Tumor, run *NovaSeq X 1000*) + **99103_2** (Normal, run *NovaSeq 6000*). Different run modes skip the run-mode tier → pairing via the **tempo coarse (exome) bucket** (1 row). |
| `dev_sample_update.json` | `MDB_STREAM.server.cpt-gateway.cmo-sample-update` | Flips **99102_2** from Normal to **Tumor** (request 99102). `tumor_or_normal` is pairing-relevant so the trigger fires, but 99102 now has no Normal left → **no new pairing row** (known no-op); the existing 99102_1 ↔ 99102_2 row stays `latest`. |
| `dev_request_update.json` | `MDB_STREAM.server.cpt-gateway.cmo-request-update` | JSON array re-importing request **99101** metadata (validationStatus `Validated`). Sample set and roles unchanged → **no pairing change** (known no-op; metadata refresh only). |
| `dev_dmp_sample.json` | `MDB_STREAM.server.dmp-gateway.dmp-sample` | **ACELA envelope** `{path, metadata, pathChecks}` (flat DMP record under `metadata`): fake bam path `/dmp/mirror/impact468/I/T/ISFAKE-P-00DEV09-T01-IM3.bam`, all-OK `pathChecks` → flattens at ingest and creates DMP Tumor **P-00DEV09-T01-IM3** (pool `poolDEV9`, patient C-DEV09 / P-00DEV09, `sample_type` "1" metastasis flag) storing `bam_path` = the envelope `path`, `path_checks` = the `pathChecks` verbatim, and `bam_exists` **1** (all checks OK). `part_c_consent: 0` rides verbatim in `additional_metadata` — it never drives consent. **No pairing at ingest** — DMP pairing is reference-alignment-driven, not ingest-triggered. |
| `dev_dmp_sample_normal.json` | `MDB_STREAM.server.dmp-gateway.dmp-sample` | **ACELA envelope**: fake bam path `/dmp/mirror/impact468/I/N/ISFAKE-P-00DEV09-N01-IM3.bam`, `dms` check `MISSING` → creates the matching DMP Normal **P-00DEV09-N01-IM3** (same patient + pool) storing `bam_path` + `path_checks` verbatim and `bam_exists` **0** (non-OK check). `part_c_consent: 1` (varies from the Tumor's `0`; metadata pass-through coverage only — never drives consent). **No pairing at ingest**; running the DMP pairing trigger (`trigger_dmp_pairing`, needs the active DMP logic from the seeder) pairs the two via the same-patient + same-pool tier. |
| `dev_cohort_validate.json` | `MDB_STREAM.syzygy.inbound.tempo-cohort-validate` | Valid against `syzygy/data/cohort-request.schema.json` (draft-07). The listener validates it and replies on `tempo-cohort-result`; **no samples, no pairings** (validation only). References the 99101 + 99103 pairs; deliberately omits 99102 (its Normal was flipped to Tumor above). |

## Notes

- Publish order matters: the sample/request updates reference 99101/99102,
  and the cohort payload references registered samples — `pub-dir`
  enforces the dependency order automatically.
- All payloads are idempotent-ish: re-publishing re-imports the same
  request ids and upserts the same sample rows (pairing rows are not
  duplicated for already-`latest`-paired tumors).
- To watch the cohort round-trip: `python scripts/mock_smile.py sub
  cohort-result --count 1` in a second terminal, then `pub-dir`.
