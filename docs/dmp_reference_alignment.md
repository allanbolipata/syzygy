# DMP Reference Alignment — Canonical Spec

**Status:** 1/8 of the DMP Reference Alignment series (spec + key-field audit).
Tickets 2–7 implement against this document. **Where this doc and the reference
scripts disagree, the scripts win.** Stakeholder rule: when in doubt, follow
`bam-mirror-tools`.

Reference implementation (authoritative): the internal `bam-mirror-tools` repo (not published with this project)

- `scripts/generate_dmp_tumor_normal_key.pl` — raw DMP key file → 12-column TSV
- `scripts/pair_tumor_with_normal.R` — two-tier pairing → 15-column pair file, summary counts, 5 QC warnings
- `README.md`, `make_dmp_tn_key.sh`, `test/` — context

No behavior change in this ticket: this document plus the field audit below.

---

## a. Reference pipeline

Raw DMP-program key file → Perl → 12-column TSV → R script.

### Raw key file (DMP program output)

- Rows; the Perl script's usage header calls the key file tab-delimited, and
  the script additionally runs `s/,/\t/g` before parsing, so a comma-delimited
  form parses as well. Rows whose first field does not match `^P-\d+` are
  skipped (e.g. a header line).
- Columns, positional (trailing fields anchored from the END of the row —
  0-based indices; the row needs at least 8 columns so the anchored fields
  don't collide with `group` at [2]):

| position | field | notes |
|---|---|---|
| [0] | `sample` | e.g. `P-0000001-T01-IM6` |
| [1] | `bamname` | extensionless basename of the BAM file; parsed `(\w+)-(\w+)` into `bprefix`/`bsuffix`; `bsuffix` ∈ {"T","N"} drives tumor/normal split |
| [2] | `group` | DMP-program value, carried through untouched |
| [3 .. n-6] | (program-specific) | zero or more middle columns, unused by the scripts |
| [n-5] | `pool_id` | fifth from the end |
| [n-4] | `coverage` | fourth from the end, string |
| [n-3] | `matched_tag` | **third from the end** — the DMP program's match flag (e.g. `Matched`) |
| [n-2], [n-1] | (program-specific) | two trailing columns AFTER `matched_tag`, not referenced anywhere by the Perl or R scripts |

Note: this ticket's paraphrase listed `matched_tag` as the final column; the
script reads it at `scalar(@sp)-3` (lines 94–96), so it is third from the end
with two unused columns after it. The script wins.

### Perl stage (`generate_dmp_tumor_normal_key.pl`)

Per row:

- `patient`, `idx`, `ver` parsed from `sample` via `(P-\d+)-(\w+)-(\w+)`:
  patient = `P-…` id, idx = middle word (e.g. `T01`), ver = last word
  (e.g. `IM6`). **ver is the IM_ver used by the R rescue tier.**
- `bprefix`, `bsuffix` parsed from `bamname` via `(\w+)-(\w+)`.
- `bam_exists`: `1` iff `bamname` (the raw field, as-is) is among the
  extensionless basenames of files found under the BAM mirror root by
  `find <mirror> -name '*bam'` (basename, extension stripped via
  `cut -f 1 -d'.'`); else `0`.
- Output: one row per input row (no T/N split yet), written to
  `<output_dir>/<date>/dmp_key_tabbed_<date>.txt` as a **12-column TSV**
  with this exact header and column order (verified against line 83 of the
  script):

```
sample, patient, idx, ver, bamname, bprefix, bsuffix, group, bam_exists, coverage, pool_id, is_matched
```

(`is_matched` = the raw file's `matched_tag`.) `<date>` = `YYYY_MM_DD`,
derived from the key file name (`key_YYYY_MM_DD.txt` → that date; a plain
`key.txt` → the run date).

Hand-off: the Perl script then invokes
`Rscript --vanilla <scripts>/pair_tumor_with_normal.R <tabbed_key>
<output_dir>/<date>/dmp_key_paired_<date>.txt
<output_dir>/<date>/dmp_key_paired_<date>.summary.txt <output_dir>/<date>>`
and symlinks the pair file and summary as `<output_dir>/dmp_key_paired_latest.txt`
and `<output_dir>/dmp_key_paired_latest.summary.txt`.

### R stage input

The R script reads the 12-column TSV with
`fread(tabbed_key_file, colClasses = rep('character', 12))` (header on) —
every column arrives as a string.

---

## b. Two-tier pairing rule (exactly as `pair_tumor_with_normal.R` implements it)

1. **Split.** `tumors` = rows with `bsuffix == "T"`; `normals` = rows with
   `bsuffix == "N"`.
2. **Tier 1 — pool match.** Tumors are `left_join`ed to normals on
   **`(patient, pool_id)`**.
   - Multiple matching normals → the tumor keeps **all** of them as separate
     rows (no winner selection; ambiguous matches are kept).
   - No matching normal → a single row with all `normal_*` columns `NA`.
   - The script then runs `filter(!is.na(bsuffix.x))` — a no-op, because
     `bsuffix.x` is the tumor-side column and is always `"T"`. Effective
     behavior: **every tumor row is retained; the pair file contains tumor
     rows only.**
3. **Tier 2 — IM_ver rescue.** Rows whose `normal_bamname` is `NA` (i.e. tier
   1 found nothing) are `left_join`ed to normals (renamed with a
   `rescued_*` prefix) on **`(patient, IM_ver)`** — specifically
   `tumor_IM_ver` (the tumor's `ver` column, the last dash-segment of the
   tumor sample name) matched against the normal's `ver`.
   - `unique()` is applied to the rescue join result — fully duplicate rows
     (identical rescue columns) are collapsed.
   - Rescued values are joined back to the pair frame by `tumor_sample` and
     applied with `ifelse` **only where the tier-1 normal column is `NA`**:
     `normal_sample`, `normal_bamname`, `normal_bam_exists`,
     `normal_bam_coverage`. (The rescue join-back does **not** select
     `rescued_normal_IM_ver`; `normal_IM_ver` is handled in step 4.)
4. **Backfill.** `normal_IM_ver` =
   `ifelse(is.na(normal_IM_ver), tumor_IM_ver, normal_IM_ver)` — when no
   tier-1 normal exists, `normal_IM_ver` is backfilled with **the tumor's own
   IM_ver** (identical in value to the rescued normal's `ver`, since that is
   the join key).
5. **Consequences (all verified in the script):**
   - A tumor with no normal at all (both tiers empty) still appears as a
     row: `normal_sample`/`normal_bamname`/`normal_bam_exists`/
     `normal_bam_coverage` are `NA`, but `normal_IM_ver` = tumor IM_ver.
   - A normal may appear on multiple rows (shared across tumors, or repeated
     under one ambiguous tumor).
   - Row count per tumor = number of tier-1 normals, else number of distinct
     tier-2 rescue normals, else exactly 1.

---

## c. 15-column pair file

Written TSV with header, `sep="\t"`, `quote=FALSE`, `row.names=FALSE`
(`NA` values written as the literal `NA`). Exact column names and order
(verified against the R `select` block):

| # | column | source in the R script |
|---|---|---|
| 1 | `tumor_sample` | `sample.x` (tumor's `sample`) |
| 2 | `patient` | `patient` |
| 3 | `tumor_idx` | `idx.x` |
| 4 | `tumor_IM_ver` | `ver.x` |
| 5 | `tumor_bamname` | `bamname.x` |
| 6 | `tumor_group` | `group.x` |
| 7 | `tumor_matched` | `is_matched.x` — **the raw key file's `matched_tag` (a DMP-program value, NOT derived from pairing)** |
| 8 | `tumor_bam_exists` | `bam_exists.x` (string `"0"`/`"1"`) |
| 9 | `tumor_bam_coverage` | `coverage.x`, cast `as.numeric` |
| 10 | `normal_sample` | `sample.y`; tier-2 rescue value where tier 1 was `NA` |
| 11 | `normal_bamname` | `bamname.y`; rescue value where tier 1 was `NA` |
| 12 | `normal_IM_ver` | `ver.y`; backfilled with `tumor_IM_ver` where tier 1 was `NA` |
| 13 | `pool_id` | `pool_id` |
| 14 | `normal_bam_exists` | `bam_exists.y` (string `"0"`/`"1"`); rescue value where tier 1 was `NA` |
| 15 | `normal_bam_coverage` | `coverage.y`; rescue value where tier 1 was `NA` |

Notes:

- Tumor rows only (see b.3 no-op filter).
- `tumor_bam_coverage` is numeric after the `as.numeric` cast.
- `normal_bam_coverage` is **character** after the rescue step: the
  `ifelse` mixes the character-typed `rescued_normal_bam_coverage` into the
  numeric column, and R promotes the whole column to character. This affects
  warning 5 below.

---

## d. Summary counts

The Perl caller passes `<output_dir>/<date>/dmp_key_paired_<date>.summary.txt`
as the R script's third argument (`warnings_file`); the R script opens it with
`open="w"` (truncate, not append) and writes a `#########`-framed header plus
three count lines. All are computed from the final 15-column frame, with
`matched_tumor_normals` = the sub-frame of rows where
`tumor_matched == "Matched"`:

- `# of patients in the key file:` = **distinct `patient` values among
  matched rows** (the log label says "key file", but the computation is over
  matched rows — formula as implemented).
- `# of Tumor-Normal pairs:` = **row count of matched rows**
  (`dim(matched_tumor_normals)[1]`).
- `# of unmatched tumors:` = **row count of rows with
  `tumor_matched != "Matched"`** (computed over all rows).

**5/8 implementation note (response contract).** `GET /pairing/report/`
for DMP logics adds `summary: {patients, tn_pairs, unmatched_tumors}`
(ints, keys renamed from the log labels) and `warnings` (section e),
computed over the **full** 15-column pair frame — latest paired rows
then unmatched DMP tumor rows, the same cells the DMP export renders —
so they are **independent of the requested page and of the `q` search**
(`q` filters rows only). Non-DMP logics leave both fields `null`
(existing contract unchanged).

---

## e. The 5 QC warnings

Each is a TSV file (header, `quote=FALSE`) written to the same dated output
directory as the pair file, computed over the final 15-column frame. **A file
is only written when its row set is non-empty** (the R script guards each
write with `dim(...) > 0`); an empty set produces no file. Exact conditions
as implemented (R `dplyr::filter`; rows where the condition evaluates `NA`
are dropped):

| # | file | row set | exact condition |
|---|---|---|---|
| 1 | `matched_tumors_without_normal.txt` | all rows | `tumor_matched == "Matched" & is.na(normal_bamname)` (still no normal after the tier-2 rescue) |
| 2 | `tumor_samples_with_multiple_ambiguous_normals.txt` | count: matched rows; **file: all rows** (matched filter NOT applied when writing) | count = number of distinct `tumor_bamname` values among rows with `tumor_matched == "Matched"` whose group-by-`tumor_bamname` row count is > 1; file = all rows with `tumor_bamname` in that set |
| 3 | `samples_with_bams_missing.txt` | all rows | `tumor_bam_exists == 0 \| normal_bam_exists == 0` (columns are the strings `"0"`/`"1"`; R coerces `0` to `"0"`) |
| 4 | `low_coverage_tumors.txt` | all rows | `(!is.na(tumor_bamname) & is.na(tumor_bam_coverage)) \| (tumor_bam_coverage < 100)` — numeric coverage; threshold **tumor < 100**; the first disjunct covers a BAM whose coverage is `NA` (missing / empty / non-numeric) |
| 5 | `low_coverage_normals.txt` | all rows | `(!is.na(normal_bamname) & is.na(normal_bam_coverage)) \| (normal_bam_coverage < 50)` — threshold **normal < 50**; see quirk below |

**Reference quirk in warning 5 (must be mirrored by the 7/8 conformance
harness):** because the rescue step (b.3/c) promotes `normal_bam_coverage` to
character, `< 50` is a **lexicographic string comparison** in R (the numeric
`50` is coerced to `"50"`). Consequences: `"45" < "50"` → TRUE, `"588" <
"50"` → FALSE, but `"100" < "50"` → TRUE and `"9" < "50"` → FALSE — i.e.
values with a different first digit than `5` can compare wrong. The
`NA`/`is.na` half of the predicate behaves normally. Warning 4 (tumor side)
is a genuine numeric comparison; only the normal side is affected. If the
stakeholder wants numeric semantics, that is a change to the reference, not
to this spec.

Additional notes:

- A row with no normal at all (all `normal_*` `NA`) is dropped from warning
  5 by the `NA`-filtering behavior (condition evaluates `NA`), so
  "missing normal" rows are not flagged as low-coverage normals.
- "Where a BAM exists" for the low-coverage warnings = the corresponding
  `bamname` column is non-`NA` (the first disjunct), not `bam_exists == 1`.
- The log line for warning 2 reads `# of patients with multiple normals for a
  given tumor:` but the counted unit is distinct `tumor_bamname` values
  (label as implemented; formula as in the table).

**5/8 implementation notes (response contract).**
- `GET /pairing/report/` for DMP logics adds
  `warnings: {matched_tumors_without_normal,
  tumor_samples_with_multiple_ambiguous_normals, samples_with_bams_missing,
  low_coverage_tumors, low_coverage_normals}` — one list per bucket of the
  affected samples' `primary_id`s (the sample names the reference files
  would list: the tumor side, plus the normal side where the reference
  writes both columns).
- **Empty buckets are present as empty lists** — the reference writes a
  file only when non-empty, but the response shape is fixed (the 6/8
  frontend renders all five buckets unconditionally).
- Bucket lists are in **pair-frame order** (latest paired rows
  created-desc, then unmatched rows created-desc), with each affected
  sample listed **once** (first occurrence). The reference files are the
  flagged ROWS written verbatim (no `unique()`), so a sample appearing on
  several flagged rows is repeated there; the response shape is a sample
  list, so it dedupes.
- Warning 2 in syzygy: pairing rows are unique per (tumor record,
  logic), so "a tumor_bamname on >1 row" arises from **multiple tumor
  records sharing the same `tumor_bamname`** (e.g. duplicated DMP tumor
  records), not from one record fanned out. The formula (count over
  matched rows, list over all rows) is unchanged.

---

## f. Syzygy ↔ reference mapping

**Data flow (stakeholder correction, 2026-11-04):** the DMP data arrives from a
service via NATS as a JSON message **in the exact format we already store in
the syzygy DB** (the DMP record format — `sample, anon_id, patient, coverage,
project_name, …`). We will NOT receive key-file-shaped data; the key file is
replaced by this message. The reference-column remap below (e.g. `pool_id` ←
`project_name`) is done **within syzygy at pairing/report time**, not at
ingest. Ingest stores the record as-is.

| reference field | syzygy source | mapping rule |
|---|---|---|
| `sample` | `Sample.primary_id` (`sample__dmp` column) | 1:1. The import promotes `metadata.sample` to the sample row and excludes it from `additional_metadata` (`EXCLUDED_DMP_KEYS`). |
| `patient` | `Sample.patient__dmp` (from `metadata.patient.dmp`) | Syzygy stores it as its own field and treats it as **authoritative** (it is also the DMP pairing key). The reference parses the same value out of the sample name; 2/8's fixture audit found the two can diverge in dev data — see section g, caveat 1. |
| `idx` / `ver` (IM_ver) | not stored today | Reference derives them in Perl from the sample name (`(P-\d+)-(\w+)-(\w+)`). Derived at pairing time from the sample name (in-syzygy remap, not ingest). 3/8's rescue tier needs `ver` = last dash-segment of the sample name. |
| `bamname` | `Sample.anon_id` (from `metadata.anon_id`) | **Stand-in**: no `bamname` key exists in beagle DMP records. An explicit `bamname` in a record, if ever present, takes precedence over `anon_id` (the known format does not carry one). (The beagle file record's `.bam` `file_name` is not imported today and is not the source of truth for this field.) |
| `group` | not present in DMP records | No beagle equivalent; absent from the record format (verified in fixture). 2/8 documents it as a gap with the fallback for the `tumor_group` column. |
| `pool_id` | `additional_metadata.project_name` | **Stakeholder-confirmed rename of `project_name`** — remapped within syzygy: `pool_id` = `additional_metadata.project_name`; an explicit `pool_id` in a record, if ever present, takes precedence (the known format does not carry one). Beagle records contain no `pool_id` key (verified in the fixture). |
| `coverage` | `additional_metadata.coverage` (numeric in the fixture) | Stored as-is from `metadata.coverage` (not in `EXCLUDED_DMP_KEYS`). Reference treats it as a string end-to-end until the R `as.numeric` cast; syzygy stores JSON numeric. |
| `matched_tag` → `is_matched` → `tumor_matched` | not present in DMP records | **A DMP-program value, not derivable from pairing.** Absent from the record format — a gap; 2/8 documents the fallback and flags it for 5/8 before the summary counts that consume it are finalized. |
| `bam_exists` | not present; not computed today | **The one sanctioned deviation.** Reference: Perl checks the BAM path exists in the mirror (`bamname` ∈ mirror listing). Syzygy equivalent: the sample's beagle file record (`file_system.filemetadata` → `file_system.file`) has a **non-empty `path`** → `1`, else `0`. The import currently selects only `fm.metadata` and never joins the file record; the file record is already in the DB from the beagle dump, so this is a read-time derivation computed at pairing/report time (2/8 documents it). |

### Current state (for context — changed by tickets 3/8–5/8)

Syzygy's existing `dmp` pairing strategy (`pairing_strategies.py`,
`DmpPairingStrategy`) pairs DMP tumors → DMP normals strictly inside the DMP
group on **`(patient__dmp, gene_panel)`** (case-insensitive) and picks a
**single deterministic winner** (first in `primary_id` string order). It has
no `pool_id` tier, no IM_ver rescue, no ambiguous-match fan-out, and
produces no summary/QC output. The reference behavior specified in b–e above
is what tickets 3/8–5/8 implement.

---

## g. Reference-column remap (in-Syzygy)

**When and where.** Ingest (NATS `dmp-gateway.dmp-sample` subject and the
beagle dump import) stores the record as-is via
`dmp_translator.to_dmp_sample_kwargs` — mapped keys become Sample columns,
everything else lands in `additional_metadata`. The remap onto the
reference 12-column key-file schema (section a) is applied **at pairing/
report time**, never at ingest, and **no code path parses or accepts a
key-file-shaped payload** (there is none — see the section f data-flow
note). One implementation is shared by the two-tier strategy (ticket 3/8)
and the Pair Report (ticket 4/8):

```python
dmp_translator.remap_dmp_record(sample, bam_exists: int = 0) -> dict
```

It returns the 12 reference columns (in section a order, `DMP_REF_COLUMNS`)
for one stored DMP `Sample` row.

### Column contract

| # | reference column | syzygy source | rule |
|---|---|---|---|
| 1 | `sample` | `Sample.primary_id` | 1:1. |
| 2 | `patient` | `Sample.patient__dmp` | Stored column wins (it is also the DMP pairing key); if NULL, parse `(P-\d+)-(\w+)-(\w+)` from `primary_id` and use the `P-…` group (the reference's own derivation). See caveat 1. |
| 3 | `idx` | parsed from `primary_id` | Group 2 of `(P-\d+)-(\w+)-(\w+)` (e.g. `T01`); `""` when the name does not parse. Not stored as a column — derived here, at pairing/report time. |
| 4 | `ver` | parsed from `primary_id` | Group 3 — the **IM_ver** used by the R rescue tier; `""` when unparseable. 3/8's rescue tier joins on this value. |
| 5 | `bamname` | `additional_metadata.bamname`, else `Sample.anon_id` | Explicit `bamname` wins if ever present (absent in the known format); `anon_id` is the documented stand-in (f). |
| 6 | `bprefix` | parsed from `bamname` | Group 1 of `(\w+)-(\w+)`; `""` when no match. |
| 7 | `bsuffix` | parsed from `bamname` | Group 2 of `(\w+)-(\w+)`; `""` when no match. **Not a tumor/normal signal in syzygy** — see caveat 2. |
| 8 | `group` | `additional_metadata.group` if ever present | **Gap** (audited absent — 0/8 fixture records, and no such key in `.beagle_ground_truth.example.json` DMP rows). An explicit value passes through untouched (as the reference carries it); otherwise `None` and the report renders `NA` in the `tumor_group` column. |
| 9 | `bam_exists` | caller-resolved; helper parameter `bam_exists` (default `0`) | **The one sanctioned deviation** (f/audit): the reference checks the BAM path against the mirror listing. Syzygy has no file table, so the value is resolved from the beagle file record (non-empty `path` → `1`, else `0`) by the consumer that can reach it, and passed in; unresolvable → `0`. |
| 10 | `coverage` | `additional_metadata.coverage` | As-is (JSON numeric in the known format); `None` means NA and must not be coerced to `0` — the reference's warning 4 treats missing coverage as a distinct case. |
| 11 | `pool_id` | `additional_metadata.pool_id`, else `additional_metadata.project_name` | Explicit `pool_id` wins if ever present (absent in the known format); `project_name` is the stakeholder-confirmed rename. |
| 12 | `is_matched` | `additional_metadata.matched_tag` if ever present | **Gap** (audited absent — a DMP-program flag, not lab metadata). An explicit `matched_tag` passes through (the reference's raw value); otherwise `None`, and 3/8 derives match status from the pairing result — a tumor is *matched* iff the strategy output gives it at least one normal after both tiers. 4/8 renders that status. |

### Caveats (2/8 fixture audit, beagle repo `file_system/fixtures/DMP_data.json`)

1. **`patient` divergence.** For 3 of the 4 distinct DMP samples in the
   fixture, the stored `patient__dmp` differs from the sample-name prefix
   (e.g. sample `P-0000002-T01-IM6` carries `patient__dmp = P-0000001`;
   the 4 samples map to only 2 patients). Syzygy treats the stored
   `patient__dmp` as authoritative so the report's patient column and the
   strategy's pairing key agree; the name parse is a NULL-column fallback
   only. **Flagged for stakeholder review**: confirm whether production
   records preserve name-prefix == `patient.dmp` (it affects 5/8's
   patient-level summary counts).
2. **`bsuffix` is not a T/N signal here.** Real `anon_id` values are
   8-letter tokens with no dash (all 8 fixture records), so
   `(\w+)-(\w+)` never matches and `bprefix`/`bsuffix` are `""` for
   known-format records. The reference splits tumors/normals on
   `bsuffix` ∈ {"T","N"}; syzygy must split on `tumor_or_normal`
   (derived from `type` at ingest) and must not use `bsuffix`.
3. **`group` and `matched_tag` are gaps** — audited absent from the record
   format. The helper never invents values for them (pass-through of an
   explicit field, else `None`).

### Pairing context for 3/8

The reference (b) joins tier 1 on `(patient, pool_id)` and the tier-2
rescue on `(patient, IM_ver)`, retaining every tumor row. Under this
contract the syzygy analogues are: `patient` = `patient__dmp`,
`pool_id` = `project_name`, `IM_ver` = the last dash-segment of
`primary_id` (helper column `ver`). 3/8 owns the two-tier shape and the
tumor/normal split (`tumor_or_normal`); this section fixes only the column
names and derivations that the strategy and report consume.

### 5/8 flag (summary counts) — resolved by 5/8

The reference summary counts (d) consume `tumor_matched == "Matched"` —
the DMP-program `matched_tag`, which is **absent from real syzygy records**.
5/8 consumes the final `tumor_matched` cell of the pair frame as 4/8
defines it — an explicit record `matched_tag` when present, else the
**pairing-derived** status (matched = the row has a normal; unmatched-tumor
rows = no normal). In practice, since no real record carries a
`matched_tag` today, the counts are computed from the pairing-derived
status; an explicit tag, when the DMP program ever supplies one, takes
precedence (the reference consumes `tumor_matched` as-is, so both halves
are reference-faithful). The stakeholder-review flag on real
`matched_tag` values remains open for 7/8 conformance.

---

## Field audit

Fixture examined: beagle repo `file_system/fixtures/DMP_data.json`
(Django fixture; 8 `file_system.filemetadata` records, each `fields.metadata`
with keys: `type, assay, sample, anon_id, patient{cmo,dmp}, updated,
version, coverage, imported, cmo_assay, tumor_type, external_id,
sample_type, tissue_type, primary_site, project_name, part_c_consent,
metastasis_site, somatic_calling_status, major_allele_contamination,
minor_allele_contamination` — all 8/8 records carry every key). Linked
`file_system.file` records carry `path` (non-empty for all 4 `.bam` files in
the fixture) and `file_name`. Sources checked: `dmp_translator.py`,
`scripts/import_beagle_dump.py`, `scripts/.beagle_ground_truth.example.json`
(DMP sample rows: `primary_id`, `anon_id`, `patient__cmo`, `patient__dmp`,
`tumor_or_normal`, `gene_panel`, `tumor_site`, `sample_type`,
`tumor_or_normal_type`).

| reference field | present in syzygy DMP `additional_metadata` today? | current source | gap |
|---|---|---|---|
| `sample` | No — promoted out of `additional_metadata` by `EXCLUDED_DMP_KEYS` | `metadata.sample` → `Sample.primary_id` (`sample__dmp` column) | None for identity. Reference additionally derives `patient`/`idx`/`IM_ver` from the name; syzygy stores `patient__dmp` but not `idx`/`IM_ver` (see `patient`/`idx` rows). |
| `bamname` | No — no such key in beagle DMP metadata | Stand-in: `metadata.anon_id` → `Sample.anon_id` column (all 8 fixture records carry `anon_id`) | No `bamname` key anywhere in the record; an explicit `bamname`, if ever present, takes precedence over the `anon_id` stand-in (absent in the known format). The `.bam` file record `file_name` is not imported. |
| `group` | No — no such key in beagle DMP metadata | None | No source today; gap documented by 2/8 (with fallback for the `tumor_group` column). |
| `pool_id` | No — no `pool_id` key (verified: absent from all 8 fixture records) | Fallback: `metadata.project_name` → `additional_metadata.project_name` (present in all 8 fixture records) | Stakeholder-confirmed rename `pool_id` = `project_name`, remapped within syzygy at pairing/report time; an explicit `pool_id`, if ever present, takes precedence (absent in the known format). |
| `coverage` | Yes | `metadata.coverage` → `additional_metadata.coverage` (numeric in the fixture, e.g. `687`); not in `EXCLUDED_DMP_KEYS` | None at the data level. Reference keeps it a string until the R `as.numeric` cast; syzygy stores JSON numeric — fine, cast on read. |
| `matched_tag` | No — no such key in beagle DMP metadata (it is a DMP-program flag, not lab metadata) | None | No source today; gap — 2/8 documents the fallback and flags it for 5/8. |
| `bam_exists` | No — not in metadata and not computed anywhere in syzygy today | None (the import SQL selects only `fm.metadata`; the linked `file` record's `path` is never read) | Sanctioned derivation: `filemetadata` → `file` has non-empty `path` → `1` else `0`, computed at pairing/report time from the file record already in the DB (2/8 documents it). |

---

## Verification notes (how this spec was checked)

- Raw-file positional indices: `sample`/`bamname`/`group` at 0-based [0]/[1]/[2]
  and the trailing fields at `scalar(@sp)-3/-4/-5` (i.e. `matched_tag` third
  from the end, `coverage` fourth, `pool_id` fifth; two unused columns after
  `matched_tag`) — `generate_dmp_tumor_normal_key.pl` lines 88–96; 12-column
  TSV header at line 83; `bam_exists` via `find … -name '*bam'` +
  `cut -f 1 -d'.'` basename check at lines 53–59, 91–92.
- 15-column names/order, no-op `filter(!is.na(bsuffix.x))`, tier-1 join on
  `(patient, pool_id)`, tier-2 rescue on `(patient, ver)` + `unique()` +
  `ifelse` backfill, summary formulas, 5 warning predicates and their row
  sets: `pair_tumor_with_normal.R` (read in full).
- R type behavior behind the warning-5 quirk: standard R semantics —
  `ifelse` promotes a numeric/character mix to character, and a
  character/numeric comparison coerces the numeric to character
  (lexicographic). No R runtime is installed on this machine to probe
  live; the 7/8 conformance harness should verify against the reference
  output directly.
- Field audit: fixture probe (Django fixture format, `model`/`fields`
  records) over all 8 `file_system.filemetadata` records + their linked
  `file_system.file` records; cross-checked against
  `dmp_translator.py` (`EXCLUDED_DMP_KEYS`, `PROMOTED_METADATA_KEYS`),
  `scripts/import_beagle_dump.py` (import selects `fm.metadata` only),
  `scripts/.beagle_ground_truth.example.json`, and
  `pairing_strategies.py` (`DmpPairingStrategy`, lines 1009–1142).

---

## 7/8 completion note (DMP reference conformance — gate result)

**Gate run (2026-11-06, commit 8183509, `feature/pairing-adjustments`, local,
working tree clean):**
`python -m pytest tests/ -q` (repo venv) →
**458 passed / 2 skipped / 0 failed** (4m12s). Baseline at ticket start was
416 passed / 2 skipped; the delta is exactly the 42 new conformance tests
(28 endpoint + 14 R cross-checks). The 2 skips are the NATS e2e tests
(`test_nats_e2e.py`, `test_nats_ingest.py` — dev stack down), identical to the
baseline. No non-DMP failures; zero regressions in other pairing logics.

**Conformance harness (present):** `tests/test_dmp_reference_conformance.py`
— fixture-driven over the 14-case set
(`tests/fixtures/dmp_reference_conformance/cases/`: 12 required scenarios +
bonus case 13 + combined case 99), feeding each payload through the real
endpoint path (`pairing_report`, `export_pair_report`) and asserting the exact
15-column pair rows (endpoint frame order), 3 summary counts, and 5 warning
buckets against the byte-pinned reference expectations. 42/42 pass.

**DMP behaviors now matching the reference** (byte-for-byte vs the pinned
Perl→R expectations; those expectations were verified end-to-end against the
real `generate_dmp_tumor_normal_key.pl` + `pair_tumor_with_normal.R`, R 4.6.2,
2026-11-05):

- Two-tier pairing: tier-1 plain left-join on `(patient, pool_id)` with **no
  winner selection** — ambiguous matches fan out, one row per matching normal
  (case 02); no pool match → a single row with NA normal columns.
- Tier-2 IM_ver rescue on `(patient, IM_ver)` with `unique()` + `ifelse`
  backfill, applied only where tier 1 is NA (cases 03, 11); pool beats
  IM_ver (case 04); a normal may be shared by two tumors (case 05); unpaired
  normals produce no rows — tumor-only frame (case 12).
- `normal_IM_ver` backfill with the tumor's IM_ver wherever tier 1 is NA
  (cases 03, 06, 11), including rows with no normal at all.
- 15-column pair file: exact column names/order (c); endpoint frame order =
  paired rows (created-desc) then unmatched DMP tumor rows.
- 3 summary counts per the reference formulas (d): distinct patients among
  matched rows, matched row count, unmatched row count.
- 5 QC warning buckets per the reference conditions (e), including the
  warning-5 lexicographic quirk (case 13 pins `"100" < "50"` → TRUE) and
  warning 4's has-BAM-but-NA-coverage disjunct (case 08).

**Rscript cross-check: USED (not skipped).** Rscript is installed on this
host (`/opt/homebrew/bin/Rscript`; the pre-T7 gate note that it was absent is
stale). All 14 cross-checks ran — the real `pair_tumor_with_normal.R`
reproduces every pinned expectation byte-for-byte — and passed. The
cross-check stays skipif-gated (Rscript + reference script presence), so the
suite never depends on R.

**Existing DMP tests: reconciled** (commit ade3f9b). The winner-era tie test
in `tests/test_pairing_service.py` now asserts the no-winner fan-out
expectation (one pairing row per matching normal; `new_pairings` carries the
representative row). No stale DMP expectations remain in any DMP test file.

**Remaining documented deviations** (render-level; the harness applies them
via `endpoint_row` — the underlying pairing data is identical in every case):

1. `tumor_group`: the endpoint renders NA — `group` is a gap in the DMP model
   (g); the key file's group value is reference-side only.
2. `patient`: the stored `patient__dmp` column wins over the reference's name
   parse (g caveat 1) — case 10's unparseable id leaves the reference cell
   empty while the stored column says P-7000011.
3. Missing values: the reference leaves failed parses / missing pools as
   EMPTY cells; the endpoint renders every missing value as `NA`.
4. `tumor_matched`: the endpoint derives the flag from the pairing
   (`matched_tag` audited absent from real records — g); an unmatched tumor
   row renders `Unmatched` where the key file's `is_matched` can say `Matched`
   (case 06 / combined case 99 row 7).

Plus the spec-sanctioned data-level deviations (f/g): `bam_exists` is derived
from the beagle file record (non-empty `path` → 1) instead of the BAM mirror
listing, and `bamname` is the `anon_id` stand-in.

**Still open (not a 7/8 blocker):** the 5/8 stakeholder-review flag on real
`matched_tag` values remains open — real DMP records still carry none, so the
pairing-derived status stands; an explicit `matched_tag`, if the DMP program
ever supplies one, takes precedence (g, 5/8 flag).

**Unrelated failures: none.** Only T7 files committed for this series of
tickets: ade3f9b (reconcile), 1596576 (fixtures), 206843c (warning-5 quirk
fixture fix), 8183509 (conformance harness) — all local on
`feature/pairing-adjustments`, not pushed.
