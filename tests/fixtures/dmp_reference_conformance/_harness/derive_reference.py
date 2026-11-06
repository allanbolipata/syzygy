"""Faithful Python port of the DMP tumor/normal reference pipeline.

Reference (the authority this port mirrors, line by line):
  1. generate_dmp_tumor_normal_key.pl  — parses the raw DMP key file into a
     12-column tab-separated key table (Perl stage).
  2. pair_tumor_with_normal.R          — two-tier pairing (pool_id primary,
     IM_ver rescue), the 15-column tumor-only pair frame, the normal_IM_ver
     backfill, the 3 summary counts, and the 5 QC warning buckets.

This module is a *derivation harness*: given the raw key-file rows and the
mirror BAM listing, it reproduces the reference output exactly. It is the tool
used to generate the pinned expectations under tests/fixtures/dmp_reference_conformance/.

Reference semantics reproduced here (with the R/Perl lines they come from):
  * Perl stage  : name/bamname regexes, `bam_exists` from the mirror, and the
                  end-anchored field reads pool_id = sp[n-5], coverage = sp[n-4],
                  matched_tag = sp[n-3] (raw key file has two trailing dummies).
  * Tier 1      : left join of tumors onto normals on (patient, pool_id). A
                  missing/empty pool_id never matches tier 1 (dplyr's NA never
                  matches; the spec and syzygy strategy both pin this).
  * Tier 2      : rescue left join of the tier-1-unmatched rows onto normals on
                  (patient, tumor_IM_ver == normal ver).
  * Backfill    : normal_IM_ver = ifelse(is.na(normal_IM_ver), tumor_IM_ver,
                  normal_IM_ver)  (R line 63-64).
  * Type quirk  : normal_bam_coverage is numeric after R line 34. The rescue
                  ifelse (R line 65-66) then mixes it with the *character*
                  rescued coverage, and R's ifelse adopts the type of the
                  NON-NA argument. So the final column is CHARACTER only when
                  the frame contains at least one rescue match (a non-NA
                  rescued coverage); a frame with no rescue match keeps the
                  column NUMERIC (verified against real R 4.6.2:
                  ifelse(FALSE, NA_character_, 100) -> numeric 100). Warning 5
                  therefore compares LEXICOGRAPHICALLY only in frames with a
                  rescue match ("100" < "50" is TRUE there) and NUMERICALLY
                  otherwise; warning 4 (tumor) is always numeric.
  * Row order   : tumor input (key-file) order, tier-1 fan-out then tier-2
                  fan-out consecutive per tumor. NOT syzygy's created-desc order.

Empty vs NA: an unparseable sample name yields empty ("") patient/idx/ver/
normal_IM_ver cells (not "NA"). Syzygy renders the stored-column gap as "NA";
that is a documented, expected divergence (spec §g), not a harness bug.
"""
from __future__ import annotations

import re
from collections import Counter

# The reference 15-column pair frame, in the R select() order (R lines 24-31).
REF_COLUMNS = [
    "tumor_sample",     # 1  sp[0] / sample
    "patient",          # 2  name-parsed (P-\\d+)
    "tumor_idx",        # 3  name-parsed
    "tumor_IM_ver",     # 4  name-parsed
    "tumor_bamname",    # 5  bamname (syzygy: Sample.anon_id stand-in)
    "tumor_group",      # 6  group (carried through untouched)
    "tumor_matched",    # 7  matched_tag (raw DMP-program flag)
    "tumor_bam_exists", # 8  "0"/"1" from the mirror listing
    "tumor_bam_coverage",  # 9  numeric (as.numeric)
    "normal_sample",    # 10 (NA when unmatched)
    "normal_bamname",   # 11 (NA when unmatched)
    "normal_IM_ver",    # 12 (backfilled with tumor_IM_ver when tier-1 NA)
    "pool_id",          # 13 the join key (tumor's pool)
    "normal_bam_exists",   # 14 "0"/"1" or NA
    "normal_bam_coverage", # 15 CHARACTER (see type quirk) or NA
]

