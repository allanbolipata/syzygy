#!/usr/bin/env python3
"""Cross-validate the reference-derivation harness against the ground-truth
test (tests/test_dmp_reference_conformance.py, committed in ade3f9b).

The test is the user's prior attempt at this fixture set. Its hand-pinned
EXPECTED_FRAME / EXPECTED_SUMMARY / EXPECTED_WARNINGS are
a solid INDEPENDENT check — but it was built against the SYZYGY code, and the
syzygy code diverges from the reference (pair_tumor_with_normal.R +
generate_dmp_tumor_normal_key.pl) in a handful of documented ways. So this
script separates the columns that MUST agree (the reliable core: pairing
structure, coverage, pool, bam_exists, and the three coverage/BAM warnings)
from the documented divergences (reported for transparency, not failures).

How it works:
  1. ast-parse the test and extract RECORDS (its input samples),
     MISSING_BAM, and the EXPECTED_* constants — WITHOUT executing the file
     (so its syzygy imports never run).
  2. Reconstruct the reference key-file + BAM manifest from RECORDS, adding
     the T/N suffix the reference needs to split tumors from normals (R 19-20).
  3. Run our harness (perl_parse + pair) on that input.
  4. Compare the harness output against EXPECTED_* on the reliable columns.

Documented divergences (reported, NOT failures):
  * tumor_bamname / normal_bamname (cols 4, 10) — sanctioned deviation; the
    reference wants a -T/-N-suffixed bamname, the syzygy test uses the bare
    beagle anon_id (and has a bamname bug of its own).
  * tumor_group (col 5) — the reference echoes the key file's group
    ("GRP-DMP"); the syzygy code emits "NA" (group is a gap in the DMP model).
  * tumor_matched (col 6) + the 3 summary counts + warnings 1 (matched with
    no normal) and 2 (ambiguous normals) — the reference uses the key file's
    matched_tag column VERBATIM, whereas the syzygy code DERIVES it (a tumor
    is "Matched" if it paired to a normal, "Unmatched" otherwise). On the
    test's data the key file marks only P-7000006 "Matched" (the
    matched-without-normal case), so the reference's summary is
    patients=1 / tn_pairs=1 / unmatched=13 while the syzygy code's is
    patients=11 / tn_pairs=13 / unmatched=1. Both are internally consistent
    with their own matched_tag rule.
  * patient for the unparseable name (P-7000011-T01) — the reference's name
    regex P-\\d+-\\w+-\\w+ needs four hyphen groups and yields "" on the three
    groups P-7000011-T01 carries; the syzygy code still extracts "P-7000011".
  * empty-vs-NA cells — the reference renders missing values as ""; the syzygy
    test renders "NA". We normalise "" == "NA" for the comparison.
  * row order — the reference echoes key-file order; the syzygy test pins a
    created-desc report order. We align rows by (tumor, normal) key.

Exit 0 = every RELIABLE column matches; exit 1 = at least one reliable
mismatch.
"""

import ast
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import derive_reference as ref  # noqa: E402

# The ground-truth test lives three levels up: .../_harness -> fixtures ->
# dmp_reference_conformance -> tests.
GROUND_TRUTH_TEST = HERE.parent.parent.parent / "test_dmp_reference_conformance.py"

# 15 reference pair-frame columns (see dmp_translator.DMP_REF_COLUMNS).
COLUMNS = [
    "tumor_sample", "patient", "tumor_idx", "tumor_IM_ver", "tumor_bamname",
    "tumor_group", "tumor_matched", "tumor_bam_exists", "tumor_bam_coverage",
    "normal_sample", "normal_bamname", "normal_IM_ver", "pool_id",
    "normal_bam_exists", "normal_bam_coverage",
]

# Columns that MUST agree between the harness (reference) and the syzygy
# test — the reliable core. The three matched_tag-driven columns (tumor_group,
# tumor_matched) and the two bamname columns are excluded (documented
# divergences); the patient cell for the unparseable sample is exempted via
# KNOWN_DIVERGENT_CELLS below.
RELIABLE_COLS = [
    COLUMNS.index(c) for c in (
        "tumor_sample", "patient", "tumor_idx", "tumor_IM_ver",
        "tumor_bam_exists", "tumor_bam_coverage", "normal_sample",
        "normal_IM_ver", "pool_id", "normal_bam_exists", "normal_bam_coverage",
    )
]
DIVERGENT_COLS = [
    COLUMNS.index(c) for c in ("tumor_bamname", "tumor_group", "tumor_matched",
                               "normal_bamname")
]
TUMOR_COL = COLUMNS.index("tumor_sample")
NORMAL_COL = COLUMNS.index("normal_sample")

