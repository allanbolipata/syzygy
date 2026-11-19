"""Migrate pairing-logic rows to the consolidated names.

Groups related logics under one canonical row (see PAIRING_LOGIC_ALIASES
in syzygy/pairing_service.py):

* ``argos`` (lowercase, pre-consolidation) -> renamed to ``ARGOS``;
  if both exist, the lowercase row's pairings move to ``ARGOS``.
* ``IMPACT`` row -> its pairings re-pointed to ``ARGOS``, row deleted
  (IMPACT now resolves to ARGOS via the alias).
* ``CHRONOS`` row -> its pairings re-pointed to ``TEMPO``, row deleted
  (CHRONOS now resolves to TEMPO via the alias).
* Remaining rows (ARGOS/TEMPO/ACCESS) get their strategy_name
  self-healed to the current canonical values.

Idempotent: a second run is a no-op. Dev-database housekeeping only --
do not run against production data without review.

Alembic (ticket A2): this stays a standalone script and has NO
corresponding alembic revision, deliberately. It is a pure DATA
migration (row renames/consolidation in ``pairing_logic`` + re-pointing
``pairing_table`` rows) with no DDL: the versioned revision chain tracks
schema shape, and this one-shot dev housekeeping fix would add no
schema state a fresh ``create_all`` database doesn't already have, so
folding it into the chain would only make ``upgrade head`` do data
surgery on databases that never had the pre-consolidation rows. The
other three migrate scripts (patient_columns, consent, dmp_generated)
do have Alembic twins (revisions 0002-0004) because they change schema.

Usage:
    DATABASE_URL=... python scripts/migrate_logic_names.py
"""
from __future__ import annotations

import os
import sys

STRATEGY_BY_NAME = {
    "ARGOS": "argos",
    "TEMPO": "tempo",
    "ACCESS": "default",
}
REPOINT = {
    # old row name: canonical row name its pairings move to
    "argos": "ARGOS",
    "IMPACT": "ARGOS",
    "CHRONOS": "TEMPO",
}


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)

    from syzygy.database import Base, SessionLocal, engine
    from syzygy.models import PairingLogic, PairingTable

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for old, new in REPOINT.items():
            old_row = db.query(PairingLogic).filter(PairingLogic.name == old).first()
            if old_row is None:
                continue
            new_row = db.query(PairingLogic).filter(PairingLogic.name == new).first()
            if new_row is None:
                # Rename in place (no data loss; keeps the row id).
                old_row.name = new
                db.commit()
                continue
            n = (
                db.query(PairingTable)
                .filter(PairingTable.pairing_logic_id == old_row.id)
                .update({PairingTable.pairing_logic_id: new_row.id})
            )
            db.delete(old_row)
            db.commit()
            print(f"  {old!r} row removed; {n} pairing row(s) moved to {new!r}")
        for name, strategy in STRATEGY_BY_NAME.items():
            row = db.query(PairingLogic).filter(PairingLogic.name == name).first()
            if row is not None and row.strategy_name != strategy:
                print(f"  {name!r} strategy_name: {row.strategy_name!r} -> {strategy!r}")
                row.strategy_name = strategy
        db.commit()
        rows = db.query(PairingLogic).order_by(PairingLogic.name).all()
        print("pairing_logic rows now:")
        for r in rows:
            print(f"  {r.name:<10} strategy={r.strategy_name}")
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
