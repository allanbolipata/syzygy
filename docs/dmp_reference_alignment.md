# DMP Reference Alignment — Canonical Spec

**Status:** 1/8 of the DMP Reference Alignment series (spec + key-field audit).
Tickets 2–7 implement against this document. **Where this doc and the reference
scripts disagree, the scripts win.** Stakeholder rule: when in doubt, follow
`bam-mirror-tools`.

Reference implementation (authoritative): `/Users/bolipatc/dev/bam-mirror-tools`

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

---

## f. Syzygy ↔ reference mapping

| reference field | syzygy source | mapping rule |
|---|---|---|
| `sample` | `Sample.primary_id` (`sample__dmp` column) | 1:1. The import promotes `metadata.sample` to the sample row and excludes it from `additional_metadata` (`EXCLUDED_DMP_KEYS`). |
| `patient` | `Sample.patient__dmp` (from `metadata.patient.dmp`) | Same `P-…` value the reference parses out of the sample name; syzygy stores it as its own field. |
| `idx` / `ver` (IM_ver) | not stored today | Reference derives them in Perl from the sample name (`(P-\d+)-(\w+)-(\w+)`). Tickets 2/8 or 3/8 decide: store at ingest vs. derive at pairing. 3/8's rescue tier needs `ver` = last dash-segment of the sample name. |
| `bamname` | `Sample.anon_id` (from `metadata.anon_id`) | **Stand-in**: no `bamname` key exists in beagle DMP records. An explicit `bamname` in the incoming payload takes precedence over `anon_id`. (The beagle file record's `.bam` `file_name` is not imported today and is not the source of truth for this field.) |
| `group` | not present in DMP records | No beagle equivalent. Optional payload field (2/8); carried through to `tumor_group` untouched. |
| `pool_id` | `additional_metadata.project_name` | **Stakeholder-confirmed rename of `project_name`**: keep reading `pool_id` from `additional_metadata.project_name` as the fallback; an explicit `pool_id` in the incoming payload takes precedence. Beagle records contain no `pool_id` key (verified in the fixture). |
| `coverage` | `additional_metadata.coverage` (numeric in the fixture) | Stored as-is from `metadata.coverage` (not in `EXCLUDED_DMP_KEYS`). Reference treats it as a string end-to-end until the R `as.numeric` cast; syzygy stores JSON numeric. |
| `matched_tag` → `is_matched` → `tumor_matched` | not present in DMP records | **A DMP-program value, not derivable from pairing.** Must arrive with the incoming payload / key-file ingest (2/8). Open item for 2/8: default when the field is absent. |
| `bam_exists` | not present; not computed today | **The one sanctioned deviation.** Reference: Perl checks the BAM path exists in the mirror (`bamname` ∈ mirror listing). Syzygy equivalent: the sample's beagle file record (`file_system.filemetadata` → `file_system.file`) has a **non-empty `path`** → `1`, else `0`. The import currently selects only `fm.metadata` and never joins the file record, so 2/8 must add this derivation at ingest. |

### Current state (for context — changed by tickets 3/8–5/8)

Syzygy's existing `dmp` pairing strategy (`pairing_strategies.py`,
`DmpPairingStrategy`) pairs DMP tumors → DMP normals strictly inside the DMP
group on **`(patient__dmp, gene_panel)`** (case-insensitive) and picks a
**single deterministic winner** (first in `primary_id` string order). It has
no `pool_id` tier, no IM_ver rescue, no ambiguous-match fan-out, and
produces no summary/QC output. The reference behavior specified in b–e above
is what tickets 3/8–5/8 implement.

---

## Field audit

Fixture examined: `/Users/bolipatc/dev/beagle/file_system/fixtures/DMP_data.json`
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
| `bamname` | No — no such key in beagle DMP metadata | Stand-in: `metadata.anon_id` → `Sample.anon_id` column (all 8 fixture records carry `anon_id`) | No `bamname` key anywhere in the record; explicit `bamname` payload field (2/8) takes precedence over the `anon_id` stand-in. The `.bam` file record `file_name` is not imported. |
| `group` | No — no such key in beagle DMP metadata | None | No source today; optional payload field (2/8). |
| `pool_id` | No — no `pool_id` key (verified: absent from all 8 fixture records) | Fallback: `metadata.project_name` → `additional_metadata.project_name` (present in all 8 fixture records) | Stakeholder-confirmed rename `pool_id` = `project_name`; explicit `pool_id` payload field (2/8) takes precedence over the `project_name` fallback. |
| `coverage` | Yes | `metadata.coverage` → `additional_metadata.coverage` (numeric in the fixture, e.g. `687`); not in `EXCLUDED_DMP_KEYS` | None at the data level. Reference keeps it a string until the R `as.numeric` cast; syzygy stores JSON numeric — fine, cast on read. |
| `matched_tag` | No — no such key in beagle DMP metadata (it is a DMP-program flag, not lab metadata) | None | No source today; must come from the incoming payload / key-file ingest (2/8). Default-when-absent is an open 2/8 decision. |
| `bam_exists` | No — not in metadata and not computed anywhere in syzygy today | None (the import SQL selects only `fm.metadata`; the linked `file` record's `path` is never read) | Sanctioned derivation: `filemetadata` → `file` has non-empty `path` → `1` else `0`. 2/8 must add the file-record join at ingest. |

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