# (tumor_sample, column) cells that are expected to differ even though they
# are in RELIABLE_COLS — the unparseable-name patient cell (see the docstring).
KNOWN_DIVERGENT_CELLS = {("P-7000011-T01", "patient")}

# Warning buckets that are independent of the matched_tag rule (reliable),
# versus those that depend on it (documented divergence).
RELIABLE_WARNINGS = ["samples_with_bams_missing", "low_coverage_tumors",
                     "low_coverage_normals"]
DIVERGENT_WARNINGS = ["matched_tumors_without_normal",
                      "tumor_samples_with_multiple_ambiguous_normals"]


def _extract_constants(path):
    """Pull top-level literal assignments out of a .py file without running it."""
    tree = ast.parse(path.read_text())
    wanted = {"RECORDS", "MISSING_BAM", "EXPECTED_FRAME", "EXPECTED_ROW_ORDER",
              "EXPECTED_SUMMARY", "EXPECTED_WARNINGS"}
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in wanted:
            out[node.targets[0].id] = ast.literal_eval(node.value)
    missing = wanted - set(out)
    if missing:
        raise SystemExit(f"could not extract {sorted(missing)} from {path}")
    return out


def _ref_bam(anon, ttype):
    """The reference splits tumors from normals off the bamname's trailing
    suffix (R line 19-20: bsuffix == 'T'/'N'). The untracked test feeds the
    bare beagle anon_id (AT01T) as its bamname, which carries no suffix and so
    yields zero tumors under the reference. We reconstruct the reference's
    expected T/N-suffixed bamname (AT01T -> AT01T-T) from the record's type.
    bamname is excluded from the column comparison anyway (sanctioned dev)."""
    suffix = "T" if ttype == "T" else "N"
    return f"{anon}-{suffix}"


def _key_rows_from_records(records):
    """Rebuild the raw key file from the untracked test's RECORDS tuples:
    (sample, ttype, anon_id, pool, coverage, matched_tag).

    End-anchored 12-field layout (see generate_dmp_tumor_normal_key.pl):
    sp[0]=sample, sp[1]=bamname, sp[2]=group, sp[3..6]=dummies,
    sp[7]=pool_id (n-5), sp[8]=coverage (n-4), sp[9]=matched_tag (n-3),
    sp[10..11]=trailing dummies. Each row is a LIST of fields (perl_parse
    comma-joins them itself).
    """
    rows = []
    for sample, ttype, anon, pool, coverage, matched in records:
        rows.append([
            sample,                                     # sp[0]
            _ref_bam(anon, ttype),                      # sp[1] bamname
            "GRP-DMP",                                  # sp[2] group
            "X", "X", "X", "X",                         # sp[3..6]
            "" if pool is None else pool,               # sp[7] pool_id
            "NA" if coverage is None else str(coverage),  # sp[8] coverage
            "" if matched is None else matched,         # sp[9] matched_tag
            "X", "X",                                   # sp[10..11]
        ])
    return rows


def _manifest_from_records(records, missing_bam):
    """The BAM mirror listing: every T/N-suffixed bamname except the
    missing-BAM sample's. The untracked test stubs bam_exists by SAMPLE name;
    the reference looks it up by BAMNAME, so we map the missing sample to its
    reference bamname first."""
    sample_to_bam = {r[0]: _ref_bam(r[2], r[1]) for r in records}
    missing_bams = {sample_to_bam[s] for s in missing_bam}
    all_bams = [_ref_bam(r[2], r[1]) for r in records]
    return [b for b in all_bams if b not in missing_bams]


def _norm(cell):
    """Empty-string and 'NA' are the same 'no value' state across the two
    systems; normalize so the comparison is about values, not spelling."""
    cell = cell if isinstance(cell, str) else str(cell)
    return "NA" if cell in ("", "NA") else cell


def _render(row):
    """Render a harness pair-row dict into the 15 reference cells (strings)."""
    return [ref.to_cell(row[c]) for c in COLUMNS]


def _key(row):
    return (row[TUMOR_COL], row[NORMAL_COL])


