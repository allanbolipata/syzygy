# Case 13: warning5_lexicographic_quirk

BONUS: warning-5 lexicographic quirk (rescue-forced character column).

## Pinned reference behavior

Two-tier frame in one patient. T01 (POOL14, IM6) tier-1 matches N01 (POOL14, coverage '100'). T02 (POOL14C, IM7) has no same-pool normal, so the tier-2 rescue matches N02 (POOL14D, IM7, coverage 45). Because the frame now contains a rescue match (a non-NA rescued coverage), R's rescue ifelse (R lines 65-66, type of the non-NA argument) promotes normal_bam_coverage to CHARACTER for the WHOLE frame — so Warning 5's `normal_bam_coverage < 50` becomes a LEXICOGRAPHIC string compare: '100' < '50' is TRUE, and the coverage-100 normal N01 IS flagged alongside N02 (45). BOUNDARY (verified against real R 4.6.2): without the rescue pair the column stays NUMERIC and a '100' normal is NOT flagged (ifelse(FALSE, NA_character_, 100) -> numeric). 2 rows; Warning 5 = [P-7000014-N01-IM6, P-7000014-N02-IM7]; patients=1, tn_pairs=2, unmatched=0.

## Inputs
- Samples (4):
    - P-7000014-T01-IM6 (T, pool=POOL14, cov=600, bam=AT14T-T, matched=Matched, bam_present=True)
    - P-7000014-N01-IM6 (N, pool=POOL14, cov=100, bam=AT14N-N, matched=Matched, bam_present=True)
    - P-7000014-T02-IM7 (T, pool=POOL14C, cov=600, bam=AT14T2-T, matched=Matched, bam_present=True)
    - P-7000014-N02-IM7 (N, pool=POOL14D, cov=45, bam=AT14N2-N, matched=Matched, bam_present=True)

- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

  1. P-7000014-T01-IM6  x  P-7000014-N01-IM6
  2. P-7000014-T02-IM7  x  P-7000014-N02-IM7

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
  - tumor_samples_with_multiple_ambiguous_normals: (empty)
  - samples_with_bams_missing: (empty)
  - low_coverage_tumors: (empty)
  - low_coverage_normals: ['P-7000014-N01-IM6', 'P-7000014-N02-IM7']

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
