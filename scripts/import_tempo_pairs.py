#!/usr/bin/env python3
"""Import explicit tumor/normal pair assignments from a tab-delimited file.

Input file: first line is the header ``NORMAL_ID<TAB>TUMOR_ID``; every
following non-empty line carries the two cmoSampleNames. Example::

    NORMAL_ID<TAB>TUMOR_ID
    DEV-020-N<TAB>DEV-020-T

Each id is resolved to its sample UUID via ``GET /v1/sample/`` filtered by
the lookup column (``--id-field``: ``cmo_sample_name`` default, or
``primary_id`` — the stable registry id), then the pair is pinned
under the pairing logic via ``POST /v1/pairing/trigger`` with a pinned
``normal_id`` (override semantics): the pair is created even when the
strategy's matching keys differ, and any key mismatches are recorded in the
pairing's ``mismatched_keys``. Re-running the same file is idempotent: a
tumor already pinned to the same normal keeps its pairing untouched.

When a tumor has more than one candidate normal that passes the strategy's
matching keys, the curated pair from the file is still pinned, and the
multiplicity is surfaced as a ``MULTI`` line (and in the ``--log`` file)
listing the candidate normals and which one the cascade would have picked.

The file's NORMAL_ID/TUMOR_ID columns are cmoSampleNames; the API's
normal_id/tumor_id fields are sample UUIDs — this script is the mapping
layer between the two.

Usage (needs the app running and an API token):

    export SYZYGY_API_TOKEN=***        # dev: DEV_AUTH_TOKEN from .env
    python scripts/import_tempo_pairs.py pairs.tsv --dry-run
    python scripts/import_tempo_pairs.py pairs.tsv

Options: ``--base-url`` (default http://localhost:8123), ``--logic``
(default TEMPO; any non-DMP logic works), ``--trigger-type``
(override|manual, default override), ``--strict`` (reject key-mismatched
pairs instead of recording them), ``--dry-run`` (resolve and validate every
row without creating anything), ``--log PATH`` (write a TSV pairing log of
every row that failed to match cleanly), ``--check-beagle PATH`` (before
importing, verify beagle's ``tempo_sample._map_run_modes`` in the given
beagle checkout still agrees with this repo's ``TEMPO_RUN_MODE_MAP``;
drift aborts with exit 2).

Exit code 0 only when every row resolved and pinned cleanly; 1 otherwise;
2 when ``--check-beagle`` found drift (nothing was imported).
"""

from __future__ import annotations

import argparse
import ast
import glob
import os
import sys
from typing import Optional

import httpx

EXPECTED_HEADER = ("NORMAL_ID", "TUMOR_ID")

#: Where the pairing log columns live; keep in sync with --log help.
LOG_HEADER = ("status", "NORMAL_ID", "TUMOR_ID", "detail")


def parse_pairs_file(path: str) -> list[tuple[str, str]]:
    """Parse the TSV into (normal_name, tumor_name) rows.

    The first non-empty line must be the NORMAL_ID/TUMOR_ID header
    (case-insensitive). Blank lines are skipped; a row without exactly two
    tab-separated fields is a hard error (fail fast, don't guess).
    """
    rows: list[tuple[str, str]] = []
    with open(path, newline="") as fh:
        lines = [ln.rstrip("\r\n") for ln in fh]

    content = [ln for ln in lines if ln.strip()]
    if not content:
        raise ValueError(f"{path}: file is empty")

    header = [cell.strip() for cell in content[0].split("\t")]
    if tuple(h.upper() for h in header) != EXPECTED_HEADER:
        raise ValueError(
            f"{path}: expected header 'NORMAL_ID<TAB>TUMOR_ID', "
            f"got {content[0]!r}"
        )

    for lineno, line in enumerate(content[1:], start=2):
        cells = [cell.strip() for cell in line.split("\t")]
        if len(cells) != 2 or not all(cells):
            raise ValueError(
                f"{path}: line {lineno}: expected two tab-separated "
                f"non-empty fields, got {line!r}"
            )
        rows.append((cells[0], cells[1]))
    return rows


