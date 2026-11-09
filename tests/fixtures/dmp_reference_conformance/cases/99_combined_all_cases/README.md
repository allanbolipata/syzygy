# Case 99: combined_all_cases

Combined: all 13 required cases (cross-validation set).

## Pinned reference behavior

The 13 required patients combined into one run. This is the set cross-validated against the prior (untracked) conformance test's reliable columns (pairing structure, summary counts, warning sets). Pinned: patients=11, tn_pairs=13, unmatched=1. Row order is the reference key-file order (tumor input order), NOT syzygy's created-desc order. 14 rows total.

## Inputs
- Samples (27):
    - P-7000001-T01-IM6 (T, pool=POOL1, cov=600, bam=AT01T-T, matched=Matched, bam_present=True)
    - P-7000001-N01-IM6 (N, pool=POOL1, cov=588, bam=AT01N-N, matched=Matched, bam_present=True)
    - P-7000002-T01-IM6 (T, pool=POOL2, cov=600, bam=AT02T-T, matched=Matched, bam_present=True)
    - P-7000002-N01-IM6 (N, pool=POOL2, cov=588, bam=AT02N1-N, matched=Matched, bam_present=True)
    - P-7000002-N02-IM6 (N, pool=POOL2, cov=588, bam=AT02N2-N, matched=Matched, bam_present=True)
    - P-7000003-T01-IM6 (T, pool=POOL3A, cov=600, bam=AT03T-T, matched=Matched, bam_present=True)
    - P-7000003-N01-IM6 (N, pool=POOL3B, cov=588, bam=AT03N-N, matched=Matched, bam_present=True)
    - P-7000004-T01-IM7 (T, pool=POOL4, cov=600, bam=AT04T-T, matched=Matched, bam_present=True)
    - P-7000004-N01-IM8 (N, pool=POOL4, cov=588, bam=AT04N1-N, matched=Matched, bam_present=True)
    - P-7000004-N02-IM7 (N, pool=POOL4B, cov=588, bam=AT04N2-N, matched=Matched, bam_present=True)
    - P-7000005-T01-IM6 (T, pool=POOL5, cov=600, bam=AT05T1-T, matched=Matched, bam_present=True)
    - P-7000005-T02-IM6 (T, pool=POOL5, cov=600, bam=AT05T2-T, matched=Matched, bam_present=True)
    - P-7000005-N01-IM6 (N, pool=POOL5, cov=588, bam=AT05N-N, matched=Matched, bam_present=True)
    - P-7000006-T01-IM6 (T, pool=POOL6, cov=600, bam=AT06T-T, matched=Matched, bam_present=True)
    - P-7000007-T01-IM6 (T, pool=POOL7, cov=45, bam=AT07T-T, matched=Matched, bam_present=True)
    - P-7000007-N01-IM6 (N, pool=POOL7, cov=588, bam=AT07N-N, matched=Matched, bam_present=True)
    - P-7000008-T01-IM6 (T, pool=POOL8, cov=600, bam=AT08T-T, matched=Matched, bam_present=True)
    - P-7000008-N01-IM6 (N, pool=POOL8, cov=45, bam=AT08N-N, matched=Matched, bam_present=True)
    - P-7000009-T01-IM6 (T, pool=POOL9, cov=NA, bam=AT09T-T, matched=Matched, bam_present=True)
    - P-7000009-N01-IM6 (N, pool=POOL9, cov=588, bam=AT09N-N, matched=Matched, bam_present=True)
    - P-7000010-T01-IM6 (T, pool=POOL10, cov=600, bam=AT10T-T, matched=Matched, bam_present=True)
    - P-7000010-N01-IM6 (N, pool=POOL10, cov=588, bam=AT10N-N, matched=Matched, bam_present=False)
    - P-7000011-T01 (T, pool=POOL11A, cov=600, bam=AT11T-T, matched=Unmatched, bam_present=True)
    - P-7000011-N01-IM6 (N, pool=POOL11B, cov=588, bam=AT11N-N, matched=Matched, bam_present=True)
    - P-7000012-T01-IM6 (T, pool=(empty), cov=600, bam=AT12T-T, matched=Matched, bam_present=True)
    - P-7000012-N01-IM6 (N, pool=(empty), cov=588, bam=AT12N-N, matched=Matched, bam_present=True)
    - P-7000013-N01-IM6 (N, pool=POOL13, cov=588, bam=AT13N-N, matched=Matched, bam_present=True)

- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

  1. P-7000001-T01-IM6  x  P-7000001-N01-IM6
  2. P-7000002-T01-IM6  x  P-7000002-N01-IM6
  3. P-7000002-T01-IM6  x  P-7000002-N02-IM6
  4. P-7000003-T01-IM6  x  P-7000003-N01-IM6
  5. P-7000004-T01-IM7  x  P-7000004-N01-IM8
  6. P-7000005-T01-IM6  x  P-7000005-N01-IM6
  7. P-7000005-T02-IM6  x  P-7000005-N01-IM6
  8. P-7000006-T01-IM6  (unmatched — no normal)
  9. P-7000007-T01-IM6  x  P-7000007-N01-IM6
  10. P-7000008-T01-IM6  x  P-7000008-N01-IM6
  11. P-7000009-T01-IM6  x  P-7000009-N01-IM6
  12. P-7000010-T01-IM6  x  P-7000010-N01-IM6
  13. P-7000011-T01  (unmatched — no normal)
  14. P-7000012-T01-IM6  x  P-7000012-N01-IM6

Full frame: `expected/pair_rows.tsv` (columns: tumor_sample, patient,
tumor_idx, tumor_IM_ver, tumor_bamname, tumor_group, tumor_matched,
tumor_bam_exists, tumor_bam_coverage, normal_sample, normal_bamname,
normal_IM_ver, pool_id, normal_bam_exists, normal_bam_coverage).

## Summary counts (R lines 77-86)
  - patients: 11
  - tn_pairs: 13
  - unmatched_tumors: 1

## Warning buckets (R lines 96-165)
  - matched_tumors_without_normal: ['P-7000006-T01-IM6']
  - tumor_samples_with_multiple_ambiguous_normals: ['P-7000002-T01-IM6']
  - samples_with_bams_missing: ['P-7000010-T01-IM6', 'P-7000010-N01-IM6']
  - low_coverage_tumors: ['P-7000007-T01-IM6', 'P-7000009-T01-IM6']
  - low_coverage_normals: ['P-7000008-N01-IM6']

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
