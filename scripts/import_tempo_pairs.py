#!/usr/bin/env python3
"""Import explicit tumor/normal pair assignments from a tab-delimited file.

Input file: first line is the header ``NORMAL_ID<TAB>TUMOR_ID`` (historical)
or ``NORMAL_ID<TAB>TUMOR_ID<TAB>NORMAL_PRIMARY_ID<TAB>TUMOR_PRIMARY_ID``
(beagle's extended tempo pairing file); every following non-empty line
carries the ids. When the primary_id columns are present they take
precedence for resolution (stable registry ids); the cmoSampleName columns
remain the display labels. Example::

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

The parsing, resolution, candidate-normal summary and the per-row loop
live in ``syzygy/pairing_import.py`` (ticket AL3): the same code serves
``POST /v1/pairing/import`` in the app, so the CLI and the API cannot
diverge. This script keeps its CLI UX (flags, stdout/stderr layout,
exit codes) unchanged.

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

# The script may be run as `python scripts/import_tempo_pairs.py`, where
# sys.path[0] is scripts/ — make the repo root importable so the shared
# import core resolves.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from syzygy import pairing_import as _shared

#: Re-exported for importers/tests; the shared core is the source of truth.
EXPECTED_HEADER = _shared.EXPECTED_HEADER
EXTENDED_HEADER = _shared.EXTENDED_HEADER
#: Where the pairing log columns live; keep in sync with --log help.
LOG_HEADER = _shared.LOG_HEADER


def parse_pairs_file(path: str) -> list[tuple]:
    """Parse the TSV into (normal_name, tumor_name, normal_primary, tumor_primary) rows.

    The first non-empty line must be either the historical
    ``NORMAL_ID<TAB>TUMOR_ID`` header or the extended
    ``NORMAL_ID<TAB>TUMOR_ID<TAB>NORMAL_PRIMARY_ID<TAB>TUMOR_PRIMARY_ID``
    header emitted by beagle's tempo operator (case-insensitive). Blank
    lines are skipped; a row whose field count doesn't match its header or
    contains empty cells is a hard error (fail fast, don't guess). Rows
    from a historical 2-column file carry ``None`` in the primary slots.
    """
    with open(path, newline="") as fh:
        text = fh.read()
    return _shared.parse_pairs_text(text, source=path)


def resolve_sample(
    client: httpx.Client,
    base: str,
    cmo_sample_name: str,
    tumor_or_normal: str,
    cache: dict[tuple[str, str, str], str],
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
    return _shared.resolve_sample(
        _shared.HttpxTransport(client, base),
        cmo_sample_name,
        tumor_or_normal,
        cache,
        id_field,
    )


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
    return _shared.candidate_normal_summary(
        _shared.HttpxTransport(client, base), tumor_id, logic
    )


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
    for normal_name, tumor_name, _np, _tp in rows:
        if tumor_name in seen_tumors and seen_tumors[tumor_name] != normal_name:
            print(
                f"warning: tumor {tumor_name} appears twice with different "
                f"normals ({seen_tumors[tumor_name]} then {normal_name}); "
                f"the last row wins",
                file=sys.stderr,
            )
        seen_tumors[tumor_name] = normal_name

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    # The shared core runs the row loop; HttpxTransport is the transport
    # (the same GET /v1/sample/, possible-matches and POST trigger calls
    # the CLI always made).
    with httpx.Client(headers=headers, timeout=30.0) as client:
        result = _shared.run_import(
            rows,
            _shared.HttpxTransport(client, base),
            logic=args.logic,
            trigger_type=args.trigger_type,
            strict=args.strict,
            dry_run=args.dry_run,
            id_field=args.id_field,
        )

    for row in result.rows:
        if row.multi_line:
            print(row.multi_line)
        if row.to_stderr:
            print(row.line, file=sys.stderr)
        else:
            print(row.line)

    if args.log:
        with open(args.log, "w", newline="") as fh:
            fh.write(result.log_tsv())
        print(f"pairing log: {args.log} ({len(result.log_rows)} row(s))")

    print(result.summary_line(args.logic, args.dry_run))
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