def resolve_sample(
    client: httpx.Client,
    base: str,
    cmo_sample_name: str,
    tumor_or_normal: str,
    cache: dict[tuple[str, str], str],
    id_field: str = "cmo_sample_name",
) -> str:
    """Resolve an external sample id to its sample UUID, enforcing exact match.

    ``id_field`` selects the lookup hint column (``cmo_sample_name`` or
    ``primary_id`` — the latter is the stable id from the sample registry).
    The API filter is a case-insensitive substring on either column, so the
    exact match is enforced client-side. ``tumor_or_normal`` is "TUMOR" or
    "NORMAL" and disambiguates names that appear on more than one row.
    Raises ValueError when the id matches zero or more than one sample of
    that side.
    """
    key = (cmo_sample_name, tumor_or_normal)
    if key in cache:
        return cache[key]

    resp = client.get(
        f"{base}/v1/sample/",
        params={
            id_field: cmo_sample_name,
            "tumor_or_normal": tumor_or_normal,
        },
    )
    resp.raise_for_status()
    exact = [
        s for s in resp.json()
        if s[id_field] == cmo_sample_name
        and s["tumor_or_normal"] == tumor_or_normal
    ]
    if not exact:
        raise ValueError(
            f"no {tumor_or_normal.lower()} sample with {id_field} "
            f"{cmo_sample_name!r}"
        )
    if len(exact) > 1:
        ids = ", ".join(s["id"] for s in exact)
        raise ValueError(
            f"{id_field} {cmo_sample_name!r} matches {len(exact)} "
            f"{tumor_or_normal.lower()} samples ({ids}); ambiguous"
        )
    cache[key] = exact[0]["id"]
    return cache[key]


def candidate_normal_summary(
    client: "httpx.Client", base: str, tumor_id: str, logic: str
) -> tuple[int, list[str], Optional[str]]:
    """Summarize a tumor's candidate normals via possible-matches.

    Returns ``(matched_count, matched_names, cascade_pick)``: how many
    candidate normals pass every matching key of the strategy (the
    currently-paired normal counts too when it carries no
    ``mismatched_keys``), their display names (page-bounded), and which
    candidate the strategy's cascade would pick. Informational only —
    a curated pair from the pairing file is pinned regardless; callers
    must not let a failure here block the import.
    """
    resp = client.get(
        f"{base}/v1/sample/{tumor_id}/possible-matches",
        params={"pairing_logic_name": logic},
    )
    resp.raise_for_status()
    data = resp.json()
    names: list[str] = []
    cascade_pick: Optional[str] = None
    count = int(data.get("other_matches_matched", 0))
    current = data.get("current_match") or {}
    if current and not current.get("mismatched_keys"):
        count += 1
        names.append(current["sample"]["cmo_sample_name"])
    for cand in data.get("other_matches", []):
        if cand.get("matched"):
            names.append(cand["sample"]["cmo_sample_name"])
            if cand.get("would_be_selected"):
                cascade_pick = cand["sample"]["cmo_sample_name"]
    return count, names, cascade_pick


def extract_beagle_run_mode_rules(path: str) -> dict[str, str]:
    """Extract the {substring: bucket} rules from a beagle tempo_sample.py.

    AST-parses ``_map_run_modes`` and collects every
    ``if/elif "<sub>" in <expr>.lower(): ... .add("<Bucket>")`` rule. The
    else-branch (raw pass-through) is implicit and not represented.
    """
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "_map_run_modes"):
            continue
        rules: dict[str, str] = {}
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # pattern: "<sub>" in something.lower()
            if not (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.In)
                and isinstance(test.left, ast.Constant)
                and isinstance(test.left.value, str)
                and isinstance(test.comparators[0], ast.Call)
                and isinstance(test.comparators[0].func, ast.Attribute)
                and test.comparators[0].func.attr == "lower"
            ):
                continue
            bucket = None
            for stmt in node.body:
                call = getattr(stmt, "value", None)
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    bucket = call.args[0].value
            if bucket is not None:
                rules[test.left.value.lower()] = bucket
        return rules
    raise ValueError(f"{path}: no _map_run_modes function found")


