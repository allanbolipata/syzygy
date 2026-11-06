"""Generator for the DMP reference-conformance fixture set.

Defines the 12 required cases (+ 1 bonus quirk pin + 1 combined cross-
validation set) as beagle-shaped payloads + reference raw key files, then runs
the faithful reference port (derive_reference.py) to derive and write each
case's pinned expectations (15-column pair file, 3 summary counts, 5 warning
buckets, fill rules).

Run:  python3 generate_fixtures.py
Writes into:  tests/fixtures/dmp_reference_conformance/cases/<NN>_<slug>/

The reference raw key file is comma-separated with fields anchored from the
END (spec §f / raw-key doc): sp[0]=sample, sp[1]=bamname, sp[2]=group, then
padding dummies, then pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3],
with two trailing dummy fields. All 12 fields are non-empty so the Perl's
trailing-empty-split cannot disturb the anchoring.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from derive_reference import perl_parse, pair, format_pair_tsv  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(HERE, "..", "cases"))
GROUP = "GRP-DMP"  # the DMP-program group, carried through untouched


def S(sample, patient, ttype, pool, coverage, bamname, matched, present=True):
    """One sample: its name, stored patient, tumor/normal type, pool_id,
    coverage (str or None for NA), bamname (anon_id stand-in, T/N-suffixed),
    the DMP matched_tag, and whether its BAM is in the mirror."""
    return {
        "sample": sample, "patient": patient, "type": ttype, "pool": pool,
        "coverage": coverage, "bamname": bamname, "matched": matched,
        "present": present,
    }


# All samples in reference key-file order: patient ascending, tumors before
# normals within a patient. This order is what the reference pair frame echoes
# (tumor input order, fan-out consecutive per tumor).
SAMPLES = [
    # P-7000001 — clean single pair
    S("P-7000001-T01-IM6", "P-7000001", "T", "POOL1", "600", "AT01T-T", "Matched"),
    S("P-7000001-N01-IM6", "P-7000001", "N", "POOL1", "588", "AT01N-N", "Matched"),
    # P-7000002 — ambiguous: 2 same-pool normals
    S("P-7000002-T01-IM6", "P-7000002", "T", "POOL2", "600", "AT02T-T", "Matched"),
    S("P-7000002-N01-IM6", "P-7000002", "N", "POOL2", "588", "AT02N1-N", "Matched"),
    S("P-7000002-N02-IM6", "P-7000002", "N", "POOL2", "588", "AT02N2-N", "Matched"),
    # P-7000003 — rescue-only: different pool, same IM_ver
    S("P-7000003-T01-IM6", "P-7000003", "T", "POOL3A", "600", "AT03T-T", "Matched"),
    S("P-7000003-N01-IM6", "P-7000003", "N", "POOL3B", "588", "AT03N-N", "Matched"),
    # P-7000004 — pool beats IM_ver: same-pool normal with a DIFFERENT ver
    # (IM8 != IM7, tier-1 winner by pool) + different-pool same-ver normal
    # (IM7 == IM7, the tier-2 candidate that loses)
    S("P-7000004-T01-IM7", "P-7000004", "T", "POOL4", "600", "AT04T-T", "Matched"),
    S("P-7000004-N01-IM8", "P-7000004", "N", "POOL4", "588", "AT04N1-N", "Matched"),
    S("P-7000004-N02-IM7", "P-7000004", "N", "POOL4B", "588", "AT04N2-N", "Matched"),
    # P-7000005 — one normal shared by 2 same-pool tumors
    S("P-7000005-T01-IM6", "P-7000005", "T", "POOL5", "600", "AT05T1-T", "Matched"),
    S("P-7000005-T02-IM6", "P-7000005", "T", "POOL5", "600", "AT05T2-T", "Matched"),
    S("P-7000005-N01-IM6", "P-7000005", "N", "POOL5", "588", "AT05N-N", "Matched"),
    # P-7000006 — matched flag, no normal
    S("P-7000006-T01-IM6", "P-7000006", "T", "POOL6", "600", "AT06T-T", "Matched"),
    # P-7000007 — low-coverage tumor (45)
    S("P-7000007-T01-IM6", "P-7000007", "T", "POOL7", "45", "AT07T-T", "Matched"),
    S("P-7000007-N01-IM6", "P-7000007", "N", "POOL7", "588", "AT07N-N", "Matched"),
    # P-7000008 — low-coverage normal (45)
    S("P-7000008-T01-IM6", "P-7000008", "T", "POOL8", "600", "AT08T-T", "Matched"),
    S("P-7000008-N01-IM6", "P-7000008", "N", "POOL8", "45", "AT08N-N", "Matched"),
    # P-7000009 — NA tumor coverage
    S("P-7000009-T01-IM6", "P-7000009", "T", "POOL9", None, "AT09T-T", "Matched"),
    S("P-7000009-N01-IM6", "P-7000009", "N", "POOL9", "588", "AT09N-N", "Matched"),
    # P-7000010 — missing BAM (the normal's BAM is not in the mirror)
    S("P-7000010-T01-IM6", "P-7000010", "T", "POOL10", "600", "AT10T-T", "Matched"),
    S("P-7000010-N01-IM6", "P-7000010", "N", "POOL10", "588", "AT10N-N", "Matched",
      present=False),
    # P-7000011 — unparseable tumor sample name (no IM suffix)
    S("P-7000011-T01", "P-7000011", "T", "POOL11A", "600", "AT11T-T", "Unmatched"),
    S("P-7000011-N01-IM6", "P-7000011", "N", "POOL11B", "588", "AT11N-N", "Matched"),
    # P-7000012 — missing pool_id on both sides (same IM_ver)
    S("P-7000012-T01-IM6", "P-7000012", "T", "", "600", "AT12T-T", "Matched"),
    S("P-7000012-N01-IM6", "P-7000012", "N", "", "588", "AT12N-N", "Matched"),
    # P-7000013 — unpaired normal (no tumor)
    S("P-7000013-N01-IM6", "P-7000013", "N", "POOL13", "588", "AT13N-N", "Matched"),
    # P-7000014 — BONUS: warning-5 lexicographic quirk. The tier-1 pair's
    # normal has coverage "100"; the rescue pair (T02 x N02, via IM7) gives
    # the frame a non-NA rescued coverage, which makes R's rescue ifelse
    # (type of the non-NA argument) promote normal_bam_coverage to CHARACTER
    # for the whole frame — so "100" < "50" flags N01 lexicographically.
    S("P-7000014-T01-IM6", "P-7000014", "T", "POOL14", "600", "AT14T-T", "Matched"),
    S("P-7000014-N01-IM6", "P-7000014", "N", "POOL14", "100", "AT14N-N", "Matched"),
    S("P-7000014-T02-IM7", "P-7000014", "T", "POOL14C", "600", "AT14T2-T", "Matched"),
    S("P-7000014-N02-IM7", "P-7000014", "N", "POOL14D", "45", "AT14N2-N", "Matched"),
]

# The 12 required cases (task t_40224000) + 1 bonus + 1 combined.
CASES = [
    ("01", "clean_single_pair", ["P-7000001"],
     "Clean single pair.",
     "Tier-1 left join on (patient, pool_id) matches exactly one normal. One "
     "pair row; normal_* filled from the normal; normal_IM_ver = normal ver "
     "(not the backfill). Counts: patients=1, tn_pairs=1, unmatched=0. No "
     "warnings."),
    ("02", "ambiguous_two_same_pool_normals", ["P-7000002"],
     "Ambiguous: 2 same-pool normals.",
     "Tier-1 matches BOTH normals (same patient + pool). Fan-out to 2 rows "
     "(T x N01, T x N02), normal input order. tn_pairs=2. Warning 2 "
     "(ambiguous normals) fires: the tumor_bamname appears on 2 matched rows."),
    ("03", "rescue_only_different_pool_same_ver", ["P-7000003"],
     "Rescue-only: different pool, same IM_ver.",
     "Tier-1 finds nothing (pools differ: POOL3A vs POOL3B). Tier-2 rescue on "
     "(patient, IM_ver) matches the normal (both IM6). One row; normal_IM_ver "
     "= IM6 (the backfill of tumor_IM_ver, equal to the rescued normal's ver). "
     "pool_id = POOL3A (the tumor's pool / tier-1 key)."),
    ("04", "pool_beats_im_ver", ["P-7000004"],
     "Pool beats IM_ver.",
     "Tumor (POOL4, IM7) has a same-pool normal N01 (POOL4, IM8 — a "
     "DIFFERENT IM_ver than the tumor) and a different-pool normal N02 "
     "(POOL4B, IM7 — the tumor's own ver). Tier-1 matches N01 by pool even "
     "though its IM_ver (IM8) differs from the tumor's (IM7) — the pool match "
     "wins. Because the tumor already has a tier-1 match, it is not in the "
     "rescue set, so the tier-2 rescue (which would match N02 on IM_ver) never "
     "runs and N02 is left unpaired. One row (T x N01), normal_IM_ver = IM8. "
     "This pins that a pool match 'beats' an IM_ver rescue."),
    ("05", "normal_shared_by_two_tumors", ["P-7000005"],
     "Normal shared by 2 same-pool tumors.",
     "Two same-pool tumors (T01, T02) both match the single normal N01 by "
     "tier-1. Two rows (T01 x N01, T02 x N01), tumor input order. tn_pairs=2. "
     "The normal appears on 2 rows; no warning (warnings are per-tumor)."),
    ("06", "matched_flag_without_normal", ["P-7000006"],
     "Matched flag, no normal.",
     "Tumor is tagged Matched but has no normal (tier-1 and tier-2 both empty). "
     "One unmatched row: normal_* NA, normal_IM_ver backfilled to tumor_IM_ver "
     "(IM6). Warning 1 (matched without normal) fires. Counts: patients=1, "
     "tn_pairs=1, unmatched=0 (it IS a matched row, just with no normal)."),
    ("07", "low_coverage_tumor_and_normal", ["P-7000007", "P-7000008"],
     "Low coverage: tumor <100 and normal <50.",
     "P-7000007: tumor coverage 45 (<100 numeric) -> Warning 4 (low-coverage "
     "tumors). P-7000008: normal coverage 45 (<50 numeric — no rescue row in "
     "this case, so normal_bam_coverage stays numeric) -> Warning 5 "
     "(low-coverage normals). Both pairs match tier-1. 2 rows; "
     "tn_pairs=2."),
    ("08", "na_coverage", ["P-7000009"],
     "NA coverage.",
     "Tumor coverage is NA (null). as.numeric -> NA, so Warning 4's numeric "
     "(coverage < 100) is NA->false and the is.na(bam)&!is.na(coverage) branch "
     "is false (bam present) -> NOT flagged. Normal coverage 588 -> not "
     "low. One clean row, no warnings. Pins that an NA coverage is not treated "
     "as low coverage."),
    ("09", "missing_bam", ["P-7000010"],
     "Missing BAM.",
     "The normal's BAM is absent from the mirror -> normal_bam_exists=0. The "
     "pair still matches tier-1 (pairing is on patient+pool, not bam_exists). "
     "Warning 3 (samples with missing BAM) fires, listing both the tumor and "
     "the normal. One row; tn_pairs=1."),
    ("10", "unparseable_sample_name", ["P-7000011"],
     "Unparseable sample name.",
     "Tumor name 'P-7000011-T01' has no IM suffix, so the Perl name regex "
     "(P-\\d+)-(\\w+)-(\\w+) fails -> patient/idx/ver are EMPTY (not NA). "
     "Tier-1 and tier-2 both find nothing (empty keys + different pool). One "
     "unmatched row with empty patient/idx/ver/normal_IM_ver cells. DIVERGENCE "
     "from syzygy: syzygy renders the stored-column gap as 'NA'; the reference "
     "writes empty cells (spec §g). Warning 1 does NOT fire (matched_tag is "
     "'Unmatched', not 'Matched'). Counts: patients=0, tn_pairs=0, "
     "unmatched=1."),
    ("11", "missing_pool_id_both_sides", ["P-7000012"],
     "Missing pool_id on both sides.",
     "Both tumor and normal have empty pool_id. A missing pool never matches "
     "tier 1 (dplyr NA never matches), so tier-1 is skipped. Tier-2 rescue on "
     "(patient, IM_ver) matches (both IM6). One row; normal_IM_ver=IM6 "
     "(backfill), pool_id empty. Same output as a tier-1 match would give, but "
     "the reference reaches it via tier 2 (spec/syzygy interpretation)."),
    ("12", "unpaired_normal_present", ["P-7000013"],
     "Unpaired normal present.",
     "A normal with no tumor. The pair frame is tumor-only, so it produces NO "
     "row (it is dropped by the tumor filter). 0 rows; patients=0, tn_pairs=0, "
     "unmatched=0. Pins that a lone normal is excluded from the pair file and "
     "from the counts."),
    ("13", "warning5_lexicographic_quirk", ["P-7000014"],
     "BONUS: warning-5 lexicographic quirk (rescue-forced character column).",
     "Two-tier frame in one patient. T01 (POOL14, IM6) tier-1 matches N01 "
     "(POOL14, coverage '100'). T02 (POOL14C, IM7) has no same-pool normal, "
     "so the tier-2 rescue matches N02 (POOL14D, IM7, coverage 45). Because "
     "the frame now contains a rescue match (a non-NA rescued coverage), R's "
     "rescue ifelse (R lines 65-66, type of the non-NA argument) promotes "
     "normal_bam_coverage to CHARACTER for the WHOLE frame — so Warning 5's "
     "`normal_bam_coverage < 50` becomes a LEXICOGRAPHIC string compare: "
     "'100' < '50' is TRUE, and the coverage-100 normal N01 IS flagged "
     "alongside N02 (45). BOUNDARY (verified against real R 4.6.2): without "
     "the rescue pair the column stays NUMERIC and a '100' normal is NOT "
     "flagged (ifelse(FALSE, NA_character_, 100) -> numeric). 2 rows; "
     "Warning 5 = [P-7000014-N01-IM6, P-7000014-N02-IM7]; patients=1, "
     "tn_pairs=2, unmatched=0."),
    ("99", "combined_all_cases",
     [f"P-700000{i}" for i in range(1, 10)] + ["P-7000010", "P-7000011",
                                              "P-7000012", "P-7000013"],
     "Combined: all 13 required cases (cross-validation set).",
     "The 13 required patients combined into one run. This is the set "
     "cross-validated against the prior (untracked) conformance test's "
     "reliable columns (pairing structure, summary counts, warning sets). "
     "Pinned: patients=11, tn_pairs=13, unmatched=1. Row order is the "
     "reference key-file order (tumor input order), NOT syzygy's "
     "created-desc order. 14 rows total."),
]


def key_row(s):
    """The 12-field comma-separated raw key-file row (spec §f / raw-key doc)."""
    cov = "NA" if s["coverage"] is None else str(s["coverage"])
    return [
        s["sample"], s["bamname"], GROUP,
        "X", "X", "X", "X",          # padding dummies (sp[3..6])
        s["pool"],                    # sp[7] pool_id  = sp[n-5]
        cov,                          # sp[8] coverage  = sp[n-4]
        s["matched"],                 # sp[9] matched_tag = sp[n-3]
        "X", "X",                     # sp[10..11] trailing dummies
    ]


def payload_record(s, seq):
    """A beagle DMP_data.json-shape record (2/8 synthetic format)."""
    return {
        "model": "file_system.filemetadata",
        "fields": {
            "id": f"f{seq:012d}",
            "metadata": {
                "sample": s["sample"],
                "type": s["type"],
                "patient": {"dmp": s["patient"]},
                "anon_id": s["bamname"],
                "project_name": s["pool"],
                "coverage": (None if s["coverage"] is None else int(s["coverage"])),
            },
        },
    }


def build_case(num, slug, patients, title, behavior):
    samples = [s for s in SAMPLES if s["patient"] in set(patients)]
    key_rows = [key_row(s) for s in samples]
    bam_manifest = [s["bamname"] for s in samples if s["present"]]

    tabbed = perl_parse(key_rows, bam_manifest)
    pair_rows, summary, warnings = pair(tabbed)

    case_dir = os.path.join(OUT_DIR, f"{num}_{slug}")
    exp_dir = os.path.join(case_dir, "expected")
    os.makedirs(exp_dir, exist_ok=True)

    # --- inputs -----------------------------------------------------------
    payload = [payload_record(s, i + 1) for i, s in enumerate(samples)]
    with open(os.path.join(case_dir, "payload.json"), "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    with open(os.path.join(case_dir, "key_file.csv"), "w") as f:
        for row in key_rows:
            f.write(",".join(row) + "\n")
    with open(os.path.join(case_dir, "bam_manifest.json"), "w") as f:
        json.dump({"bamnames": bam_manifest}, f, indent=2)
        f.write("\n")

    # --- expectations (derived from the reference port) -------------------
    with open(os.path.join(exp_dir, "pair_rows.tsv"), "w") as f:
        f.write(format_pair_tsv(pair_rows))
    with open(os.path.join(exp_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    with open(os.path.join(exp_dir, "warnings.json"), "w") as f:
        json.dump(warnings, f, indent=2)
        f.write("\n")

    # --- fill-rules + README --------------------------------------------
    write_readme(case_dir, num, slug, title, behavior, samples, pair_rows,
                 summary, warnings)
    return {
        "num": num, "slug": slug, "title": title, "n_samples": len(samples),
        "n_rows": len(pair_rows), "summary": summary, "warnings": warnings,
    }


def write_readme(case_dir, num, slug, title, behavior, samples, pair_rows,
                 summary, warnings):
    pairs = [(r["tumor_sample"], r["normal_sample"]) for r in pair_rows]
    pair_lines = "\n".join(
        f"  {i+1}. {t}" + (f"  x  {n}" if n else "  (unmatched — no normal)")
        for i, (t, n) in enumerate(pairs)) or "  (no rows — tumor-only frame is empty)"
    w_lines = "\n".join(
        f"  - {k}: {v if v else '(empty)'}" for k, v in warnings.items())
    body = f"""# Case {num}: {slug}

