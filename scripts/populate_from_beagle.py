#!/usr/bin/env python3
"""Populate the syzygy database with pairing-validation datasets and
validate syzygy's pairing strategies against beagle-derived ground truth.

Scenarios
---------
argos (default)
    Loads the five canonical ARGOS request fixtures from beagle
    (``fixtures/tests/<name>.filemetadata.json``):

        10075_D      (5 samples: 3 tumor, 2 normal)
        10075_D_2    (1 normal)
        10075_D_3    (1 tumor)
        10075_D_4    (1 normal)
        05257_CB     (1 tumor, 1 normal)

    -> 10 unique samples, 3 patients (C-DRKHP7, C-8VK0V7, C-A0D0WK),
       5 requests, all IMPACT468 / HiSeq High Output.

    Deliberately excluded (and why):
      * 10075_D_single_TN_pair, juno_argos_demo2  -- derived fixtures that
        duplicate 10075_D_5 / 10075_D_3 with variant cmoSampleName formats;
        loading them would overwrite the canonical rows (dedup is by
        primaryId).
      * 05500_HJ -- MSK-CH bait, cell lines, no runMode/runDate; used by
        other beagle tests (argos_pdx/etl), not the ARGOS v2.3.0 pairing
        tests.
      * 08944_B  -- argos input fixture, not sample metadata.

tempo
    Beagle has no TEMPO/CHRONOS fixtures, so this scenario uses a
    hand-derived dummy dataset (TEMPO_SAMPLES below) built to exercise
    every branch of beagle's TEMPO/CHRONOS pairing logic
    (runner/operator/tempo_mpgen_operator/bin/tempo_patient.py and the
    near-identical runner/operator/chronos_operator/bin/tempo_patient.py):

      A  historical (pre_pairing) pairing preferred over a newer
         patient-wide normal  (A_T -> A_N1 is seeded as a historical
         pairing, as both came from one IGO request)
      B  latest run_date wins among matching normals (across requests)
      C  bait-set mismatch            -> tumor unpaired
      D  run-mode mismatch (bait OK)  -> tumor unpaired
      E  patient with no normals      -> tumor unpaired
      F  historical pairing bypasses bait/run-mode checks
      G  gene panel is not a pairing key in beagle TEMPO/CHRONOS

    The tempo scenario seeds the historical pairings
    (TEMPO_HISTORICAL_PAIRINGS) before triggering, mirroring beagle loading
    the previous run's pairing.tsv, and triggers with
    trigger_type="manual" so the tempo strategy's historical tier actually
    runs (an automatic trigger would return an existing pairing without
    re-running the strategy).

    Bait values are stored pre-resolved (agilent/idt/idt_v2) because
    beagle compares resolve_target(baitSet) values, not raw strings.
    15 samples, 7 patients, 13 requests; every patient is isolated so
    tiers never leak across cases.

both
    Loads both datasets and validates both.

Field mapping (beagle filemetadata -> syzygy Sample, argos scenario)
--------------------------------------------------------------------
    primaryId            -> primary_id
    cmoSampleName        -> cmo_sample_name   (fallback: ciTag, then primaryId)
    cmoPatientId         -> patient_id
    igoRequestId         -> request_id
    sampleClass|specimenType -> sample_type
    genePanel|requestMetadata.genePanel -> gene_panel
    baitSet              -> bait_set
    tumorOrNormal        -> tumor_or_normal  (Tumor->TUMOR, Normal->NORMAL)
    runMode              -> run_mode
    preservation         -> preservation
    sampleOrigin         -> sample_origin
    runDate              -> sequencing_date  (UTC midnight)
    misc                 -> additional_metadata

beagle stores R1/R2 fastq rows per sample (and, for 05257_CB, latest=True
rows without cmoSampleName alongside latest=False rows without primaryId);
rows are merged into one record per sample, first non-null value wins, and
the 05257_CB anonymous rows are merged by (igoRequestId, tumorOrNormal)
when unambiguous.

Pairing trigger
---------------
All samples are inserted first, then trigger_pairing is run per tumor:
"automatic" for argos, "manual" for tempo (beagle's TEMPO/CHRONOS
recomputes pairing on every run, consulting the previous run's pairing.tsv
as pre_pairing -- which is exactly what the tempo strategy's historical
tier reads). This mirrors beagle's on-demand compile_pairs against the full
fixture set (beagle computes pairing at query time over all loaded
fixtures; syzygy triggers at ingest time, so ingest order can affect
cross-request winners -- triggering over the complete dataset is the
order-independent comparison basis).

Expected pairs, argos scenario (ground truth from beagle v2.3.0 tests,
runner/tests/operator/argos_operator/v2_3_0/test_pair_request.py, with the
merged five-fixture dataset):

    10075_D_1   -> 10075_D_2    (test_compile_pairs4/5: same-request normal)
    10075_D_4   -> 10075_D_2    (same-request normal, patient C-DRKHP7)
    10075_D_5   -> 10075_D_3    (same-request normal, patient C-8VK0V7)
    10075_D_3_5 -> 10075_D_4_3  (test_get_most_recent_normal1: latest
                                 run_date 2019-12-13 wins across requests)
    05257_CB_1  -> 05257_CB_2   (same-request normal, patient C-A0D0WK)
    10075_D_2_3 -> unpaired     (no tumor selects it; D_3_5 takes D_4_3)

Expected pairs, tempo scenario (ground truth derived from beagle's
tempo_patient.py pairing rules -- pre_pairing first, then bait_set AND
run_mode match, latest run_date wins):

    TMP-A_T -> TMP-A_N1, TMP-B_T -> TMP-B_N1,
    TMP-F_T -> TMP-F_N,  TMP-G_T -> TMP-G_N;
    unpaired tumors: TMP-C_T, TMP-D_T, TMP-E_T
    unpaired normals: TMP-A_N2, TMP-B_N2, TMP-C_N, TMP-D_N

Usage
-----
    DATABASE_URL="postgresql+psycopg2://app:app@127.0.0.1:5434/app" \
        python scripts/populate_from_beagle.py [--scenario argos|tempo|both]

Note: the syzygy db container is published on host port 5434 (5432 is
shadowed by a native Homebrew postgres on this machine; 5433 is beagle's
postgres). See docker-compose.yml.

Idempotent: wipes samples/pairings/pairing_logic, re-seeds logics
(argos -> argos strategy; TEMPO/CHRONOS -> tempo strategy; IMPACT/ACCESS
-> default strategy, as main.py lifespan does), seeds the tempo
historical pairings, loads the selected dataset(s), retriggers pairing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

DEFAULT_BEAGLE_FIXTURES = (
    "/Users/bolipatc/Library/CloudStorage/"
    "OneDrive-MemorialSloanKetteringCancerCenter/work/dev/"
    "beagle_allan_fork/fixtures/tests"
)
CANONICAL_FIXTURES = ["10075_D", "10075_D_2", "10075_D_3", "10075_D_4", "05257_CB"]

# Ground truth: beagle v2.3.0 test_pair_request.py, merged five-fixture DB.
EXPECTED_PAIRS: dict[str, tuple[str, str]] = {
    "10075_D_1": ("10075_D_2", "same-request normal (test_compile_pairs4/5)"),
    "10075_D_4": ("10075_D_2", "same-request normal (patient C-DRKHP7)"),
    "10075_D_5": ("10075_D_3", "same-request normal (patient C-8VK0V7)"),
    "10075_D_3_5": ("10075_D_4_3", "latest run_date across requests (test_get_most_recent_normal1)"),
    "05257_CB_1": ("05257_CB_2", "same-request normal (patient C-A0D0WK)"),
}
EXPECTED_UNPAIRED_NORMALS = {
    "10075_D_2_3": "no tumor selects it in the merged dataset",
}

# ---------------------------------------------------------------------------
# TEMPO dummy dataset (derived from beagle's TEMPO/CHRONOS pairing logic;
# beagle has no fixtures for it). One isolated patient per case letter.
# ---------------------------------------------------------------------------
TEMPO_SAMPLES: list[dict] = [
    # Case A: historical/same-request pairing beats a newer patient-wide normal.
    dict(primary_id="TMP-A_T",  patient_id="C-TMP-A", request_id="TMP-REQ-A1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2018-05-01", case="A"),
    dict(primary_id="TMP-A_N1", patient_id="C-TMP-A", request_id="TMP-REQ-A1",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2018-05-01", case="A"),
    dict(primary_id="TMP-A_N2", patient_id="C-TMP-A", request_id="TMP-REQ-A2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2020-01-01", case="A"),
    # Case B: latest run_date wins among matching normals (across requests).
    dict(primary_id="TMP-B_T",  patient_id="C-TMP-B", request_id="TMP-REQ-B1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2019-03-01", case="B"),
    dict(primary_id="TMP-B_N1", patient_id="C-TMP-B", request_id="TMP-REQ-B2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2019-06-01", case="B"),
    dict(primary_id="TMP-B_N2", patient_id="C-TMP-B", request_id="TMP-REQ-B3",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2018-07-01", case="B"),
    # Case C: bait-set mismatch -> unpaired.
    dict(primary_id="TMP-C_T", patient_id="C-TMP-C", request_id="TMP-REQ-C1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="idt_v2",
         run_mode="HiSeq 4000", sequencing_date="2019-09-01", case="C"),
    dict(primary_id="TMP-C_N", patient_id="C-TMP-C", request_id="TMP-REQ-C2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2019-09-15", case="C"),
    # Case D: run-mode mismatch (bait matches) -> unpaired in beagle.
    dict(primary_id="TMP-D_T", patient_id="C-TMP-D", request_id="TMP-REQ-D1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-02-01", case="D"),
    dict(primary_id="TMP-D_N", patient_id="C-TMP-D", request_id="TMP-REQ-D2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="NovaSeq X 1000", sequencing_date="2020-03-01", case="D"),
    # Case E: patient with no normals -> unpaired.
    dict(primary_id="TMP-E_T", patient_id="C-TMP-E", request_id="TMP-REQ-E1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-04-01", case="E"),
    # Case F: same request (historical pairing) bypasses bait/run-mode checks.
    dict(primary_id="TMP-F_T", patient_id="C-TMP-F", request_id="TMP-REQ-F1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-05-01", case="F"),
    dict(primary_id="TMP-F_N", patient_id="C-TMP-F", request_id="TMP-REQ-F1",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2020-05-10", case="F"),
    # Case G: gene panel is not a pairing key in beagle TEMPO/CHRONOS.
    dict(primary_id="TMP-G_T", patient_id="C-TMP-G", request_id="TMP-REQ-G1",
         sample_type="Tumor",  gene_panel="TempoWES", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-06-01", case="G"),
    dict(primary_id="TMP-G_N", patient_id="C-TMP-G", request_id="TMP-REQ-G2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-06-10", case="G"),
]

# Ground truth derived from beagle tempo_patient.py (pre_pairing first, then
# bait_set AND run_mode, latest run_date wins; gene panel ignored).
TEMPO_EXPECTED_PAIRS: dict[str, tuple[str, str]] = {
    "TMP-A_T": ("TMP-A_N1", "historical (pre_pairing) pairing preferred over newer patient-wide normal"),
    "TMP-B_T": ("TMP-B_N1", "latest run_date among matching bait+run_mode normals"),
    "TMP-F_T": ("TMP-F_N", "pre_pairing bypasses bait/run-mode checks in beagle"),
    "TMP-G_T": ("TMP-G_N", "gene panel is not a pairing key in beagle TEMPO/CHRONOS"),
}
TEMPO_EXPECTED_UNPAIRED_TUMORS = {
    "TMP-C_T": "no normal with same bait set in patient",
    "TMP-D_T": "no normal with same bait set AND run mode in patient",
    "TMP-E_T": "no normals for patient",
}
TEMPO_EXPECTED_UNPAIRED_NORMALS = {
    "TMP-A_N2": "newer normal not selected (A_T takes same-request A_N1)",
    "TMP-B_N2": "older normal not selected (B_T takes B_N1)",
    "TMP-C_N": "bait mismatch with the only tumor in patient",
    "TMP-D_N": "run-mode mismatch with the only tumor in patient",
}
# Historical (pre_pairing) pairings to seed before the tempo trigger,
# mirroring beagle loading the previous run's pairing.tsv. Case A's
# tumor and normal came from the same IGO request (TMP-REQ-A1), so a
# prior run would have paired them; case F likewise (TMP-REQ-F1) -- with
# bait and run mode intentionally different, proving the historical
# tier bypasses those checks.
TEMPO_HISTORICAL_PAIRINGS: dict[str, str] = {
    "TMP-A_T": "TMP-A_N1",
    "TMP-F_T": "TMP-F_N",
}

ADDITIONAL_METADATA_KEYS = (
    "runId", "flowCellId", "platform", "sequencingCenter", "readLength",
    "species", "sex", "oncotreeCode", "libraryIgoId", "externalSampleId", "R",
)

REQUIRED_NON_NULL = ("cmo_sample_name", "patient_id", "request_id",
                     "tumor_or_normal", "gene_panel", "bait_set")


def load_fixture_rows(fixtures_dir: str, names: list[str]) -> list[dict]:
    rows = []
    for name in names:
        path = os.path.join(fixtures_dir, f"{name}.filemetadata.json")
        with open(path) as f:
            for row in json.load(f):
                rows.append(row["fields"]["metadata"])
    return rows


def merge_samples(rows: list[dict]) -> tuple[dict[str, dict], int]:
    """Group filemetadata rows into one record per sample (keyed by primaryId).

    Rows lacking primaryId (05257_CB latest=False rows) are merged into the
    same-request sample with the matching tumorOrNormal, when unambiguous.
    Returns (samples keyed by primaryId, count of anonymous rows merged).
    """
    samples: dict[str, dict] = {}
    anon: list[dict] = []
    for m in rows:
        pid = m.get("primaryId")
        if pid:
            s = samples.setdefault(pid, {})
            for k, v in m.items():
                if v not in (None, "") and s.get(k) in (None, ""):
                    s[k] = v
        else:
            anon.append(m)

    merged_anon = 0
    for m in anon:
        cands = [
            s for s in samples.values()
            if s.get("igoRequestId") == m.get("igoRequestId")
            and s.get("tumorOrNormal") == m.get("tumorOrNormal")
        ]
        if len(cands) == 1:
            for k, v in m.items():
                if v not in (None, "") and cands[0].get(k) in (None, ""):
                    cands[0][k] = v
            merged_anon += 1
        else:
            print(f"WARNING: unmergeable anonymous row {m.get('cmoSampleName')!r} "
                  f"({len(cands)} candidate groups)")
    return samples, merged_anon


def to_sample_kwargs(pid: str, m: dict) -> dict:
    gene_panel = m.get("genePanel") or (m.get("requestMetadata") or {}).get("genePanel")
    sample_type = m.get("sampleClass") or m.get("specimenType") or m.get("sampleType")
    seq_date = None
    rd = m.get("runDate")
    if rd:
        seq_date = datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    additional = {
        k: m[k] for k in ADDITIONAL_METADATA_KEYS if m.get(k) is not None
    }
    return dict(
        primary_id=pid,
        cmo_sample_name=m.get("cmoSampleName") or m.get("ciTag") or pid,
        patient_id=m.get("cmoPatientId"),
        request_id=m.get("igoRequestId"),
        sample_type=sample_type,
        gene_panel=gene_panel,
        bait_set=m.get("baitSet"),
        tumor_or_normal={"Tumor": "TUMOR", "Normal": "NORMAL"}[m["tumorOrNormal"]],
        run_mode=m.get("runMode"),
        preservation=m.get("preservation"),
        sample_origin=m.get("sampleOrigin"),
        sequencing_date=seq_date,
        additional_metadata=additional or None,
        errors=None,
    )


def to_tempo_sample_kwargs(r: dict) -> dict:
    return dict(
        primary_id=r["primary_id"],
        cmo_sample_name=r["primary_id"],
        patient_id=r["patient_id"],
        request_id=r["request_id"],
        sample_type=r["sample_type"],
        gene_panel=r["gene_panel"],
        bait_set=r["bait_set"],
        tumor_or_normal="TUMOR" if r["sample_type"] == "Tumor" else "NORMAL",
        run_mode=r["run_mode"],
        preservation=None,
        sample_origin=None,
        sequencing_date=datetime.strptime(r["sequencing_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
        additional_metadata={"source": "tempo-dummy", "beagle_case": r["case"]},
        errors=None,
    )


def _trigger(db, samples: list, logic, label: str, trigger_type: str = "automatic") -> int:
    from models import Sample
    pair_count = 0
    for s in samples:
        if s.tumor_or_normal != "TUMOR":
            continue
        p = trigger_pairing(db, s, logic, trigger_type)
        if p is not None:
            pair_count += 1
            paired_normal = db.get(Sample, p.normal_id)
            print(f"paired    [{label}] {s.primary_id:>12} (tumor)  <->  "
                  f"{paired_normal.primary_id if paired_normal else '?'} (normal)  "
                  f"notes={p.notes}")
        else:
            print(f"unpaired  [{label}] {s.primary_id:>12} (tumor)  <->  <none>")
    return pair_count


def main() -> int:
    global trigger_pairing
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures-dir", default=DEFAULT_BEAGLE_FIXTURES,
                    help="beagle fixtures/tests directory")
    ap.add_argument("--scenario", choices=["argos", "tempo", "both"], default="argos",
                    help="dataset(s) to load and validate")
    ap.add_argument("--skip-validate", action="store_true",
                    help="load data only; do not compare against expected pairs")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)

    from database import Base, SessionLocal, engine
    from models import PairingLogic, PairingTable, Sample
    from pairing_service import trigger_pairing  # noqa: F811  (rebind global)

    want_argos = args.scenario in ("argos", "both")
    want_tempo = args.scenario in ("tempo", "both")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    exit_code = 1
    try:
        # Idempotent reset of the dev database.
        db.query(PairingTable).delete()
        db.query(Sample).delete()
        db.query(PairingLogic).delete()
        db.commit()

        seed = {
            "argos": ("argos", "ARGOS (WES) T/N pairing aligned with beagle v2.3.0"),
            "TEMPO": ("tempo", "TEMPO_MPGEN T/N pairing aligned with beagle tempo logic"),
            "CHRONOS": ("tempo", "CHRONOS T/N pairing aligned with beagle tempo logic"),
            "IMPACT": ("default", None),
            "ACCESS": ("default", None),
        }
        logics: dict[str, PairingLogic] = {}
        for name, (strategy, desc) in seed.items():
            pl = PairingLogic(name=name, strategy_name=strategy, description=desc)
            db.add(pl)
            logics[name] = pl
        db.commit()
        for pl in logics.values():
            db.refresh(pl)

        argos_samples: list[Sample] = []
        tempo_samples: list[Sample] = []

        if want_argos:
            rows = load_fixture_rows(args.fixtures_dir, CANONICAL_FIXTURES)
            samples, merged_anon = merge_samples(rows)
            print(f"[argos] Parsed {len(rows)} filemetadata rows -> "
                  f"{len(samples)} unique samples ({merged_anon} anonymous rows merged)")
            for pid in sorted(samples):
                kwargs = to_sample_kwargs(pid, samples[pid])
                missing = [k for k in REQUIRED_NON_NULL if kwargs.get(k) is None]
                if missing:
                    print(f"NOTE: {pid} has null required fields: {missing}")
                s = Sample(**kwargs)
                db.add(s)
                db.commit()
                db.refresh(s)
                argos_samples.append(s)

        if want_tempo:
            for r in TEMPO_SAMPLES:
                s = Sample(**to_tempo_sample_kwargs(r))
                db.add(s)
                db.commit()
                db.refresh(s)
                tempo_samples.append(s)
            by_pid = {s.primary_id: s for s in tempo_samples}
            for tumor_pid, normal_pid in TEMPO_HISTORICAL_PAIRINGS.items():
                db.add(PairingTable(
                    tumor_id=by_pid[tumor_pid].id,
                    normal_id=by_pid[normal_pid].id,
                    pairing_logic_id=logics["TEMPO"].id,
                    trigger_type="historical",
                    latest=True,
                ))
            db.commit()
            print(f"[tempo] Loaded {len(tempo_samples)} dummy samples "
                  f"(7 patients, 13 requests; cases A-G) + "
                  f"{len(TEMPO_HISTORICAL_PAIRINGS)} historical pairings")

        if want_argos:
            pair_count = _trigger(db, argos_samples, logics["argos"], "argos")
            print(f"[argos] Loaded {len(argos_samples)} samples, created {pair_count} pairings.")
        if want_tempo:
            pair_count = _trigger(db, tempo_samples, logics["TEMPO"], "tempo",
                                  trigger_type="manual")
            print(f"[tempo] Loaded {len(tempo_samples)} samples, created {pair_count} pairings.")
        db.commit()

        if args.skip_validate:
            print("Validation skipped (--skip-validate).")
            exit_code = 0
        else:
            exit_code = 0
            if want_argos:
                exit_code |= validate(db, argos_samples, logics["argos"])
            if want_tempo:
                exit_code |= validate_tempo(db, tempo_samples, logics["TEMPO"])
    finally:
        db.close()
        engine.dispose()
    return exit_code


def validate(db, created: list, argos) -> int:
    from models import PairingTable, Sample

    actual: dict[str, str | None] = {}
    for s in created:
        if s.tumor_or_normal != "TUMOR":
            continue
        p = db.query(PairingTable).filter_by(
            tumor_id=s.id, pairing_logic_id=argos.id, latest=True
        ).first()
        normal = db.get(Sample, p.normal_id) if p else None
        actual[s.primary_id] = normal.primary_id if normal else None

    print("\n--- Validation: syzygy argos strategy vs beagle v2.3.0 expectations ---")
    failures = 0
    for tumor_pid, (expected_pid, why) in EXPECTED_PAIRS.items():
        got = actual.get(tumor_pid)
        ok = got == expected_pid
        failures += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] tumor {tumor_pid:>12} -> {got} "
              f"(expected {expected_pid})  # {why}")

    paired_normals = {v for v in actual.values() if v is not None}
    for pid, why in EXPECTED_UNPAIRED_NORMALS.items():
        ok = pid not in paired_normals
        failures += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] normal  {pid:>12} {'unpaired' if ok else 'unexpectedly paired: ' + str(paired_normals)}  # {why}")

    unexpected_unpaired_tumors = [t for t in actual if actual[t] is None]
    for t in unexpected_unpaired_tumors:
        print(f"  [NOTE] tumor {t} unpaired (not covered by expectations)")

    total = len(EXPECTED_PAIRS) + len(EXPECTED_UNPAIRED_NORMALS)
    print(f"\n{total - failures}/{total} expectations met.")
    return 0 if failures == 0 else 1


def validate_tempo(db, tempo_samples: list, tempo_logic) -> int:
    """Compare syzygy TEMPO (tempo strategy) results against the ground
    truth derived from beagle's TEMPO/CHRONOS pairing logic.
    Exit 1 on any mismatch.
    """
    from models import PairingTable, Sample

    actual: dict[str, str | None] = {}
    for s in tempo_samples:
        if s.tumor_or_normal != "TUMOR":
            continue
        p = db.query(PairingTable).filter_by(
            tumor_id=s.id, pairing_logic_id=tempo_logic.id, latest=True
        ).first()
        normal = db.get(Sample, p.normal_id) if p else None
        actual[s.primary_id] = normal.primary_id if normal else None

    print("\n--- Validation: syzygy TEMPO (tempo strategy) vs beagle TEMPO/CHRONOS expectations ---")
    n_pass = n_fail = 0

    for tumor, (expected, why) in TEMPO_EXPECTED_PAIRS.items():
        got = actual.get(tumor)
        if got == expected:
            n_pass += 1
            print(f"  [PASS]    tumor {tumor:>9} -> {got}  # {why}")
        else:
            n_fail += 1
            print(f"  [FAIL]    tumor {tumor:>9} -> {got} (expected {expected})  # {why}")

    for tumor, why in TEMPO_EXPECTED_UNPAIRED_TUMORS.items():
        got = actual.get(tumor)
        if got is None:
            n_pass += 1
            print(f"  [PASS]    tumor {tumor:>9} -> unpaired  # {why}")
        else:
            n_fail += 1
            print(f"  [FAIL]    tumor {tumor:>9} -> {got} (expected unpaired)  # {why}")

    for pid, why in TEMPO_EXPECTED_UNPAIRED_NORMALS.items():
        paired_by = [t for t, v in actual.items() if v == pid]
        if not paired_by:
            n_pass += 1
            print(f"  [PASS]    normal  {pid:>9} unpaired  # {why}")
        else:
            n_fail += 1
            print(f"  [FAIL]    normal  {pid:>9} unexpectedly paired by {', '.join(paired_by)}  # {why}")

    total = n_pass + n_fail
    print(f"\n{n_pass}/{total} expectations match beagle ground truth.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