def check_run_mode_alignment(beagle_root: str) -> tuple[bool, list[str]]:
    """Compare beagle's TEMPO run-mode rules with TEMPO_RUN_MODE_MAP.

    Discovers every ``runner/operator/*/bin/tempo_sample.py`` under
    ``beagle_root`` (TEMPO_MPGEN + CHRONOS operators), extracts each
    ``_map_run_modes`` rule set, and checks (a) the operators agree with
    each other and (b) they agree with this repo's ``TEMPO_RUN_MODE_MAP``
    (case-insensitively — syzygy buckets are lower-cased). Returns
    ``(ok, report_lines)``.
    """
    # The script may be run as `python scripts/import_tempo_pairs.py`,
    # where sys.path[0] is scripts/ — make the repo root importable.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from syzygy.pairing_strategies import TEMPO_RUN_MODE_MAP

    pattern = os.path.join(beagle_root, "runner", "operator", "*", "bin", "tempo_sample.py")
    paths = sorted(glob.glob(pattern))
    if not paths:
        return False, [f"no tempo_sample.py found under {pattern}"]

    ok = True
    lines: list[str] = []
    syz = {k.lower(): str(v).lower() for k, v in TEMPO_RUN_MODE_MAP.items()}
    extracted: dict[str, dict[str, str]] = {}
    for path in paths:
        try:
            extracted[path] = extract_beagle_run_mode_rules(path)
        except (ValueError, SyntaxError) as exc:
            ok = False
            lines.append(f"DRIFT {path}: {exc}")

    # (a) cross-operator agreement
    distinct = {p: r for p, r in extracted.items()}
    canonical = None
    for path, rules in distinct.items():
        norm = {k: str(v).lower() for k, v in rules.items()}
        if canonical is None:
            canonical = (path, norm)
        elif norm != canonical[1]:
            ok = False
            lines.append(
                f"DRIFT operators disagree: {os.path.relpath(path, beagle_root)} "
                f"!= {os.path.relpath(canonical[0], beagle_root)}"
            )

    # (b) agreement with syzygy's TEMPO_RUN_MODE_MAP
    for path, rules in extracted.items():
        bea = {k: str(v).lower() for k, v in rules.items()}
        rel = os.path.relpath(path, beagle_root)
        problems: list[str] = []
        for sub in sorted(set(bea) - set(syz)):
            problems.append(f"beagle maps run-mode substring {sub!r} -> "
                            f"{bea[sub]!r}, TEMPO_RUN_MODE_MAP does not")
        for sub in sorted(set(syz) - set(bea)):
            problems.append(f"TEMPO_RUN_MODE_MAP maps {sub!r} -> {syz[sub]!r}, "
                            f"beagle _map_run_modes does not")
        for sub in sorted(set(bea) & set(syz)):
            if bea[sub] != syz[sub]:
                problems.append(f"bucket differs for {sub!r}: beagle "
                                f"{bea[sub]!r} vs TEMPO_RUN_MODE_MAP {syz[sub]!r}")
        if problems:
            ok = False
            for p in problems:
                lines.append(f"DRIFT {rel}: {p}")
        else:
            lines.append(f"OK    {rel}: matches TEMPO_RUN_MODE_MAP "
                         f"({', '.join(sorted(bea))})")
    return ok, lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import tumor/normal pair assignments from a "
                    "tab-delimited NORMAL_ID/TUMOR_ID file."
    )
    parser.add_argument("pairs_file", help="TSV with NORMAL_ID<TAB>TUMOR_ID")
    parser.add_argument(
        "--base-url", default=os.environ.get("SYZYGY_BASE_URL", "http://localhost:8123"),
        help="app base URL (default %(default)s or $SYZYGY_BASE_URL)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("SYZYGY_API_TOKEN"),
        help="API bearer token (default $SYZYGY_API_TOKEN; dev: DEV_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--id-field", choices=("cmo_sample_name", "primary_id"),
        default="cmo_sample_name",
        help="column the file's ids resolve against (default %(default)s; "
             "primary_id is the stable registry id)",
    )
    parser.add_argument(
        "--logic", default="TEMPO",
        help="pairing logic name (default %(default)s)",
    )
    parser.add_argument(
        "--trigger-type", choices=("override", "manual"), default="override",
        help="recorded trigger_type on new pairings (default %(default)s)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="reject pairs whose matching keys differ instead of recording "
             "them with mismatched_keys",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and validate every row without creating pairings",
    )
    parser.add_argument(
        "--log", metavar="PATH",
        help="write a TSV pairing log (status<TAB>NORMAL_ID<TAB>TUMOR_ID"
             "<TAB>detail) of every row that failed to match cleanly",
    )
    parser.add_argument(
        "--check-beagle", metavar="PATH",
        help="beagle repo root: verify its tempo_sample._map_run_modes "
             "still matches TEMPO_RUN_MODE_MAP before importing; drift "
             "aborts with exit 2",
    )
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    if not args.token and not args.dry_run:
        parser.error("no API token: set --token or $SYZYGY_API_TOKEN")

    if args.check_beagle:
        try:
            aligned, report = check_run_mode_alignment(args.check_beagle)
        except ImportError as exc:
            print(f"error: --check-beagle needs the syzygy package "
                  f"(run from the repo root with the project venv): {exc}",
                  file=sys.stderr)
            return 2
        for line in report:
            print(line)
        if not aligned:
            print("error: TEMPO run-mode drift — fix TEMPO_RUN_MODE_MAP "
                  "(or the import) before assigning pairs", file=sys.stderr)
            return 2

    try:
        rows = parse_pairs_file(args.pairs_file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("error: no data rows below the header", file=sys.stderr)
        return 1

    seen_tumors: dict[str, str] = {}
    for normal_name, tumor_name in rows:
        if tumor_name in seen_tumors and seen_tumors[tumor_name] != normal_name:
            print(
                f"warning: tumor {tumor_name} appears twice with different "
                f"normals ({seen_tumors[tumor_name]} then {normal_name}); "
                f"the last row wins",
                file=sys.stderr,
            )
        seen_tumors[tumor_name] = normal_name

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    cache: dict[tuple[str, str], str] = {}
    pinned = 0
    mismatched = 0
    multi = 0
    failed = 0
    log_rows: list[tuple[str, str, str, str]] = []

    with httpx.Client(headers=headers, timeout=30.0) as client:
        for normal_name, tumor_name in rows:
            label = f"{normal_name} -> {tumor_name}"
            try:
                normal_id = resolve_sample(
                    client, base, normal_name, "NORMAL", cache, args.id_field)
                tumor_id = resolve_sample(
                    client, base, tumor_name, "TUMOR", cache, args.id_field)
            except httpx.HTTPError as exc:
                print(f"FAIL  {label}: API error: {exc}", file=sys.stderr)
                log_rows.append(("FAIL", normal_name, tumor_name, f"API error: {exc}"))
                failed += 1
                continue
            except ValueError as exc:
                print(f"FAIL  {label}: {exc}", file=sys.stderr)
                log_rows.append(("FAIL", normal_name, tumor_name, str(exc)))
                failed += 1
                continue

            multi_note = None
            try:
                n_matched, names, pick = candidate_normal_summary(
                    client, base, tumor_id, args.logic)
                if n_matched > 1:
                    if len(names) < n_matched:
                        shown = f"{', '.join(names)} (+{n_matched - len(names)} more)"
                    else:
                        shown = ", ".join(names)
                    extra = f"; cascade would pick {pick}" if pick else ""
                    multi_note = (f"tumor has {n_matched} matching candidate "
                                  f"normals ({shown}{extra}); pinning curated "
                                  f"{normal_name}")
                    print(f"MULTI {label}: {multi_note}")
                    multi += 1
            except httpx.HTTPError:
                pass  # informational only — never block the curated pin

            if args.dry_run:
                note = f" — {multi_note}" if multi_note else ""
                print(f"OK    {label}: resolves (dry run){note}")
                if multi_note:
                    log_rows.append(("MULTI", normal_name, tumor_name, multi_note))
                pinned += 1
                continue

            resp = client.post(
                f"{base}/v1/pairing/trigger",
                json={
                    "tumor_id": tumor_id,
                    "normal_id": normal_id,
                    "pairing_logic_name": args.logic,
                    "trigger_type": args.trigger_type,
                    "strict": args.strict,
                },
            )
            if resp.status_code >= 400:
                detail = resp.text[:300]
                print(f"FAIL  {label}: HTTP {resp.status_code}: {detail}",
                      file=sys.stderr)
                log_rows.append(("FAIL", normal_name, tumor_name,
                                 f"HTTP {resp.status_code}: {detail}"))
                failed += 1
                continue
            pairing = resp.json()
            keys = pairing.get("mismatched_keys")
            if keys:
                mismatched += 1
                detail = "; ".join(keys)
                if multi_note:
                    detail = f"{detail} | {multi_note}"
                print(f"WARN  {label}: paired under {pairing['pairing_logic']} "
                      f"with mismatched keys: {detail}")
                log_rows.append(("MISMATCH", normal_name, tumor_name, detail))
            elif multi_note:
                print(f"OK    {label}: paired under {pairing['pairing_logic']} "
                      f"— {multi_note}")
                log_rows.append(("MULTI", normal_name, tumor_name, multi_note))
                pinned += 1
            else:
                pinned += 1
                print(f"OK    {label}: paired under {pairing['pairing_logic']}")

    if args.log:
        def _flat(text: str) -> str:
            # keep the log strictly one row per line, tab-delimited
            return " ".join(text.split())
        with open(args.log, "w", newline="") as fh:
            fh.write("\t".join(LOG_HEADER) + "\n")
            for status, n, t, detail in log_rows:
                fh.write("\t".join((status, n, t, _flat(detail))) + "\n")
        print(f"pairing log: {args.log} ({len(log_rows)} row(s))")

    total = len(rows)
    print(
        f"\n{args.logic}: {pinned} pinned, {mismatched} pinned-with-mismatch, "
        f"{multi} with multiple candidate normals, {failed} failed "
        f"(of {total} rows)"
        + (" [dry run]" if args.dry_run else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
