#!/usr/bin/env python3
"""Idempotent dev-fixture seeder for the syzygy dev database.

Prepopulates the local dev DB with synthetic samples covering EVERY seeded
pairing logic (ACCESS, ARGOS, TEMPO, DMP, HARMONY), and creates real
``pairing_table`` rows through the app's own per-tumor pairing trigger
(``trigger_pairing``) -- not hand-inserted rows.

Why per-tumor ``trigger_pairing`` and not the bulk triggers?
    ``trigger_dmp_pairing`` / ``trigger_harmony_pairing`` scan ALL
    ``sample_type == "DMP"`` tumors regardless of logic, so the HARMONY
    DMP-shaped fixture would leak into the DMP pairing space and vice-versa.
    ``trigger_pairing(db, tumor, logic, "automatic")`` scopes each pairing to
    exactly one logic.

Idempotency
-----------
Every seeded sample is tagged ``file_group="dev-fixture"``. Re-running the
seeder upserts by ``primary_id`` (existing rows are reused, never duplicated)
and the app's triggers are themselves idempotent (an existing latest pairing
is returned, not recreated). ``--reset`` first deletes the pairing rows that
reference dev-fixture samples, then the dev-fixture samples themselves.

Usage (run from the repo root)
------------------------------
    python scripts/seed_dev_data.py            # seed (idempotent)
    python scripts/seed_dev_data.py --reset    # wipe dev fixtures, then seed

The database is selected by DATABASE_URL (env var or .env) via
syzygy.database.settings -- no connection string is hardcoded here.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select

# Make the repo root importable (this script lives in scripts/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from syzygy.bait_sets import seed_bait_sets
from syzygy.database import Base, SessionLocal, engine
from syzygy.models import PairingLogic, PairingTable, Sample
from syzygy.pairing_service import trigger_pairing
from syzygy.pairing_strategies import seed_pooled_machines
import syzygy.models  # noqa: F401  (register models on Base.metadata)


DEV_FILE_GROUP = "dev-fixture"
SEQUENCING_DATE = datetime(2026, 11, 1)

# Expected number of latest pairing rows per case (for the summary check).
_EXPECTED_ROWS = {"pair": 1, "unmatched": 0, "fanout(2)": 2}

LOGIC_ORDER = ("ACCESS", "ARGOS", "TEMPO", "DMP", "HARMONY")


@dataclass
class SampleSpec:
    """Constructor kwargs for one synthetic ``Sample`` row."""

    primary_id: str
    patient__cmo: Optional[str]
    request_id: Optional[str]
    sample_type: str
    tumor_or_normal: str
    gene_panel: str
    bait_set: Optional[str]
    preservation: Optional[list] = None
    run_mode: Optional[str] = None
    patient__dmp: Optional[str] = None
    additional_metadata: Optional[dict] = None
    consent: Optional[bool] = None

    def kwargs(self) -> dict:
        kw: dict = {
            "primary_id": self.primary_id,
            "cmo_sample_name": self.primary_id,
            "patient__cmo": self.patient__cmo,
            "request_id": self.request_id,
            "sample_type": self.sample_type,
            "gene_panel": self.gene_panel,
            "bait_set": self.bait_set,
            "tumor_or_normal": self.tumor_or_normal,
            "preservation": self.preservation,
            "run_mode": self.run_mode,
            "file_group": DEV_FILE_GROUP,
            "sequencing_date": SEQUENCING_DATE,
        }
        if self.patient__dmp is not None:
            kw["patient__dmp"] = self.patient__dmp
        if self.additional_metadata is not None:
            kw["additional_metadata"] = self.additional_metadata
        if self.consent is not None:
            kw["consent"] = self.consent
        return kw


@dataclass
class Case:
    """One pairing fixture: seed the normals first, then trigger the tumor."""

    logic: str
    label: str
    expected: str  # "pair" | "unmatched" | "fanout(2)"
    tumor: SampleSpec
    normals: list = field(default_factory=list)


def _regular(primary_id, patient, request, sample_type, ton, panel, bait,
             preservation=None, run_mode=None) -> SampleSpec:
    """A LIMS-style (non-DMP) fixture sample."""
    return SampleSpec(
        primary_id=primary_id, patient__cmo=patient, request_id=request,
        sample_type=sample_type, tumor_or_normal=ton, gene_panel=panel,
        bait_set=bait, preservation=preservation, run_mode=run_mode,
    )


def _dmp(primary_id, patient_dmp, pool_id, ton, patient_cmo=None) -> SampleSpec:
    """A DMP/HARMONY fixture sample.

    ``primary_id`` must carry a ``P-<digits>-<word>-IM<n>`` segment so the
    DMP translator's regex parses the IM_ver (group 3); the real patient id
    lives in the ``patient__dmp`` column, which is the DMP strategy's
    ``patient`` matching key.

    ``patient_cmo`` is the optional CMO bridge: the Patients tab groups by
    ``patient__cmo``, so a seeded DMP patient carrying one is navigable
    from the Pair Report's patient link (card t_d8720219's direct-proof
    path). It is inert for DMP/HARMONY pairing (those key on
    ``patient__dmp`` + pool/IM_ver). The *ingested* ``P-00DEV9`` fixture
    deliberately keeps no bridge so the no-match banner path stays exercised.
    """
    return SampleSpec(
        primary_id=primary_id, patient__cmo=patient_cmo, request_id=None,
        sample_type="DMP", tumor_or_normal=ton, gene_panel="IMPACT468",
        bait_set="IMPACT468", patient__dmp=patient_dmp,
        additional_metadata={"pool_id": pool_id}, consent=False,
    )


# ---------------------------------------------------------------------------
# Coverage matrix -- every seeded pairing logic
# ---------------------------------------------------------------------------

FIXTURES: list[Case] = [
    # ---- ACCESS (default): hard keys patient__cmo/gene_panel/bait_set, NO
    # preservation/run-mode gate; cascade same_request -> other_request.
    Case(
        logic="ACCESS",
        label="C-DEV001 same_request",
        expected="pair",
        tumor=_regular("DEV-001-T", "C-DEV001", "99001", "Tumor", "TUMOR",
                       "MSK-ACCESS-468", "MSK-ACCESS-v2_0-probesAllwFP",
                       preservation=["FFPE"], run_mode="NovaSeq 6000"),
        normals=[_regular("DEV-001-N", "C-DEV001", "99001", "Normal", "NORMAL",
                          "MSK-ACCESS-468", "MSK-ACCESS-v2_0-probesAllwFP",
                          preservation=["Frozen"], run_mode="NovaSeq 6000")]),
    Case(
        logic="ACCESS",
        label="C-DEV002 other_request",
        expected="pair",
        tumor=_regular("DEV-002-T", "C-DEV002", "99001", "Tumor", "TUMOR",
                       "MSK-ACCESS-468", "MSK-ACCESS-v2_0-probesAllwFP",
                       preservation=["FFPE"], run_mode="NovaSeq 6000"),
        normals=[_regular("DEV-002-N", "C-DEV002", "99002", "Normal", "NORMAL",
                          "MSK-ACCESS-468", "MSK-ACCESS-v2_0-probesAllwFP",
                          preservation=["Frozen"], run_mode="NovaSeq 6000")]),
    # C-DEV003 stays unmatched. The default (ACCESS) strategy has no
    # preservation gate, so the FFPE-tumor vs frozen-normal split alone would
    # pair it; the unpaired outcome is driven by a bait-set hard-key
    # difference (v2 vs v1, both ACCESS bait sets) -- the FFPE/frozen split is
    # kept as the visible annotation.
    Case(
        logic="ACCESS",
        label="C-DEV003 bait mismatch (FFPE vs frozen)",
        expected="unmatched",
        tumor=_regular("DEV-003-T", "C-DEV003", "99001", "Tumor", "TUMOR",
                       "MSK-ACCESS-468", "MSK-ACCESS-v2_0-probesAllwFP",
                       preservation=["FFPE"], run_mode="NovaSeq 6000"),
        normals=[_regular("DEV-003-N", "C-DEV003", "99001", "Normal", "NORMAL",
                          "MSK-ACCESS-468", "MSK-ACCESS-v1_0-probesAllwFP",
                          preservation=["Frozen"], run_mode="NovaSeq 6000")]),

    # ---- ARGOS (IMPACT505 / IMPACT505_BAITS): hard keys + run-mode bucket
    # (novaseq_x/novaseq/hiseq) + preservation gate; pooled tier via
    # pooled_machines (diana -> novaseq).
    Case(
        logic="ARGOS",
        label="C-DEV010 matched (NovaSeq 6000 + FFPE)",
        expected="pair",
        tumor=_regular("DEV-010-T", "C-DEV010", "99010", "Tumor", "TUMOR",
                       "IMPACT505", "IMPACT505_BAITS",
                       preservation=["FFPE"], run_mode="NovaSeq 6000"),
        normals=[_regular("DEV-010-N", "C-DEV010", "99010", "Normal", "NORMAL",
                          "IMPACT505", "IMPACT505_BAITS",
                          preservation=["FFPE"], run_mode="NovaSeq 6000")]),
    # C-DEV011 stays unmatched at the run-mode FAMILY level (HiSeq tumor vs
    # NovaSeq normal). The ARGOS patient tier uses fine buckets
    # (novaseq_x/novaseq/hiseq) but the pooled tier resolves run mode to the
    # coarse novaseq/hiseq FAMILY (pool machine -> class; tumor run_mode ->
    # family), so a novaseq-family tumor (e.g. a "NovaSeq X 1000" vs
    # "NovaSeq 6000" split, which the patient tier rejects on the fine
    # bucket) would still be RESCUED by the novaseq pooled normal and pair --
    # making that split a non-idempotent unmatched case. A family-level
    # mismatch (hiseq vs novaseq) is rejected by both the patient tier and
    # the pooled tier, so it stays unmatched regardless of run order.
    Case(
        logic="ARGOS",
        label="C-DEV011 family mismatch (HiSeq vs NovaSeq)",
        expected="unmatched",
        tumor=_regular("DEV-011-T", "C-DEV011", "99011", "Tumor", "TUMOR",
                       "IMPACT505", "IMPACT505_BAITS",
                       preservation=["FFPE"], run_mode="HiSeq 4000"),
        normals=[_regular("DEV-011-N", "C-DEV011", "99011", "Normal", "NORMAL",
                          "IMPACT505", "IMPACT505_BAITS",
                          preservation=["FFPE"], run_mode="NovaSeq 6000")]),
    Case(
        logic="ARGOS",
        label="C-DEV012 pooled normal (diana)",
        expected="pair",
        tumor=_regular("DEV-012-T", "C-DEV012", "99012", "Tumor", "TUMOR",
                       "IMPACT505", "IMPACT505_BAITS",
                       preservation=["FFPE"], run_mode="NovaSeq 6000"),
        normals=[_regular("DEV-012-POOLED-N", None, "99012", "Pooled Normal",
                          "NORMAL", "IMPACT505", "IMPACT505_BAITS",
                          preservation=None, run_mode="diana")]),

    # ---- TEMPO (IDT_Exome_v2_FP_BAITS): coarse novaseq/hiseq buckets,
    # patient-wide (no request cascade, no pooled tier).
    Case(
        logic="TEMPO",
        label="C-DEV020 coarse match (X 1000 vs 6000)",
        expected="pair",
        tumor=_regular("DEV-020-T", "C-DEV020", "99020", "Tumor", "TUMOR",
                       "IDT_Exome_v2_FP", "IDT_Exome_v2_FP_BAITS",
                       run_mode="NovaSeq X 1000"),
        normals=[_regular("DEV-020-N", "C-DEV020", "99020", "Normal", "NORMAL",
                          "IDT_Exome_v2_FP", "IDT_Exome_v2_FP_BAITS",
                          run_mode="NovaSeq 6000")]),
    Case(
        logic="TEMPO",
        label="C-DEV021 run-mode mismatch (hiseq vs novaseq)",
        expected="unmatched",
        tumor=_regular("DEV-021-T", "C-DEV021", "99021", "Tumor", "TUMOR",
                       "IDT_Exome_v2_FP", "IDT_Exome_v2_FP_BAITS",
                       run_mode="HiSeq 4000"),
        normals=[_regular("DEV-021-N", "C-DEV021", "99021", "Normal", "NORMAL",
                          "IDT_Exome_v2_FP", "IDT_Exome_v2_FP_BAITS",
                          run_mode="NovaSeq 6000")]),

    # ---- DMP (sample_type "DMP", patient__dmp, pool_id in additional_metadata,
    # IM_ver parsed from the P-<digits>-<word>-IM<n> name): tier-1 same-pool,
    # tier-2 same-IM_ver rescue, fan-out (multi_pair).
    Case(
        logic="DMP",
        label="P-00DEV01 tier-1 (same pool)",
        expected="pair",
        tumor=_dmp("P-100001-T01-IM6", "P-00DEV01", "POOL-A1", "TUMOR", patient_cmo="C-DEV030"),
        normals=[_dmp("P-100001-N01-IM6", "P-00DEV01", "POOL-A1", "NORMAL", patient_cmo="C-DEV030")]),
    Case(
        logic="DMP",
        label="P-00DEV02 tier-2 (diff pool, same IM_ver)",
        expected="pair",
        tumor=_dmp("P-100002-T01-IM6", "P-00DEV02", "POOL-B2", "TUMOR", patient_cmo="C-DEV031"),
        normals=[_dmp("P-100002-N01-IM6", "P-00DEV02", "POOL-C2", "NORMAL", patient_cmo="C-DEV031")]),
    Case(
        logic="DMP",
        label="P-00DEV03 fan-out (two same-pool normals)",
        expected="fanout(2)",
        tumor=_dmp("P-100003-T01-IM6", "P-00DEV03", "POOL-D3", "TUMOR", patient_cmo="C-DEV032"),
        normals=[
            _dmp("P-100003-N01-IM6", "P-00DEV03", "POOL-D3", "NORMAL", patient_cmo="C-DEV032"),
            _dmp("P-100003-N02-IM6", "P-00DEV03", "POOL-D3", "NORMAL", patient_cmo="C-DEV032"),
        ]),

    # ---- HARMONY: one DMP-shaped pair triggered under the HARMONY logic.
    Case(
        logic="HARMONY",
        label="P-00HARM01 harmony pair",
        expected="pair",
        tumor=_dmp("P-100010-T01-IM6", "P-00HARM01", "POOL-H1", "TUMOR", patient_cmo="C-DEV040"),
        normals=[_dmp("P-100010-N01-IM6", "P-00HARM01", "POOL-H1", "NORMAL", patient_cmo="C-DEV040")]),
]


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_logic(db, name: str, strategy: str, description: str) -> None:
    """Idempotently ensure a pairing logic exists (lifespan parity)."""
    existing = db.scalar(select(PairingLogic).where(PairingLogic.name == name))
    if existing is None:
        db.add(PairingLogic(name=name, strategy_name=strategy,
                            description=description))
        db.commit()


def _get_logic(db, name: str):
    logic = db.scalar(select(PairingLogic).where(PairingLogic.name == name))
    if logic is None:
        raise SystemExit(f"pairing logic {name!r} not found after seeding")
    return logic


def _get_or_create_sample(db, spec: SampleSpec):
    """Return the existing sample with this primary_id, else create it."""
    existing = db.scalar(
        select(Sample).where(Sample.primary_id == spec.primary_id))
    if existing is not None:
        return existing
    sample = Sample(**spec.kwargs())
    db.add(sample)
    return sample


def _reset_fixtures(db) -> int:
    """Delete pairing rows that reference dev-fixture samples, then the
    dev-fixture samples themselves. Returns the number of samples removed."""
    fixtures = list(db.scalars(
        select(Sample).where(Sample.file_group == DEV_FILE_GROUP)).all())
    if not fixtures:
        return 0
    ids = [s.id for s in fixtures]
    pairings = list(db.scalars(select(PairingTable).where(
        or_(PairingTable.tumor_id.in_(ids),
            PairingTable.normal_id.in_(ids)))).all())
    for p in pairings:
        db.delete(p)
    for s in fixtures:
        db.delete(s)
    db.commit()
    return len(fixtures)


def _pairing_rows(db, tumor_id, logic_id) -> int:
    """Count the latest pairing rows for a tumor under a logic."""
    return len(list(db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor_id,
            PairingTable.pairing_logic_id == logic_id,
            PairingTable.latest == True,  # noqa: E712
        )).all()))


def _print_summary(results: list) -> None:
    headers = ("Logic", "Case", "Expected", "Rows", "Status")
    rows = []
    for case, actual in results:
        expected = _EXPECTED_ROWS[case.expected]
        status = "OK" if actual == expected else "MISMATCH"
        rows.append((case.logic, case.label, str(expected), str(actual),
                     status))
    widths = [max([len(h)] + [len(r[i]) for r in rows])
              for i, h in enumerate(headers)]
    print("\n" + "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())

    per_logic = defaultdict(lambda: [0, 0, 0])  # samples, rows, cases
    for case, actual in results:
        per_logic[case.logic][1] += actual
        per_logic[case.logic][2] += 1
    for spec in FIXTURES:
        per_logic[spec.logic][0] += len(spec.normals) + 1

    print("\nPer-logic totals:")
    for logic in LOGIC_ORDER:
        samples, rows_count, cases = per_logic.get(logic, (0, 0, 0))
        print(f"  {logic:<8} samples={samples:<3} "
              f"pairing_rows={rows_count:<3} cases={cases}")

    total_samples = sum(len(c.normals) + 1 for c in FIXTURES)
    total_rows = sum(actual for _, actual in results)
    total_unmatched = sum(1 for _, a in results if a == 0)
    print(f"\nTotal: {total_samples} samples, {total_rows} pairing rows, "
          f"{total_unmatched} unmatched tumors")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Idempotent dev-fixture seeder for the syzygy dev DB.")
    ap.add_argument("--reset", action="store_true",
                    help="delete dev-fixture samples + their pairing rows "
                         "before seeding")
    args = ap.parse_args(argv)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Same baseline the app lifespan seeds (pooled machines, pairing
        # logics, bait sets) so the triggers resolve as in production.
        seed_pooled_machines(db)
        _seed_logic(db, "ARGOS", "argos", "ARGOS auto-pairing")
        _seed_logic(db, "TEMPO", "tempo", "TEMPO auto-pairing")
        _seed_logic(db, "ACCESS", "default", "ACCESS auto-pairing")
        _seed_logic(db, "DMP", "dmp", "DMP two-tier auto-pairing")
        _seed_logic(db, "HARMONY", "dmp", "HARMONY auto-pairing")
        seed_bait_sets(db)

        if args.reset:
            removed = _reset_fixtures(db)
            print(f"--reset: removed {removed} dev-fixture sample(s) and "
                  f"their pairing rows")

        results = []
        for case in FIXTURES:
            # Normals first so the tumor's automatic pairing can find them.
            for spec in case.normals:
                _get_or_create_sample(db, spec)
            tumor = _get_or_create_sample(db, case.tumor)
            db.flush()
            logic = _get_logic(db, case.logic)
            trigger_pairing(db, tumor, logic, "automatic")
            results.append((case, _pairing_rows(db, tumor.id, logic.id)))

        db.commit()
        _print_summary(results)
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
