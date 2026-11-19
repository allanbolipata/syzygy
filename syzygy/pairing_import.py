"""Shared TEMPO pair-assignment import core (ticket AL3, decision 8a).

Single source of truth for the TSV pair-assignment file import: both
the CLI script (``scripts/import_tempo_pairs.py``, talking to the API
over HTTP) and the server endpoint (``POST /v1/pairing/import``,
in-process) run the same ``run_import`` row loop against a transport,
so the two cannot diverge.

The transport protocol has three operations, mirroring the HTTP calls
the CLI used to make directly:

- ``list_samples(id_field, value, tumor_or_normal)`` — the
  ``GET /v1/sample/`` substring listing; the core applies the
  exact-match + side filter (ambiguity errors included).
- ``possible_matches(tumor_id, logic_name)`` — the
  ``GET /v1/sample/{id}/possible-matches`` data (a dict).
- ``trigger(tumor_id, normal_id, logic_name, trigger_type, strict)`` —
  the ``POST /v1/pairing/trigger`` result (a dict with at least
  ``pairing_logic`` and ``mismatched_keys``).

Transport-level failures raise :class:`ImportApiError` (the loop
treats them like the CLI's HTTP errors); resolution failures raise
:class:`ImportResolveError` (a ``ValueError``, like the CLI's).
"""

from __future__ import annotations

import httpx
from dataclasses import dataclass, field
from typing import Optional, Protocol
from uuid import UUID

EXPECTED_HEADER = ("NORMAL_ID", "TUMOR_ID")
EXTENDED_HEADER = EXPECTED_HEADER + ("NORMAL_PRIMARY_ID", "TUMOR_PRIMARY_ID")
LOG_HEADER = ("status", "NORMAL_ID", "TUMOR_ID", "detail")
TRIGGER_TYPES = ("override", "manual")
ID_FIELDS = ("cmo_sample_name", "primary_id")


class ImportApiError(Exception):
    """A transport-level (HTTP/API) failure for one operation."""


class ImportResolveError(ValueError):
    """A sample could not be resolved to exactly one id (the CLI's
    ``ValueError`` semantics, subclassed so ``except ValueError``
    keeps working)."""


class ImportTransport(Protocol):
    def list_samples(self, id_field: str, value: str, tumor_or_normal: str) -> list[dict]: ...
    def possible_matches(self, tumor_id: str, logic_name: str) -> dict: ...
    def trigger(
        self,
        tumor_id: str,
        normal_id: str,
        logic_name: str,
        trigger_type: str,
        strict: bool,
    ) -> dict: ...