# R line 17: key <- fread(..., colClasses = rep("character", 12)).
TAB_COLUMNS = [
    "sample", "patient", "idx", "ver", "bamname", "bprefix", "bsuffix",
    "group", "bam_exists", "coverage", "pool_id", "is_matched",
]

# Perl line 87: next if ($line !~ /^P-\d+/)
_LINE_RE = re.compile(r"^P-\d+")
# Perl line 89: ($sp[0] =~ /(P-\d+)-(\w+)-(\w+)/)
_NAME_RE = re.compile(r"(P-\d+)-(\w+)-(\w+)")
# Perl line 90: ($sp[1] =~ /(\w+)-(\w+)/)
_BAM_RE = re.compile(r"(\w+)-(\w+)")


def _to_num(value):
    """R as.numeric() on a string cell (R line 33-34).

    "" / "NA" / non-numeric -> NA (None); otherwise the float value.
    """
    if value is None or value == "" or value == "NA":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def perl_parse(raw_rows, bam_manifest):
    """Faithful port of generate_dmp_tumor_normal_key.pl lines 84-98.

    raw_rows     : list of raw key-file rows. Each row is a list of the
                   comma-separated fields (the generator already split them),
                   in the DMP key-file layout (see the spec §f / raw-key doc):
                     [sample, bamname, group, ..., pool_id, coverage,
                      matched_tag, <dummy>, <dummy>]
                   i.e. pool_id = sp[n-5], coverage = sp[n-4],
                   matched_tag = sp[n-3], with two trailing dummy fields.
    bam_manifest : iterable of bamnames present in the mirror (drives bam_exists).

    Returns a list of 12-column dicts (the tabbed key table), one per accepted
    sample (rows not starting with P-\\d+ are skipped, as in the Perl).
    """
    manifest = set(bam_manifest)
    tabbed = []
    for row in raw_rows:
        line = ",".join(row)
        # line 87: next if ($line !~ /^P-\d+/)
        if not _LINE_RE.match(line):
            continue
        # line 88: my @sp = split(/\t/, $line)  (we already hold the fields)
        sp = list(row)
        # line 89: name parse (patient, idx, ver); unparseable -> ""
        m = _NAME_RE.search(sp[0])
        patient, idx, ver = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
        # line 90: bamname parse (bprefix, bsuffix); no dash -> "" / ""
        mb = _BAM_RE.search(sp[1])
        bprefix, bsuffix = (mb.group(1), mb.group(2)) if mb else ("", "")
        # lines 91-92: bam_exists = 1 if bamname in mirror, else 0
        bam_exists = "1" if sp[1] in manifest else "0"
        # lines 94-96: end-anchored reads (raw key file has two trailing dummies)
        n = len(sp)
        pool_id = sp[n - 5]
        coverage = sp[n - 4]
        matched_tag = sp[n - 3]
        # line 97: print the 12 tab-separated columns
        tabbed.append({
            "sample": sp[0], "patient": patient, "idx": idx, "ver": ver,
            "bamname": sp[1], "bprefix": bprefix, "bsuffix": bsuffix,
            "group": sp[2], "bam_exists": bam_exists, "coverage": coverage,
            "pool_id": pool_id, "is_matched": matched_tag,
        })
    return tabbed


def _pair_row(t, n):
    """Build a pre-rescue pair-frame row from a tumor dict `t` and a normal
    dict `n` (or None when tier-1 found no match). R lines 20-31."""
    row = {
        "tumor_sample": t["sample"],
        "patient": t["patient"],
        "tumor_idx": t["idx"],
        "tumor_IM_ver": t["ver"],
        "tumor_bamname": t["bamname"],
        "tumor_group": t["group"],
        "tumor_matched": t["is_matched"],
        "tumor_bam_exists": t["bam_exists"],
        "tumor_bam_coverage": t["coverage"],      # string; cast below
        "normal_sample": n["sample"] if n else None,
        "normal_bamname": n["bamname"] if n else None,
        "normal_IM_ver": n["ver"] if n else None,
        "pool_id": t["pool_id"],                  # the join key (tumor's pool)
        "normal_bam_exists": n["bam_exists"] if n else None,
        "normal_bam_coverage": n["coverage"] if n else None,  # string; cast below
    }
    # R lines 33-34: mutate(tumor_bam_coverage = as.numeric(coverage.x),
    #                       normal_bam_coverage = as.numeric(coverage.y))
    row["tumor_bam_coverage"] = _to_num(row["tumor_bam_coverage"])
    row["normal_bam_coverage"] = _to_num(row["normal_bam_coverage"])
    return row