{title}

## Pinned reference behavior

{behavior}

## Inputs
- Samples ({len(samples)}):
""" + "".join(
        f"    - {s['sample']} ({s['type']}, pool={s['pool'] or '(empty)'}, "
        f"cov={s['coverage'] if s['coverage'] is not None else 'NA'}, "
        f"bam={s['bamname']}, matched={s['matched']}, "
        f"bam_present={s['present']})\n"
        for s in samples) + f"""
- Raw key file: `key_file.csv` (12 comma-separated fields, end-anchored:
  pool_id=sp[n-5], coverage=sp[n-4], matched_tag=sp[n-3]).
- BAM mirror listing: `bam_manifest.json` (drives the reference's bam_exists).
- Beagle-shaped payload: `payload.json` (the syzygy ingest input; 2/8 format).

## Expected reference output (15-column pair frame, reference row order)

{pair_lines}

Full frame: `expected/pair_rows.tsv` (columns: tumor_sample, patient,
tumor_idx, tumor_IM_ver, tumor_bamname, tumor_group, tumor_matched,
tumor_bam_exists, tumor_bam_coverage, normal_sample, normal_bamname,
normal_IM_ver, pool_id, normal_bam_exists, normal_bam_coverage).

## Summary counts (R lines 77-86)
  - patients: {summary['patients']}
  - tn_pairs: {summary['tn_pairs']}
  - unmatched_tumors: {summary['unmatched_tumors']}

## Warning buckets (R lines 96-165)
{w_lines}

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
"""
    with open(os.path.join(case_dir, "README.md"), "w") as f:
        f.write(body)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    results = []
    for num, slug, patients, title, behavior in CASES:
        r = build_case(num, slug, patients, title, behavior)
        results.append(r)
        print(f"[{num}] {slug:<40} samples={r['n_samples']:>2}  "
              f"rows={r['n_rows']:>2}  summary={r['summary']}")
    print(f"\nWrote {len(results)} cases to {OUT_DIR}")


if __name__ == "__main__":
    main()