class HttpxTransport:
    """Transport over the running API (the CLI path)."""

    def __init__(self, client: httpx.Client, base: str):
        self._client = client
        self._base = base.rstrip("/")

    def list_samples(self, id_field, value, tumor_or_normal):
        try:
            resp = self._client.get(
                f"{self._base}/v1/sample/",
                params={id_field: value, "tumor_or_normal": tumor_or_normal},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImportApiError(str(exc))
        return resp.json()

    def possible_matches(self, tumor_id, logic_name):
        try:
            resp = self._client.get(
                f"{self._base}/v1/sample/{tumor_id}/possible-matches",
                params={"pairing_logic_name": logic_name},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImportApiError(str(exc))
        return resp.json()

    def trigger(self, tumor_id, normal_id, logic_name, trigger_type, strict):
        try:
            resp = self._client.post(
                f"{self._base}/v1/pairing/trigger",
                json={
                    "tumor_id": tumor_id,
                    "normal_id": normal_id,
                    "pairing_logic_name": logic_name,
                    "trigger_type": trigger_type,
                    "strict": strict,
                },
            )
        except httpx.HTTPError as exc:
            raise ImportApiError(str(exc))
        if resp.status_code >= 400:
            raise ImportApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()


class DbTransport:
    """In-process transport for the server endpoint
    (``POST /v1/pairing/import``).

    Runs the row loop directly against the DB and the pairing service —
    the same code paths the HTTP transport reaches through
    ``GET /v1/sample/``, ``GET /v1/sample/{id}/possible-matches`` and
    ``POST /v1/pairing/trigger`` — so the endpoint and the CLI cannot
    drift. ``logic`` is the resolved :class:`PairingLogic` for the whole
    import; ``actor`` is the authenticated identity dict, attributed on
    every pairing the import creates.
    """

    def __init__(self, db, logic, actor=None):
        # Imported lazily so the CLI (which imports this module outside
        # the app) never loads the DB layer for a pure-HTTP run.
        from fastapi import HTTPException
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from syzygy.models import Sample
        from syzygy.pairing_service import (
            possible_matches as service_possible_matches,
            trigger_pairing as service_trigger_pairing,
        )

        self._db: Session = db
        self._logic = logic
        self._actor = actor
        self._http_exc = HTTPException
        self._sample = Sample
        self._select = select
        self._service_possible_matches = service_possible_matches
        self._service_trigger_pairing = service_trigger_pairing

    def list_samples(self, id_field, value, tumor_or_normal):
        # Mirrors GET /v1/sample/?{id_field}=..&tumor_or_normal=.. :
        # case-insensitive substring match on the column + the side.
        # The shared core applies the exact-match filter.
        stmt = self._select(self._sample).where(
            getattr(self._sample, id_field).ilike(f"%{value}%"),
            self._sample.tumor_or_normal == tumor_or_normal,
        )
        return [
            {
                "id": str(s.id),
                "cmo_sample_name": s.cmo_sample_name,
                "primary_id": s.primary_id,
                "tumor_or_normal": s.tumor_or_normal,
            }
            for s in self._db.scalars(stmt)
        ]

    def possible_matches(self, tumor_id, logic_name):
        sample = self._db.get(self._sample, UUID(tumor_id))
        if sample is None:
            raise ImportApiError("HTTP 404: Sample not found")
        data = self._service_possible_matches(self._db, sample, self._logic)
        # Normalize to the HTTP response shape: the service returns ORM
        # Sample objects, which the endpoint serializes (SampleRead) —
        # the shared core reads the serialized form, so the in-process
        # path must match it exactly.
        return {
            "current_match": self._shape_match(data.get("current_match")),
            "other_matches": [
                self._shape_match(m) for m in data.get("other_matches", [])
            ],
            "other_matches_matched": data.get("other_matches_matched", 0),
        }

    @staticmethod
    def _shape_match(m):
        if not m:
            return None
        s = m["sample"]
        name = s["cmo_sample_name"] if isinstance(s, dict) else s.cmo_sample_name
        return {
            "sample": {"cmo_sample_name": name},
            "matched": m.get("matched"),
            "mismatched_keys": m.get("mismatched_keys"),
            "would_be_selected": m.get("would_be_selected"),
        }

    def trigger(self, tumor_id, normal_id, logic_name, trigger_type, strict):
        tumor = self._db.get(self._sample, UUID(tumor_id))
        try:
            pairing = self._service_trigger_pairing(
                self._db,
                tumor,
                self._logic,
                trigger_type,
                normal_id=UUID(normal_id),
                strict=strict,
                actor=self._actor,
            )
        except self._http_exc as exc:
            # The shared session is reused across rows: roll back any
            # partial work of the failed row so the next row can run.
            try:
                self._db.rollback()
            except Exception:
                pass
            raise ImportApiError(f"HTTP {exc.status_code}: {exc.detail}")
        if pairing is None:
            raise ImportApiError("HTTP 404: No valid pairing could be created")
        return {
            "id": str(pairing.id),
            "pairing_logic": self._logic.name,
            "mismatched_keys": list(pairing.mismatched_keys or []),
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_pairs_text(text: str, source: str = "<text>") -> list[tuple]:
    """Parse the TSV into rows.

    Standard file: header ``NORMAL_ID<TAB>TUMOR_ID`` then one row per
    pair. Extended file: the two extra (optional) columns
    ``NORMAL_PRIMARY_ID``/``TUMOR_PRIMARY_ID`` resolve the named
    samples by ``primary_id`` instead of the name column. Rows with a
    blank optional cell fall back to the name column.

    Returns 4-tuples ``(normal_name, tumor_name, normal_primary,
    tumor_primary)``; ``*_primary`` are ``None`` in the standard format
    (or blank cells). Raises ``ValueError`` on malformed input.
    """
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        raise ValueError(f"{source}: file is empty")
    header = tuple(cell.strip().upper() for cell in lines[0].split("\t"))
    if header == EXPECTED_HEADER:
        width = 2
    elif header == EXTENDED_HEADER:
        width = 4
    else:
        raise ValueError(
            f"{source}: expected header "
            f"'NORMAL_ID<TAB>TUMOR_ID[<TAB>NORMAL_PRIMARY_ID<TAB>TUMOR_PRIMARY_ID]', "
            f"got {lines[0]!r}"
        )
    rows = []
    for lineno, line in enumerate(lines[1:], start=2):
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) != width or not all(cells):
            raise ValueError(
                f"{source}: line {lineno}: expected {width} tab-separated "
                f"non-empty fields, got {line!r}"
            )
        normal_primary = cells[2] if width > 2 and cells[2] else None
        tumor_primary = cells[3] if width > 3 and cells[3] else None
        rows.append((cells[0], cells[1], normal_primary, tumor_primary))
    return rows


# ---------------------------------------------------------------------------
# Resolution + candidate summary (CLI semantics, ported verbatim)
# ---------------------------------------------------------------------------


def resolve_sample(
    transport: ImportTransport,
    value: str,
    tumor_or_normal: str,
    cache: dict,
    id_field: str = "cmo_sample_name",
) -> str:
    """Resolve a sample value (name or primary id) to its sample UUID.

    The sample list endpoint substring-matches ``value``; exactly one
    exact match on the same side wins.    Raises
    :class:`ImportResolveError` when zero or several match.
    """
    key = (id_field, value, tumor_or_normal)
    if key in cache:
        return cache[key]
    samples = transport.list_samples(id_field, value, tumor_or_normal)
    exact = sorted(
        (s for s in samples if s[id_field] == value and s["tumor_or_normal"] == tumor_or_normal),
        key=lambda s: s["id"],
    )
    if len(exact) == 0:
        raise ImportResolveError(f"no {tumor_or_normal.lower()} sample with {id_field} {value!r}")
    if len(exact) > 1:
        ids = ", ".join(s["id"] for s in exact)
        raise ImportResolveError(f"{id_field} {value!r} matches {len(exact)} {tumor_or_normal.lower()} samples ({ids}); ambiguous")
    sample_id = exact[0]["id"]
    cache[key] = sample_id
    return sample_id


def candidate_normal_summary(
    transport: ImportTransport,
    tumor_id: str,
    pairing_logic_name: str,
) -> tuple[int, list[str], Optional[str]]:
    """Summarize the tumor's candidate normals under the pairing logic:
    how many MATCHED, their names, and which one the strategy would
    pick. Raises :class:`ImportApiError` on transport failure (the
    loop treats that as non-blocking — same as the CLI's swallowed
    ``httpx.HTTPError``)."""
    data = transport.possible_matches(tumor_id, pairing_logic_name)
    current = data.get("current_match") or {}
    matched = bool(current) and not (current.get("mismatched_keys") or [])
    names = []
    cascade_pick = None
    if matched:
        names.append(current["sample"]["cmo_sample_name"])
        cascade_pick = current["sample"]["cmo_sample_name"]
    for cand in data.get("other_matches", []):
        if cand.get("matched"):
            name = cand["sample"]["cmo_sample_name"]
            names.append(name)
            if cand.get("would_be_selected"):
                cascade_pick = name
    count = data.get("other_matches_matched", 0) + (1 if matched else 0)
    return count, names, cascade_pick


# ---------------------------------------------------------------------------
# Row results + the import loop
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    """Outcome of one import row (the CLI's per-row statuses)."""

    status: str  # "OK" | "MISMATCH" | "MULTI" | "FAIL"
    normal_name: str
    tumor_name: str
    detail: str = ""
    line: str = ""  # the CLI's printed line for this row
    multi_line: Optional[str] = None  # the CLI's standalone MULTI line
    to_stderr: bool = False
    normal_sample_id: Optional[str] = None
    tumor_sample_id: Optional[str] = None
    pairing_id: Optional[str] = None
    pairing: Optional[dict] = field(default=None, compare=False)


@dataclass
class ImportResult:
    rows: list[RowResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r.status == "FAIL")

    @property
    def mismatched(self) -> int:
        return sum(1 for r in self.rows if r.status == "MISMATCH")

    @property
    def pinned(self) -> int:
        return sum(1 for r in self.rows if r.status in ("OK", "MULTI"))

    @property
    def multi(self) -> int:
        return sum(1 for r in self.rows if r.multi_line is not None)

    @property
    def log_rows(self) -> list[tuple]:
        """The --log rows (header excluded): everything but clean OK."""
        return [
            (r.status, r.normal_name, r.tumor_name, r.detail)
            for r in self.rows
            if r.status in ("FAIL", "MISMATCH", "MULTI")
        ]

    def log_tsv(self) -> str:
        return flatten_detail_tsv(self.log_rows)

    def summary_line(self, logic_name: str, dry_run: bool) -> str:
        line = (
            f"\n{logic_name}: {self.pinned} pinned, {self.mismatched} "
            f"pinned-with-mismatch, {self.multi} with multiple candidate "
            f"normals, {self.failed} failed (of {self.total} rows)"
        )
        return line + (" [dry run]" if dry_run else "")


def flatten_detail_tsv(log_rows: list[tuple]) -> str:
    """Render log rows as TSV (the CLI --log format, verbatim): the
    detail is whitespace-collapsed so the log stays strictly one row
    per line, tab-delimited."""
    out = ["\t".join(LOG_HEADER)]
    for status, n, t, detail in log_rows:
        flat = " ".join(str(detail).split())
        out.append("\t".join((status, n, t, flat)))
    return "\n".join(out) + "\n"


def _process_row(
    row: tuple,
    transport: ImportTransport,
    logic: str,
    trigger_type: str,
    strict: bool,
    dry_run: bool,
    id_field: str,
    cache: dict,
) -> RowResult:
    normal_name, tumor_name, normal_primary, tumor_primary = row
    label = f"{normal_name} -> {tumor_name}"

    # Resolve both samples (primary_id when the extended columns are
    # given, else the name column under --id-field / id_field).
    try:
        normal_value, normal_field = (
            (normal_primary, "primary_id") if normal_primary is not None else (normal_name, id_field)
        )
        tumor_value, tumor_field = (
            (tumor_primary, "primary_id") if tumor_primary is not None else (tumor_name, id_field)
        )
        normal_id = resolve_sample(transport, normal_value, "NORMAL", cache, normal_field)
        tumor_id = resolve_sample(transport, tumor_value, "TUMOR", cache, tumor_field)
    except ImportApiError as exc:
        return RowResult(
            status="FAIL",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=f"API error: {exc}",
            line=f"FAIL  {label}: API error: {exc}",
            to_stderr=True,
        )
    except ImportResolveError as exc:
        return RowResult(
            status="FAIL",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=str(exc),
            line=f"FAIL  {label}: {exc}",
            to_stderr=True,
        )

    # Informational: how many candidate normals match under the logic,
    # and which one the strategy would pick.
    multi_note = None
    try:
        n_matched, names, pick = candidate_normal_summary(transport, tumor_id, logic)
        if n_matched > 1:
            if len(names) < n_matched:
                shown = f"{', '.join(names)} (+{n_matched - len(names)} more)"
            else:
                shown = ", ".join(names)
            extra = f"; cascade would pick {pick}" if pick else ""
            multi_note = (
                f"tumor has {n_matched} matching candidate normals ({shown}{extra}); "
                f"pinning curated {normal_name}"
            )
    except ImportApiError:
        pass  # informational only — never block the curated pin

    if dry_run:
        note = f" — {multi_note}" if multi_note else ""
        return RowResult(
            status="MULTI" if multi_note else "OK",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=multi_note or "",
            line=f"OK    {label}: resolves (dry run){note}",
            multi_line=f"MULTI {label}: {multi_note}" if multi_note else None,
            normal_sample_id=normal_id,
            tumor_sample_id=tumor_id,
        )

    try:
        pairing = transport.trigger(tumor_id, normal_id, logic, trigger_type, strict)
    except ImportApiError as exc:
        return RowResult(
            status="FAIL",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=str(exc),
            line=f"FAIL  {label}: {exc}",
            to_stderr=True,
            normal_sample_id=normal_id,
            tumor_sample_id=tumor_id,
        )

    keys = pairing.get("mismatched_keys")
    if keys:
        detail = "; ".join(keys)
        if multi_note:
            detail = f"{detail} | {multi_note}"
        return RowResult(
            status="MISMATCH",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=detail,
            line=(
                f"WARN  {label}: paired under {pairing['pairing_logic']} "
                f"with mismatched keys: {detail}"
            ),
            multi_line=f"MULTI {label}: {multi_note}" if multi_note else None,
            normal_sample_id=normal_id,
            tumor_sample_id=tumor_id,
            pairing_id=str(pairing.get("id")),
            pairing=pairing,
        )
    if multi_note:
        return RowResult(
            status="MULTI",
            normal_name=normal_name,
            tumor_name=tumor_name,
            detail=multi_note,
            line=(
                f"OK    {label}: paired under {pairing['pairing_logic']} "
                f"— {multi_note}"
            ),
            multi_line=f"MULTI {label}: {multi_note}",
            normal_sample_id=normal_id,
            tumor_sample_id=tumor_id,
            pairing_id=str(pairing.get("id")),
            pairing=pairing,
        )
    return RowResult(
        status="OK",
        normal_name=normal_name,
        tumor_name=tumor_name,
        detail="",
        line=f"OK    {label}: paired under {pairing['pairing_logic']}",
        normal_sample_id=normal_id,
        tumor_sample_id=tumor_id,
        pairing_id=str(pairing.get("id")),
        pairing=pairing,
    )


def run_import(
    rows: list[tuple],
    transport: ImportTransport,
    *,
    logic: str,
    trigger_type: str = "override",
    strict: bool = False,
    dry_run: bool = False,
    id_field: str = "cmo_sample_name",
) -> ImportResult:
    """The import row loop, shared by the CLI and POST /v1/pairing/import.

    ``rows`` is ``parse_pairs_text`` output; per row: resolve both
    samples (primary_id precedence for extended files), check the
    tumor's candidate normals under ``logic`` (informational MULTI
    note), then dry-run or trigger. Returns an :class:`ImportResult`
    whose statuses/logs/summary match the CLI's output exactly.
    """
    cache: dict = {}
    result = ImportResult()
    for row in rows:
        result.rows.append(
            _process_row(
                row,
                transport,
                logic,
                trigger_type,
                strict,
                dry_run,
                id_field,
                cache,
            )
        )
    return result
