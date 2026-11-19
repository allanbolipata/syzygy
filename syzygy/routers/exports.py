"""On-demand export of beagle's TEMPO operator files (ticket E1).

Spec: Projects/Syzygy/Factory/TEMPO_EXPORT_TICKETS.md.

``GET /v1/export/tempo/{pairing,unpaired}`` stream the two pairing-side
beagle files as ``text/plain`` downloads with beagle's filenames
(``sample_pairing.txt`` / ``sample_unpaired.txt``). Auth required (router
mounted under the global auth dependency in ``main.py``); the
``?pairing_logic_name`` query param defaults to ``TEMPO`` and an unknown
logic name is a 404.

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

E2 (tracker) and E3 (conflict) add their routes here.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from syzygy.database import get_db
from syzygy.export.tempo_files import (
    PAIRING_FILENAME,
    UNPAIRED_FILENAME,
    build_pairing_tsv,
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
