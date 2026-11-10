# Case 02: ambiguous_two_same_pool_normals

Ambiguous: 2 same-pool normals.

## Pinned reference behavior

Tier-1 matches BOTH normals (same patient + pool). Fan-out to 2 rows (T x N01, T x N02), normal input order. tn_pairs=2. Warning 2 (ambiguous normals) fires: the tumor_bamname appears on 2 matched rows.

## Inputs
- Samples (3):
    - P-7000002-T01-IM6 (T, pool=POOL2, cov=600, bam=AT02T-T, matched=Matched, bam_present=True)
    - P-7000002-N01-IM6 (N, pool=POOL2, cov=588, bam=AT02N1-N, matched=Matched, bam_present=True)
    - P-7000002-N02-IM6 (N, pool=POOL2, cov=588, bam=AT02N2-N, matched=Matched, bam_present=True)

- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

  1. P-7000002-T01-IM6  x  P-7000002-N01-IM6
  2. P-7000002-T01-IM6  x  P-7000002-N02-IM6

Full frame: `expected/pair_rows.tsv` (columns: tumor_sample, patient,
tumor_idx, tumor_IM_ver, tumor_bamname, tumor_group, tumor_matched,
tumor_bam_exists, tumor_bam_coverage, normal_sample, normal_bamname,
normal_IM_ver, pool_id, normal_bam_exists, normal_bam_coverage).

## Summary counts (R lines 77-86)
  - patients: 1
  - tn_pairs: 2
  - unmatched_tumors: 0

## Warning buckets (R lines 96-165)
  - matched_tumors_without_normal: (empty)
  - tumor_samples_with_multiple_ambiguous_normals: ['P-7000002-T01-IM6']
  - samples_with_bams_missing: (empty)
  - low_coverage_tumors: (empty)
  - low_coverage_normals: (empty)

## Fill-rule notes
- `normal_IM_ver` backfill (R lines 63-64): where the tier-1 normal_IM_ver is
  NA, it is filled with `tumor_IM_ver`. For a tier-2 rescue the two are equal
  (the rescue joins on IM_ver); for a no-normal row it is the tumor's ver.
- `normal_bam_coverage` (R lines 34, 65-66): numeric after the as.numeric
  mutate; the rescue ifelse then adopts the type of the NON-NA argument, so
  the column is CHARACTER only when this case's frame contains at least one
  rescue match — which makes Warning 5's `< 50` a LEXICOGRAPHIC string
  compare ("100" < "50" is TRUE). Frames with no rescue row keep the column
  NUMERIC (Warning 5 numeric). Warning 4 (tumor) is always numeric.
- Row order is the reference key-file (tumor input) order, fan-out consecutive
  per tumor — NOT syzygy's created-desc order.
