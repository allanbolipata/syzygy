#!/usr/bin/env python3
"""Bait-set registration backfill (ticket P4).

While ``LOGIC_GATING_ENABLED=true`` a sample whose ``bait_set`` has no row
in the ``bait_sets`` table belongs to no pairing logic and is INVISIBLE TO
EVERYONE (fail-closed, design point 3 of the gating policy). The app
registers names lazily — on ingest/pairing, via
``bait_sets.resolve_bait_set_logic`` — but data that was imported or
backfilled before registration was first seen is never re-scanned, so its
bait sets can stay unregistered indefinitely (dev-box example: IMPACT468
samples invisible to all although ``classify_bait_set`` maps it to ARGOS).

This one-time script closes that gap:

1. read every distinct non-NULL ``bait_set`` value in ``samples``;
2. run each UNREGISTERED value through ``classify_bait_set``;
3. insert a ``bait_sets`` row for every classifiable value —
   DRY-RUN BY DEFAULT; pass ``--apply`` to write;
4. REPORT — do NOT insert — values no rule classifies (fail-closed
   preserved: unknown bait sets stay invisible until someone registers
   them deliberately, see the runbook in ``docs/logic_gating_setup.md``).

Idempotent: registration is matched case-insensitively, exactly like the
app's ``find_bait_set``; a second ``--apply`` run inserts nothing.

Usage (repo root; DATABASE_URL from the env / .env, like every other
syzygy script):
    .venv/bin/python scripts/register_bait_sets.py           # dry-run
    .venv/bin/python scripts/register_bait_sets.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Optional, Sequence

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from syzygy.bait_sets import classify_bait_set
from syzygy.models import BaitSet, PairingLogic, Sample


@dataclass
class PlanEntry:
    name: str
    logic_name: str
    sample_count: int
    logic_id: object  # PairingLogic.id


@dataclass
class RunReport:
    already_registered: list  # (name, sample_count)
    planned: list  # PlanEntry
    unclassifiable: list  # (name, sample_count)
    missing_logic: list  # (name, logic_name, sample_count)
    inserted: list = field(default_factory=list)  # PlanEntry (empty on dry-run)

    @property
    def applied(self) -> bool:
        return bool(self.inserted)


def build_plan(db: Session) -> RunReport:
    """Distinct unregistered bait_set values in ``samples``, classified.

    Mirrors ``resolve_bait_set_logic``'s registration side: already-
    registered names are matched case-insensitively (``find_bait_set``
    semantics); a classifiable name whose ``pairing_logics`` row is
    missing cannot be inserted and is reported, not inserted.
    """
    counts = db.execute(
        select(Sample.bait_set, func.count())
        .where(Sample.bait_set.is_not(None), Sample.bait_set != "")
        .group_by(Sample.bait_set)
    ).all()
    registered = {
        row.lower()
        for row in db.scalars(select(BaitSet.bait_set)).all()
        if row
    }
    report = RunReport(
        already_registered=[], planned=[], unclassifiable=[], missing_logic=[]
    )
    for name, count in sorted(counts, key=lambda rc: (rc[0] or "").lower()):
        if not name:
            continue
        if name.strip().lower() in registered:
            report.already_registered.append((name, count))
            continue
        logic_name = classify_bait_set(name)
        if logic_name is None:
            report.unclassifiable.append((name, count))
            continue
        logic = db.scalar(select(PairingLogic).where(PairingLogic.name == logic_name))
        if logic is None:
            report.missing_logic.append((name, logic_name, count))
            continue
        report.planned.append(
            PlanEntry(name=name.strip(), logic_name=logic_name,
                      sample_count=count, logic_id=logic.id)
        )
    return report


def run(db: Session, apply: bool = False) -> int:
    """Execute the backfill (dry-run unless ``apply``). Returns 0."""
    report = build_plan(db)
    mode = "APPLY" if apply else "DRY RUN"
    print(f"register_bait_sets — {mode}")
    print(
        f"  {len(report.already_registered)} already registered, "
        f"{len(report.planned)} to register, "
        f"{len(report.unclassifiable)} unclassifiable, "
        f"{len(report.missing_logic)} with a missing pairing-logic row"
    )
    for entry in report.planned:
        print(
            f"  {'insert ' if apply else 'would insert '}"
            f"{entry.name!r} -> {entry.logic_name} "
            f"({entry.sample_count} sample(s))"
        )
    for name, logic_name, count in report.missing_logic:
        print(
            f"  SKIPPED {name!r}: classify_bait_set -> {logic_name!r} but no "
            f"pairing_logics row {logic_name!r} exists ({count} sample(s)); "
            f"NOT inserted"
        )
    for name, count in report.unclassifiable:
        print(
            f"  UNCLASSIFIABLE {name!r} ({count} sample(s)): no "
            f"classify_bait_set rule matches — NOT inserted (fail-closed "
            f"preserved; register manually, see docs/logic_gating_setup.md)"
        )
    if apply:
        for entry in report.planned:
            db.add(BaitSet(bait_set=entry.name, pairing_logic_id=entry.logic_id))
            report.inserted.append(entry)
        db.commit()
        print(f"inserted {len(report.inserted)} bait_sets row(s)")
    elif report.planned:
        print("dry run — no rows written (re-run with --apply to register)")
    if not report.planned and not report.unclassifiable and not report.missing_logic:
        print("nothing to do — every bait_set value in samples is already registered")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="insert the classified bait_sets rows (default: dry run only)",
    )
    args = parser.parse_args(argv)

    # Imported here (not at module top) so tests can monkeypatch the
    # syzygy.database bindings; same lazy pattern as migrate_logic_names.
    from syzygy.database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        return run(db, apply=args.apply)
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
