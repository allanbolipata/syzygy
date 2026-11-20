"""On-demand export of beagle's TEMPO operator files (ticket E1).

Spec: Projects/Syzygy/Factory/TEMPO_EXPORT_TICKETS.md.

``GET /v1/export/tempo/{pairing,unpaired,tracker,conflict}`` stream the
beagle files as ``text/plain`` downloads with beagle's filenames
(``sample_pairing.txt`` / ``sample_unpaired.txt`` /
``sample_tracker.txt`` / ``sample_conflict.txt``). Auth required (router
mounted under the global auth dependency in ``main.py``); the
``?pairing_logic_name`` query param defaults to ``TEMPO`` and an unknown
logic name is a 404 (conflict is logic-agnostic: no logic param, per the
spec's "all samples with errors" scope).

The response bodies are produced by ``syzygy.export.tempo_files``:

- pairing -- the extended 4-column TSV (beagle's ``sample_pairing.txt``
  shape: cmo display labels in ``NORMAL_ID`` / ``TUMOR_ID``, the
  ``primary_id`` values in the two ``*_PRIMARY_ID`` columns), latest
  pairing rows for the logic, ordered by tumor ``cmo_sample_name``;
  round-trips through ``scripts/import_tempo_pairs.py --dry-run``.
- unpaired -- the 8 beagle fields + ``Possible Reason?``
  (``sample_unpaired.txt`` shape), logic-family tumors with no latest
  pairing row for the logic, reasons from the strategy diagnostics and
  never empty.
- tracker -- beagle's byte-equal PM header + the raw metadata columns
  (``sample_tracker.txt`` shape), one row per sample in the logic's
  family (tumors and normals, paired or unpaired); column resolution
  order sample column -> ``additional_metadata`` ->
  ``additional_metadata["requestMetadata"]`` -> empty cell, missing keys
  never raise, DMP samples put ``primary_id`` in the Collaborator_ID
  column.
- conflict -- beagle's byte-equal conflict header (the 8 sample fields
  + ``Conflict Reason``; ``sample_conflict.txt`` shape), one row per
  sample whose ``errors`` is non-null and non-empty (ingest validation
  failures), logic-agnostic (no logic param), reasons joined with
  ``;`` and flattened so no cell can break the TSV grid.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from syzygy.database import get_db
from syzygy.export.tempo_files import (
    CONFLICT_FILENAME,
    PAIRING_FILENAME,
    TRACKER_FILENAME,
    UNPAIRED_FILENAME,
    build_conflict_tsv,
    build_pairing_tsv,
    build_tracker_tsv,
    build_unpaired_tsv,
)

router = APIRouter(prefix="/export/tempo", tags=["exports"])


def _download(filename: str, body: str) -> PlainTextResponse:
    return PlainTextResponse(
        content=body,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/pairing",
    response_class=PlainTextResponse,
    summary="beagle sample_pairing.txt (extended 4-col TSV) for a pairing logic",
)
def export_tempo_pairing(
    pairing_logic_name: str = Query(default="TEMPO"),
    db: Session = Depends(get_db),
):
    try:
        body = build_pairing_tsv(db, pairing_logic_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _download(PAIRING_FILENAME, body)


@router.get(
    "/unpaired",
    response_class=PlainTextResponse,
    summary="beagle sample_unpaired.txt (8 fields + Possible Reason?) for a pairing logic",
)
def export_tempo_unpaired(
    pairing_logic_name: str = Query(default="TEMPO"),
    db: Session = Depends(get_db),
):
    try:
        body = build_unpaired_tsv(db, pairing_logic_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _download(UNPAIRED_FILENAME, body)


@router.get(
    "/tracker",
    response_class=PlainTextResponse,
    summary="beagle sample_tracker.txt (PM header + raw metadata columns) for a pairing logic",
)
def export_tempo_tracker(
    pairing_logic_name: str = Query(default="TEMPO"),
    db: Session = Depends(get_db),
):
    try:
        body = build_tracker_tsv(db, pairing_logic_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _download(TRACKER_FILENAME, body)


@router.get(
    "/conflict",
    response_class=PlainTextResponse,
    summary="beagle sample_conflict.txt (samples with validation errors; logic-agnostic)",
)
def export_tempo_conflict(db: Session = Depends(get_db)):
    # Spec: conflict = all samples with errors, logic-agnostic — no
    # pairing_logic_name param on this route.
    return _download(CONFLICT_FILENAME, build_conflict_tsv(db))