def _rescue_row(row, n):
    """A row of `samples_without_normal` (R lines 36-48): the tier-1-unmatched
    tumor joined onto a rescued normal (or None when the rescue finds nothing).
    The R script renames the normal columns with a `rescued_` prefix; we keep
    that naming so the backfill step mirrors the R ifelse one-for-one.

    `row` is a tier-1-unmatched pair-frame row (key `tumor_sample`); `n` is a
    tabbed normal dict (or None when the rescue found nothing)."""
    return {
        "tumor_sample": row["tumor_sample"],
        "rescued_normal_sample": n["sample"] if n else None,
        "rescued_normal_bamname": n["bamname"] if n else None,
        "rescued_normal_bam_exists": n["bam_exists"] if n else None,
        # NOTE: left as the raw CHARACTER (the R select does not cast it to
        # numeric) — this is what makes the final normal_bam_coverage column
        # character-typed (see module docstring, "Type quirk").
        "rescued_normal_bam_coverage": n["coverage"] if n else None,
    }


def _backfill(row, rr):
    """Apply the R lines 57-66 ifelse backfill to one pair-frame row.

    `rr` is the matched rescue row (a dict with `rescued_*` keys) or None for a
    tier-1-matched row (which has no rescue row). Returns the final 15-column
    row as a REF_COLUMNS dict.
    """
    # normal_sample / normal_bamname / normal_bam_exists:
    #   ifelse(is.na(x), rescued_x, x)
    if row["normal_sample"] is not None:
        normal_sample = row["normal_sample"]
    elif rr is not None and rr["rescued_normal_sample"] is not None:
        normal_sample = rr["rescued_normal_sample"]
    else:
        normal_sample = None

    if row["normal_bamname"] is not None:
        normal_bamname = row["normal_bamname"]
    elif rr is not None and rr["rescued_normal_bamname"] is not None:
        normal_bamname = rr["rescued_normal_bamname"]
    else:
        normal_bamname = None

    if row["normal_bam_exists"] is not None:
        normal_bam_exists = row["normal_bam_exists"]
    elif rr is not None and rr["rescued_normal_bam_exists"] is not None:
        normal_bam_exists = rr["rescued_normal_bam_exists"]
    else:
        normal_bam_exists = None

    # R line 63-64: normal_IM_ver = ifelse(is.na(normal_IM_ver), tumor_IM_ver,
    #                                       normal_IM_ver)   <-- THE BACKFILL
    if row["normal_IM_ver"] is not None:
        normal_im_ver = row["normal_IM_ver"]
    else:
        normal_im_ver = row["tumor_IM_ver"]

    # R line 65-66: normal_bam_coverage = ifelse(is.na(normal_bam_coverage),
    #   rescued_normal_bam_coverage, normal_bam_coverage).
    # The ifelse adopts the type of the NON-NA argument (module docstring,
    # "Type quirk"): the column is CHARACTER iff the frame has >=1 rescue
    # match, NUMERIC otherwise. Either way the cell is rendered here as a
    # string (as.character semantics: 588.0 -> '588'); the warning stage
    # re-parses it for the numeric compare when char_cov is False.
    if row["normal_bam_coverage"] is not None:
        normal_bam_coverage = _num_str(row["normal_bam_coverage"])
    elif rr is not None and rr["rescued_normal_bam_coverage"] is not None:
        normal_bam_coverage = rr["rescued_normal_bam_coverage"]
    else:
        normal_bam_coverage = None

    return {
        "tumor_sample": row["tumor_sample"],
        "patient": row["patient"],
        "tumor_idx": row["tumor_idx"],
        "tumor_IM_ver": row["tumor_IM_ver"],
        "tumor_bamname": row["tumor_bamname"],
        "tumor_group": row["tumor_group"],
        "tumor_matched": row["tumor_matched"],
        "tumor_bam_exists": row["tumor_bam_exists"],
        "tumor_bam_coverage": row["tumor_bam_coverage"],   # numeric or None
        "normal_sample": normal_sample,
        "normal_bamname": normal_bamname,
        "normal_IM_ver": normal_im_ver,
        "pool_id": row["pool_id"],
        "normal_bam_exists": normal_bam_exists,
        "normal_bam_coverage": normal_bam_coverage,         # char or None
    }


