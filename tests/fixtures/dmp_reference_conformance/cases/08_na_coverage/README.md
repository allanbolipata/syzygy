# Case 08: na_coverage

NA coverage.

## Pinned reference behavior

Tumor coverage is NA (null). as.numeric -> NA, so Warning 4's numeric (coverage < 100) is NA->false and the is.na(bam)&!is.na(coverage) branch is false (bam present) -> NOT flagged. Normal coverage 588 -> not low. One clean row, no warnings. Pins that an NA coverage is not treated as low coverage.

## Inputs
- Samples (2):
    - P-7000009-T01-IM6 (T, pool=POOL9, cov=NA, bam=AT09T-T, matched=Matched, bam_present=True)
    - P-7000009-N01-IM6 (N, pool=POOL9, cov=588, bam=AT09N-N, matched=Matched, bam_present=True)

- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

  1. P-7000009-T01-IM6  x  P-7000009-N01-IM6

Full frame: `expected/pair_rows.tsv` (columns: tumor_sample, patient,
tumor_idx, tumor_IM_ver, tumor_bamname, tumor_group, tumor_matched,
tumor_bam_exists, tumor_bam_coverage, normal_sample, normal_bamname,
normal_IM_ver, pool_id, normal_bam_exists, normal_bam_coverage).

## Summary counts (R lines 77-86)
  - patients: 1
  - tn_pairs: 1
  - unmatched_tumors: 0

## Warning buckets (R lines 96-165)
  - matched_tumors_without_normal: (empty)
  - tumor_samples_with_multiple_ambiguous_normals: (empty)
  - samples_with_bams_missing: (empty)
  - low_coverage_tumors: ['P-7000009-T01-IM6']
  - low_coverage_normals: (empty)

## Fill-rule notes
- `normal_IM_ver` backfill (R lines 63-64): where the tier-1 normal_IM_ver is
  NA, it is filled with `tumor_IM_ver`. For a tier-2 rescue the two are equal
  (the rescue joins on IM_ver); for a no-normal row it is the tumor's ver.
- `normal_bam_coverage` is a CHARACTER column (the rescue ifelse mixes the
  numeric normal coverage with the character rescued coverage, R lines 65-66)
  — this is what makes Warning 5 lexicographic.
- Row order is the reference key-file (tumor input) order, fan-out consecutive
  per tumor — NOT syzygy's created-desc order.
