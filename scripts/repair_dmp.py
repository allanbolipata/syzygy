#!/usr/bin/env python3
"""Re-run DMP automatic pairing over the samples already in the database.

PR #19 (DMP Reference Alignment, ticket 3/8) replaced the one-tier `dmp`
strategy with the two-tier cascade (pool_id primary, IM_ver rescue,
no-winner fan-out). Two facts make a dedicated re-run necessary:

* the ordinary ingest path never revises existing pairings, and
* the bulk importer's ``automatic`` trigger is a no-op on a tumor that
  already has a latest row ("automatic — return the existing row set
  unchanged if one exists" — pairing_service._trigger_multi_pair).

``manual`` is the trigger that re-evaluates: the strategy re-runs and a
changed row set replaces the current one (old rows retire as
latest=False history; identical row sets are left untouched).

Safety: a tumor whose current latest rows include a pinned/override
pairing (trigger_type "manual" or "override" — the dashboard pin) is
SKIPPED; manual pairings always win. Rows with trigger_type "automatic"
or NULL (legacy) are re-evaluated — that is the point of the re-run.

remap_dmp_record is applied at pairing time, so no sample re-import or
column migration is needed first.

Usage (dev DB, host venv):
    set -a && source .env && set +a
    .venv/bin/python scripts/repair_dmp.py [--dmp-name DMP] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database import engine
from models import PairingLogic, PairingTable, Sample
from pairing_service import trigger_pairing

PINNED_TRIGGER_TYPES = ("manual", "override")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dmp-name", default="DMP", help="Pairing logic name (default: DMP).")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    args = ap.parse_args()

    db = Session(engine, future=True)
    try:
        logic = db.execute(
            sa.select(PairingLogic).where(PairingLogic.name == args.dmp_name)
        ).scalar_one_or_none()
        if logic is None:
            sys.exit(
                f"pairing logic {args.dmp_name!r} not found — start the app once "
                "(the lifespan seeds/repairs the logic rows), then re-run."
            )
        print(f"logic {logic.name!r}  strategy_name={logic.strategy_name!r}")

        n_tumors = db.scalar(
            sa.select(sa.func.count()).select_from(Sample).where(
                Sample.sample_type == "DMP", Sample.tumor_or_normal == "TUMOR"
            )
        )
        n_normals = db.scalar(
            sa.select(sa.func.count()).select_from(Sample).where(
                Sample.sample_type == "DMP", Sample.tumor_or_normal == "NORMAL"
            )
        )
        tumors = (
            db.execute(
                sa.select(Sample)
                .where(Sample.sample_type == "DMP", Sample.tumor_or_normal == "TUMOR")
                .order_by(Sample.primary_id)
            )
            .scalars()
            .all()
        )

        def latest_rows() -> list[PairingTable]:
            return (
                db.execute(
                    sa.select(PairingTable).where(
                        PairingTable.pairing_logic_id == logic.id,
                        PairingTable.latest == True,  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )

        rows_before = latest_rows()
        pinned_tumor_ids = {
            r.tumor_id for r in rows_before if r.trigger_type in PINNED_TRIGGER_TYPES
        }
        print(
            f"DMP samples: {n_tumors} tumors / {n_normals} normals; "
            f"latest pairing rows under {args.dmp_name!r}: {len(rows_before)} "
            f"({len(pinned_tumor_ids)} tumor(s) pinned — will be skipped)"
        )

        retriggered = skipped = no_match = errors = 0
        for i, tumor in enumerate(tumors, 1):
            if tumor.id in pinned_tumor_ids:
                skipped += 1
                continue
            if args.dry_run:
                retriggered += 1
                continue
            try:
                if trigger_pairing(db, tumor, logic, "manual") is None:
                    no_match += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                errors += 1
                print(f"  ERROR {tumor.primary_id}: {type(exc).__name__}: {exc}")
            retriggered += 1
            if i % 500 == 0:
                print(f"  ...{i}/{n_tumors}", flush=True)

        rows_after = latest_rows()
        fresh_manual = sum(1 for r in rows_after if r.trigger_type == "manual")
        print(
            f"{'[dry-run] would re-trigger' if args.dry_run else 're-triggered'}: "
            f"{retriggered} tumors (skipped pinned: {skipped}, no match found: {no_match}, "
            f"errors: {errors})"
        )
        print(
            f"latest rows under {args.dmp_name!r}: {len(rows_before)} before -> "
            f"{len(rows_after)} after (of which trigger_type='manual': {fresh_manual})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