def main():
    consts = _extract_constants(GROUND_TRUTH_TEST)
    records = consts["RECORDS"]
    missing_bam = consts["MISSING_BAM"]
    expected_frame = consts["EXPECTED_FRAME"]
    expected_summary = consts["EXPECTED_SUMMARY"]
    expected_warnings = consts["EXPECTED_WARNINGS"]

    # Rebuild the reference input and run the harness on it.
    key_rows = _key_rows_from_records(records)
    manifest = _manifest_from_records(records, missing_bam)
    tabbed = ref.perl_parse(key_rows, manifest)
    pair_rows, summary, warnings = ref.pair(tabbed)
    rendered = [_render(r) for r in pair_rows]

    print(f"cross-validating harness vs {GROUND_TRUTH_TEST.name}")
    print(f"  input: {len(records)} samples, {len(manifest)} BAMs in mirror")
    print()
    print("RELIABLE checks (must match):")
    reliable_ok = True

    # 1. row count.
    ok = len(rendered) == len(expected_frame)
    reliable_ok &= ok
    print(f"  [{'PASS' if ok else 'FAIL'}] row count: "
          f"harness={len(rendered)} expected={len(expected_frame)}")

    # 2. pairing structure (multiset of (tumor, normal) keys).
    got_pairs = Counter(_key(r) for r in rendered)
    want_pairs = Counter(_key(r) for r in expected_frame)
    ok = got_pairs == want_pairs
    reliable_ok &= ok
    print(f"  [{'PASS' if ok else 'FAIL'}] pairing structure (tumor, normal) multiset")
    if not ok:
        print(f"         harness : {dict(sorted(got_pairs.items()))}")
        print(f"         expected: {dict(sorted(want_pairs.items()))}")

    # 3. per-row reliable cells, aligned by (tumor, normal).
    want_by_key = {_key(r): r for r in expected_frame}
    row_ok = True
    for r in rendered:
        key = _key(r)
        want = want_by_key.get(key)
        if want is None:
            print(f"  [FAIL] no expected row for harness row {key}")
            row_ok = False
            continue
        for i in RELIABLE_COLS:
            col = COLUMNS[i]
            if (key[0], col) in KNOWN_DIVERGENT_CELLS:
                continue
            if _norm(r[i]) != _norm(want[i]):
                print(f"  [FAIL] {key} {col}: harness {r[i]!r} != "
                      f"expected {want[i]!r}")
                row_ok = False
    reliable_ok &= row_ok
    if row_ok:
        print(f"  [PASS] per-row reliable cells ({len(RELIABLE_COLS)} of 15 "
              "cols; bamname/group/matched_tag excluded; ''=='NA' normalised; "
              "P-7000011 patient cell exempted)")

    # 4. warning buckets independent of the matched_tag rule (as sets).
    for bucket in RELIABLE_WARNINGS:
        ok = set(warnings[bucket]) == set(expected_warnings[bucket])
        reliable_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] warning: {bucket}")
        if not ok:
            print(f"         harness : {sorted(warnings[bucket])}")
            print(f"         expected: {sorted(expected_warnings[bucket])}")

    print()
    print("DOCUMENTED DIVERGENCES (expected to differ; reference vs syzygy):")
    print(f"  summary counts — reference uses key-file matched_tag verbatim, "
          f"syzygy derives it:")
    print(f"    harness (reference): {summary}")
    print(f"    expected (syzygy)  : {expected_summary}")
    for bucket in DIVERGENT_WARNINGS:
        print(f"  warning {bucket}:")
        print(f"    harness (reference): {sorted(warnings[bucket])}")
        print(f"    expected (syzygy)  : {sorted(expected_warnings[bucket])}")
    print(f"  tumor_group (col 5): harness 'GRP-DMP' (key-file group) vs "
          f"syzygy 'NA' (group is a gap in the DMP model)")
    print(f"  tumor_matched (col 6) + bamname cols (4, 10): matched_tag "
          f"derivation + sanctioned bamname deviation")
    print(f"  patient for P-7000011-T01 (unparseable): harness '' (R regex "
          f"needs 4 hyphen groups) vs syzygy 'P-7000011'")

    print()
    if reliable_ok:
        print("CROSS-VALIDATION PASSED: harness matches the ground-truth test "
              "on every reliable column; the divergences above are documented.")
        return 0
    print("CROSS-VALIDATION FAILED: at least one RELIABLE column mismatched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
