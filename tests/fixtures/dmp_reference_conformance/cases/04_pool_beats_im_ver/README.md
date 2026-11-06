# Case 04: pool_beats_im_ver

Pool beats IM_ver.

## Pinned reference behavior

Tumor (POOL4, IM7) has a same-pool normal N01 (POOL4, IM8 — a DIFFERENT IM_ver than the tumor) and a different-pool normal N02 (POOL4B, IM7 — the tumor's own ver). Tier-1 matches N01 by pool even though its IM_ver (IM8) differs from the tumor's (IM7) — the pool match wins. Because the tumor already has a tier-1 match, it is not in the rescue set, so the tier-2 rescue (which would match N02 on IM_ver) never runs and N02 is left unpaired. One row (T x N01), normal_IM_ver = IM8. This pins that a pool match 'beats' an IM_ver rescue.

## Inputs
- Samples (3):
    - P-7000004-T01-IM7 (T, pool=POOL4, cov=600, bam=AT04T-T, matched=Matched, bam_present=True)
    - P-7000004-N01-IM8 (N, pool=POOL4, cov=588, bam=AT04N1-N, matched=Matched, bam_present=True)
    - P-7000004-N02-IM7 (N, pool=POOL4B, cov=588, bam=AT04N2-N, matched=Matched, bam_present=True)

- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

  1. P-7000004-T01-IM7  x  P-7000004-N01-IM8

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