def _unique(rows):
    """dplyr::unique() — collapse fully-duplicate rows, preserving order
    (R line 48)."""
    seen = set()
    out = []
    for r in rows:
        key = tuple(str(r.get(c)) for c in sorted(r))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def pair(tabbed):
    """Run the R pairing stage on the 12-column tabbed key table.

    Returns (pair_rows, summary, warnings) where:
      pair_rows : list of 15-column dicts in reference row order
      summary   : {"patients", "tn_pairs", "unmatched_tumors"}
      warnings  : the 5 QC buckets, each a deduped list of sample names
    """
    tumors = [r for r in tabbed if r["bsuffix"] == "T"]   # R line 19
    normals = [r for r in tabbed if r["bsuffix"] == "N"]  # R line 20

    # ---- Tier 1: left join on (patient, pool_id) --------------------------
    tumor_normal = []
    for t in tumors:
        matches = []
        if t["pool_id"] != "":  # a missing pool never matches tier 1
            for n in normals:
                if (n["patient"] == t["patient"] and n["pool_id"] != ""
                        and n["pool_id"] == t["pool_id"]):
                    matches.append(n)
        if not matches:
            tumor_normal.append(_pair_row(t, None))
        else:
            for n in matches:  # fan-out, normal input order
                tumor_normal.append(_pair_row(t, n))
    # R line 23: filter(!is.na(bsuffix.x)) — no-op (bsuffix.x is always "T")

    # ---- Tier 2: rescue on (patient, tumor_IM_ver == normal ver) ---------
    samples_without_normal = []
    for row in tumor_normal:
        if row["normal_bamname"] is not None:
            continue  # tier-1 matched; not eligible for rescue
        rescue = [n for n in normals
                  if n["patient"] == row["patient"] and n["ver"] == row["tumor_IM_ver"]]
        if not rescue:
            samples_without_normal.append(_rescue_row(row, None))
        else:
            for n in rescue:  # fan-out, normal input order
                samples_without_normal.append(_rescue_row(row, n))
    samples_without_normal = _unique(samples_without_normal)  # R line 48

    # ---- Join back by tumor_sample + ifelse backfill (R lines 50-68) -----
    final = []
    for row in tumor_normal:
        if row["normal_bamname"] is not None:
            final.append(_backfill(row, None))  # tier-1 matched: 1 row
        else:
            rescue_rows = [r for r in samples_without_normal
                           if r["tumor_sample"] == row["tumor_sample"]]
            for rr in rescue_rows:  # 1 row if no rescue, else K rescue rows
                final.append(_backfill(row, rr))

    # R's ifelse (line 65-66) adopts the type of the NON-NA argument: the
    # normal_bam_coverage column is CHARACTER iff some row of the rescue table
    # carries a non-NA rescued coverage, i.e. the frame has >=1 rescue match.
    char_cov = any(r["rescued_normal_bam_coverage"] is not None
                   for r in samples_without_normal)

    return final, _summary(final), _warnings(final, char_cov)


def _summary(final):
    """R lines 77-86: the 3 summary counts (patients / tn_pairs /
    unmatched_tumors) over the full 15-column frame."""
    matched = [r for r in final if r["tumor_matched"] == "Matched"]
    unmatched = [r for r in final if r["tumor_matched"] != "Matched"]
    # distinct patient values among matched rows (R: dim(unique(patient))[1])
    patients = len(set(r["patient"] for r in matched))
    return {
        "patients": patients,
        "tn_pairs": len(matched),
        "unmatched_tumors": len(unmatched),
    }


