# DMP reference-conformance fixtures (T7)

Per-case fixture set with **pinned expectations** for the DMP tumor/normal
pairing, derived from the reference pipeline:

- `generate_dmp_tumor_normal_key.pl` — parses the raw comma-separated key file
  into a 12-column TSV (sample, bamname, group, …, pool_id, coverage,
  matched_tag, …) with `bam_exists` from the mirror listing.
- `pair_tumor_with_normal.R` — two-tier pairing (tier-1 left-join on
  `(patient, pool_id)`, tier-2 rescue on `(patient, IM_ver)`), a 15-column
  tumor-only pair frame, `normal_IM_ver` backfill, 3 summary counts, and 5 QC
  warning buckets.

Both live in `~/dev/bam-mirror-tools/scripts/`. The expectations were
**derived by a faithful Python port of the full Perl→R pipeline**
(`_harness/derive_reference.py`, line-mapped to the R script),
**cross-validated** against the ground-truth test
`tests/test_dmp_reference_conformance.py` (`_harness/cross_validate.py`), and
**verified end-to-end against the real reference**: the real
`generate_dmp_tumor_normal_key.pl` (perl) and the real
`pair_tumor_with_normal.R` (R 4.6.2, dplyr + data.table) were run on every
case's key file + BAM manifest, reproducing every pinned pair file, summary
count, and warning bucket (2026-11-05).

## Layout

```
dmp_reference_conformance/
├── _harness/
│   ├── derive_reference.py    # faithful Python port of Perl→R (the authority)
│   ├── generate_fixtures.py   # defines the cases, derives + writes every fixture
│   └── cross_validate.py      # harness vs the ground-truth test's EXPECTED_*
└── cases/
    └── <NN>_<name>/
        ├── key_file.csv        # raw comma key file (12 fields, end-anchored)
        ├── bam_manifest.json   # mirror BAM listing (drives bam_exists)
        ├── payload.json        # beagle-shaped DMP_data.json payload (2/8 synthetic)
        ├── README.md           # what this case covers + the pinned expectation
        └── expected/
            ├── pair_rows.tsv   # exact 15 pair rows, reference row order
            ├── summary.json    # patients / tn_pairs / unmatched_tumors
            └── warnings.json   # the 5 QC warning buckets
```

## Cases (01–12 required, 13 bonus, 99 combined)

| #  | case                              | pins                                                                 |
|----|-----------------------------------|----------------------------------------------------------------------|
| 01 | clean single pair                 | one tumor + one normal, single row, no warnings                       |
| 02 | ambiguous two same-pool normals   | tier-1 fan-out: 1 tumor × 2 same-pool normals → 2 rows; warning-2     |
| 03 | rescue-only diff pool same IM_ver | no pool match; tier-2 rescue on (patient, IM_ver); normal_IM_ver backfill |
| 04 | pool beats IM_ver                 | same-pool normal wins tier-1 despite a different IM_ver (IM8); the different-pool same-ver normal loses |
| 05 | normal shared by two tumors       | 2 same-pool tumors share one normal → 2 rows                          |
| 06 | matched flag without normal       | tumor matched_tag=Matched but no normal → warning-1; normal_IM_ver backfilled to tumor ver |
| 07 | low-coverage tumor <100 & normal <50 | tumor cov 45 + normal cov 45 → warnings 4 & 5                        |
| 08 | NA coverage                       | tumor cov NA (has BAM) → warning-4 (has BAM, no coverage); normal cov 588 |
| 09 | missing BAM                       | tumor+normal BAMs absent from mirror → warning-3                      |
| 10 | unparseable sample name           | name fails `P-\d+-\w+-\w+` → empty patient/idx/ver (not "NA"); unmatched |
| 11 | missing pool_id both sides        | pool_id empty → no tier-1; rescue on IM_ver; normal_IM_ver backfill   |
| 12 | unpaired normal present           | a normal with no tumor → 0 pair rows (tumor-only frame)               |
| 13 | warning-5 lexicographic quirk     | a rescue pair (T02 x N02, cov 45) forces `normal_bam_coverage` to character (R ifelse takes the non-NA argument's type), so the tier-1 normal's cov "100" compares lexicographically: `"100"<"50"` TRUE → both normals flagged (bonus, not a required case; without the rescue row the column stays numeric and "100" is NOT flagged) |
| 99 | combined all cases                | cases 01–12, all 27 samples in one key file — the cross-validation set (14 rows, summary 11/13/1) |

## Reference semantics (definitive, from the R script)

- **Tier-1** is a **plain left-join on `(patient, pool_id)`** with **no**
  IM_ver filter (R lines 18–22). Two same-pool normals both fan out. IM_ver
  comparison only matters in tier-2 rescue.
- **T/N split** comes from the bamname's trailing suffix (R lines 19–20:
  `bsuffix == "T"`/`"N"`), not a type column.
