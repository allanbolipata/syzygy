#!/usr/bin/env python3
"""Import explicit tumor/normal pair assignments from a tab-delimited file.

Input file: first line is the header ``NORMAL_ID<TAB>TUMOR_ID``; every
following non-empty line carries the two cmoSampleNames. Example::

    NORMAL_ID<TAB>TUMOR_ID
    DEV-020-N<TAB>DEV-020-T

Each cmoSampleName is resolved to its sample UUID via
``GET /v1/sample/?cmo_sample_name=...`` (exact match enforced client-side —
the API filter is a case-insensitive substring), then the pair is pinned
under the pairing logic via ``POST /v1/pairing/trigger`` with a pinned
``normal_id`` (override semantics): the pair is created even when the
strategy's matching keys differ, and any key mismatches are recorded in the
pairing's ``mismatched_keys``. Re-running the same file is idempotent: a
tumor already pinned to the same normal keeps its pairing untouched.

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
row without creating anything).

Exit code 0 only when every row resolved and pinned cleanly; 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import httpx

EXPECTED_HEADER = ("NORMAL_ID", "TUMOR_ID")


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
) -> str:
    """Resolve a cmoSampleName to its sample UUID, enforcing exact match.

    ``tumor_or_normal`` is "TUMOR" or "NORMAL" and disambiguates names that
    appear on more than one row. Raises ValueError when the name matches
    zero or more than one sample of that side.
    """
    key = (cmo_sample_name, tumor_or_normal)
    if key in cache:
        return cache[key]

    resp = client.get(
        f"{base}/v1/sample/",
        params={
            "cmo_sample_name": cmo_sample_name,
            "tumor_or_normal": tumor_or_normal,
        },
    )
    resp.raise_for_status()
    exact = [
        s for s in resp.json()
        if s["cmo_sample_name"] == cmo_sample_name
        and s["tumor_or_normal"] == tumor_or_normal
    ]
    if not exact:
        raise ValueError(
            f"no {tumor_or_normal.lower()} sample with cmo_sample_name "
            f"{cmo_sample_name!r}"
        )
    if len(exact) > 1:
        ids = ", ".join(s["id"] for s in exact)
        raise ValueError(
            f"cmo_sample_name {cmo_sample_name!r} matches {len(exact)} "
            f"{tumor_or_normal.lower()} samples ({ids}); ambiguous"
        )
    cache[key] = exact[0]["id"]
    return cache[key]


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
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    if not args.token and not args.dry_run:
        parser.error("no API token: set --token or $SYZYGY_API_TOKEN")

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
    failed = 0

    with httpx.Client(headers=headers, timeout=30.0) as client:
        for normal_name, tumor_name in rows:
            label = f"{normal_name} -> {tumor_name}"
            try:
                normal_id = resolve_sample(client, base, normal_name, "NORMAL", cache)
                tumor_id = resolve_sample(client, base, tumor_name, "TUMOR", cache)
            except httpx.HTTPError as exc:
                print(f"FAIL  {label}: API error: {exc}", file=sys.stderr)
                failed += 1
                continue
            except ValueError as exc:
                print(f"FAIL  {label}: {exc}", file=sys.stderr)
                failed += 1
                continue

            if args.dry_run:
                print(f"OK    {label}: resolves (dry run)")
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
                failed += 1
                continue
            pairing = resp.json()
            keys = pairing.get("mismatched_keys")
            if keys:
                mismatched += 1
                print(f"WARN  {label}: paired under {pairing['pairing_logic']} "
                      f"with mismatched keys: {'; '.join(keys)}")
            else:
                pinned += 1
                print(f"OK    {label}: paired under {pairing['pairing_logic']}")

    total = len(rows)
    print(
        f"\n{args.logic}: {pinned} pinned, {mismatched} pinned-with-mismatch, "
        f"{failed} failed (of {total} rows)"
        + (" [dry run]" if args.dry_run else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
