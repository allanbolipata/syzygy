#!/usr/bin/env python3
"""Backfill ``patient__dmp`` on DMP samples from the sample name.

Beagle DMP records carry the P- patient id ONLY in the sample name (the
payload has no explicit ``patient.dmp``). DMP rows imported before the
ingest-time imputation (``syzygy.dmp_translator.to_dmp_sample_kwargs``,
t_9e6c5cd6) therefore have ``patient__dmp`` NULL — and HARMONY's pairing
checks the stored column only (no name-parse fallback, by design), so
those tumors were invisible to it (staging audit 2026-11-20:
87,802 DMP tumors missing ``patient__dmp``).

This script fills the stored column for existing rows with the same
parse ``remap_dmp_record`` uses (the reference name pattern, group 1 —
``syzygy.dmp_translator.parse_dmp_patient``, the same pattern the
``dmp_patient_key`` generated column applies in SQL)::

    UPDATE samples
       SET patient__dmp = <parsed P- id>
     WHERE sample_type = 'DMP'
       AND (patient__dmp IS NULL OR patient__dmp = '')
       AND primary_id parses under the reference name pattern;

Stored non-empty values are NEVER touched, so the script is idempotent
(a second run updates 0) and safe to run before or after the ingest
imputation lands. Updates are grouped by parsed patient id and committed
in batches of 500 rows so a long run never holds the write lock open.

    python scripts/backfill_patient_dmp.py
    python scripts/backfill_patient_dmp.py <database-url>

Without an argument it uses the app's configured DATABASE_URL (the
``.env`` / ``database.settings`` value).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a plain script
# (``python scripts/backfill_patient_dmp.py``): sys.path[0] is this
# file's directory, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from syzygy.dmp_translator import parse_dmp_patient
from syzygy.models import Sample


def backfill_patient_dmp(db: Session, batch_size: int = 500) -> int:
    """Impute ``patient__dmp`` on DMP rows that have none.

    Only rows with ``sample_type = 'DMP'`` whose ``patient__dmp`` is NULL
    or ``''`` and whose ``primary_id`` parses under the reference name
    pattern (``parse_dmp_patient`` — the same parse ``remap_dmp_record``
    and the ``dmp_patient_key`` generated column use) are written. Stored
    non-empty values are never touched, so a second run updates 0
    (idempotent). Returns the number of rows updated.
    """
    candidates = db.execute(
        sa.select(Sample.id, Sample.primary_id).where(
            sa.and_(
                Sample.sample_type == "DMP",
                sa.or_(Sample.patient__dmp.is_(None), Sample.patient__dmp == ""),
            )
        )
    ).all()

    # Group by parsed patient id: one UPDATE per (patient, batch) instead
    # of one per row.
    by_patient: dict[str, list] = {}
    for sample_id, primary_id in candidates:
        parsed = parse_dmp_patient(primary_id)
        if parsed:
            by_patient.setdefault(parsed, []).append(sample_id)

    updated = 0
    for parsed, ids in by_patient.items():
        for i in range(0, len(ids), batch_size):
            res = db.execute(
                sa.update(Sample)
                .where(Sample.id.in_(ids[i : i + batch_size]))
                .values(patient__dmp=parsed)
            )
            updated += res.rowcount or 0
            db.commit()

    unparsed = len(candidates) - sum(len(ids) for ids in by_patient.values())
    if unparsed:
        print(
            f"  skipped {unparsed} DMP row(s) with no patient__dmp and a "
            "sample name that does not parse under the reference pattern"
        )
    print(f"backfill_patient_dmp: updated {updated} DMP row(s)")
    return updated


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        from syzygy.database import settings

        url = settings.database_url

    engine = sa.create_engine(url)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        backfill_patient_dmp(db)
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