- **`normal_IM_ver` backfill** (R 63–64): backfilled with the tumor's IM_ver
  wherever the tier-1 match is NA (rescue-only and unmatched rows).
- **Row order** = input key-file order; fan-out rows per tumor are consecutive.
- **Unparseable name** → **empty strings** (not "NA") for patient/idx/ver —
  a documented divergence from the syzygy code.
- **Warning-5** (normal low-cov <50) compares `normal_bam_coverage`, numeric
  after R line 34 but **character when the frame contains at least one rescue
  match** (the rescue `ifelse`, R 65–66, adopts the type of the non-NA
  argument — verified against real R 4.6.2: `ifelse(FALSE, NA_character_, 100)`
  stays numeric). So the compare is **lexicographic** in frames with a rescue
  row (`"45"<"50"` TRUE, `"588"<"50"` FALSE, `"100"<"50"` TRUE — the case-13
  quirk) and **numeric** otherwise.
  **Warning-4** (tumor <100) is always **numeric** and also fires on a tumor
  that has a BAM but no coverage (R 147–148).
- **Summary counts** (R 77–86) and **warnings 1 & 2** use the key file's
  `matched_tag` column **verbatim** (`tumor_matched == "Matched"`).

## Regenerate / verify

```bash
cd tests/fixtures/dmp_reference_conformance/_harness
python3 generate_fixtures.py   # rewrites every cases/<NN>/ from the samples
python3 cross_validate.py      # harness vs the ground-truth test's EXPECTED_*
```

`generate_fixtures.py` is the source of truth for the inputs; it re-derives and
overwrites every `expected/` file. `cross_validate.py` exits 0 when the harness
matches the ground-truth test on every **reliable** column. For the
end-to-end check against the real reference scripts (perl + Rscript with
dplyr/data.table), see the T7 verification: real Perl stage → real R stage →
diff every pinned file (done 2026-11-05, 14/14 cases).

## Documented divergences (reference vs the syzygy code)

The expectations pin the **reference** (Perl→R) output. Where the syzygy code
diverges, that is recorded here (and by `cross_validate.py`) — not papered over:

- **bamname** (cols 4, 10): sanctioned deviation — the reference wants a
  `-T`/`-N`-suffixed bamname; the DMP payload carries the beagle `anon_id`.
- **tumor_group** (col 5): the reference echoes the key file's group
  (`GRP-DMP`); the syzygy code emits `NA` (group is a gap in the DMP model).
- **tumor_matched** (col 6) + the **3 summary counts** + **warnings 1 & 2**:
  the reference uses the key-file `matched_tag` verbatim; the syzygy code
  **derives** it (a tumor is "Matched" if it paired, else "Unmatched"). On the
  combined set the reference (key-file tags) and the syzygy code (derived) both
  land on patients=11 / tn_pairs=13 / unmatched=1, but for different reasons.
- **unparseable patient** (case 10): the reference yields `""` (the R name
  regex needs four hyphen groups); the syzygy code still extracts the patient.
- **empty vs NA**: the reference renders missing values as `""`; the syzygy code
  renders `NA`.