def _dedup(items):
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _warnings(final, char_cov):
    """R lines 96-165: the 5 QC warning buckets.

    Each bucket is a deduped list of sample names (matching the syzygy report's
    5/8 format): bucket 1/2/4 -> tumor_sample, bucket 5 -> normal_sample,
    bucket 3 -> tumor_sample then normal_sample for each flagged row.

    char_cov: whether a rescue match promoted normal_bam_coverage to
    CHARACTER (R ifelse takes the non-NA argument's type; see module
    docstring "Type quirk"). Warning 5's `< 50` follows that column type.
    """
    # --- Warning 1: matched tumors with no normal (R lines 98-110) --------
    w1 = _dedup([r["tumor_sample"] for r in final
                 if r["tumor_matched"] == "Matched" and r["normal_bamname"] is None])

    # --- Warning 2: tumors with >1 ambiguous normal (R lines 112-135) -----
    matched = [r for r in final if r["tumor_matched"] == "Matched"]
    bam_counts = Counter(r["tumor_bamname"] for r in matched)
    ambiguous_bams = {b for b, c in bam_counts.items() if c > 1}
    w2 = _dedup([r["tumor_sample"] for r in final
                 if r["tumor_bamname"] in ambiguous_bams])

    # --- Warning 3: samples with a missing BAM (R lines 137-148) ----------
    w3 = []
    for r in final:
        # normal_bam_exists==0 | tumor_bam_exists==0  (NA compares false)
        if r["normal_bam_exists"] == "0" or r["tumor_bam_exists"] == "0":
            w3.append(r["tumor_sample"])
            if r["normal_sample"] is not None:
                w3.append(r["normal_sample"])
    w3 = _dedup(w3)

    # --- Warning 4: low-coverage TUMORS (R lines 150-156) -----------------
    # NUMERIC compare: (is.na(bam) & !is.na(coverage)) | (coverage < 100)
    def _tumor_low(r):
        if r["tumor_bamname"] is not None and r["tumor_bam_coverage"] is None:
            return True
        if r["tumor_bam_coverage"] is not None and r["tumor_bam_coverage"] < 100:
            return True
        return False
    w4 = _dedup([r["tumor_sample"] for r in final if _tumor_low(r)])

    # --- Warning 5: low-coverage NORMALS (R lines 158-165) ----------------
    # The `< 50` compare follows the column type (module docstring "Type
    # quirk"): LEXICOGRAPHIC when a rescue match promoted normal_bam_coverage
    # to character ("100" < "50" is TRUE — the case-13 quirk), NUMERIC when
    # the frame has no rescue match (then 9 < 50 is TRUE, unlike
    # lexicographic "9" < "50" which is FALSE).
    def _normal_low(r):
        nb, nc = r["normal_bamname"], r["normal_bam_coverage"]
        if nb is not None and nc is None:
            return True
        if nc is not None:
            if char_cov:
                return nc < "50"          # character column: lexicographic
            return float(nc) < 50         # numeric column (no rescue match)
        return False
    w5 = _dedup([r["normal_sample"] for r in final
                 if _normal_low(r) and r["normal_sample"] is not None])

    return {
        "matched_tumors_without_normal": w1,
        "tumor_samples_with_multiple_ambiguous_normals": w2,
        "samples_with_bams_missing": w3,
        "low_coverage_tumors": w4,
        "low_coverage_normals": w5,
    }


# --- cell / TSV rendering (R write.table semantics) ------------------------

def _num_str(value):
    """R as.character()/write.table formatting of a non-NA numeric: a
    whole-number double drops the decimal point (588.0 -> '588'); a fractional
    double keeps it (588.5 -> '588.5'). This mirrors what the reference
    write.table emits, so the pinned cells match the R output byte-for-byte."""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def to_cell(value):
    """Render one cell the way R write.table would:
    NA -> "NA"; a whole-number numeric -> no decimal point; strings as-is.
    """
    if value is None:
        return "NA"
    if isinstance(value, float):
        return _num_str(value)
    return str(value)


def format_pair_tsv(pair_rows):
    """The 15-column pair file exactly as the reference writes it (R line 70)."""
    lines = ["\t".join(REF_COLUMNS)]
    for row in pair_rows:
        lines.append("\t".join(to_cell(row[c]) for c in REF_COLUMNS))
    return "\n".join(lines) + "\n"
