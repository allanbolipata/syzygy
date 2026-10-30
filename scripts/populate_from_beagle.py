#!/usr/bin/env python3
"""Populate the syzygy database with pairing-validation datasets and
validate syzygy's pairing strategies against ground truth.

Scenarios
---------
argos
    Loads the canonical ARGOS request fixtures from a local beagle checkout
    (``<fixtures_dir>/<name>.filemetadata.json``), merges them into one
    sample per primaryId, and validates syzygy's argos strategy against the
    expected tumor/normal pairings.

    The fixture names, expected pairs, and fixture directory reference real
    beagle identifiers, so they are NOT committed to this repo. They are read
    from the local, gitignored file ``scripts/.beagle_ground_truth.json``
    (shape shown in ``scripts/.beagle_ground_truth.example.json``). Without
    that file the argos scenario cannot run.

tempo
    Beagle has no TEMPO/CHRONOS fixtures, so this scenario is fully
    self-contained (no external files). It uses a hand-derived dummy dataset
    (TEMPO_SAMPLES below) built to exercise every branch of beagle's
    TEMPO/CHRONOS pairing logic
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

dmp
    Loads a local beagle DMP fixture (a ``DMP_data.json`` dump) and
    translates the DMP-schema records into Sample rows via
    dmp_translator.to_dmp_sample_kwargs:

        sample          -> primary_id
        external_id     -> cmo_sample_name
        patient.cmo     -> patient__cmo ("C-" prefixed)
        patient.dmp     -> patient__dmp (as-is, no prefix)
        anon_id         -> anon_id
        cmo_assay       -> gene_panel (fallback: ASSAY_MAPPING[assay])
        type T/N        -> tumor_or_normal (TUMOR / NORMAL)
        "DMP"           -> sample_type (provenance marker)
        every other key -> additional_metadata (verbatim under the
                           original key, null/empty skipped; the DMP
                           "sample_type" key is a 1/0 metastasis flag,
                           stored under its original name)

    The ENTIRE DMP record is stored: only the fields mapped to columns
    are excluded from additional_metadata. The DMP fixture's sample and
    patient ids are real beagle identifiers, so the expected DMP results
    (expected pairs, which tumors must stay unpaired, plus translation
    spot-checks) are read from the local
    ``scripts/.beagle_ground_truth.json`` file, not committed here.
    Pairing is triggered under the DMP logic (auto-pairing is DMP-group
    only, keyed on patient__dmp + gene_panel), so each DMP tumor pairs
    with its same-patient DMP normal when one exists in the fixture, and
    a tumor with no same-patient__dmp same-gene-panel counterpart stays
    unpaired. Manual DMP pairing still exists (operator pins a DMP
    normal via POST /pairing/trigger with an explicit normal_id) and
    takes precedence over automatic pairing.

both / all
    Load and validate the selected combination of the above.

Field mapping (beagle filemetadata -> syzygy Sample, argos scenario)
--------------------------------------------------------------------
    primaryId            -> primary_id
    cmoSampleName        -> cmo_sample_name   (fallback: ciTag, then primaryId)
    cmoPatientId         -> patient__cmo
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

    beagle stores R1/R2 fastq rows per sample, and some fixtures hold
    latest=False rows without a primaryId alongside latest=True rows; rows
    are merged into one record per sample, first non-null value wins, and
    anonymous rows are merged by (igoRequestId, tumorOrNormal) when
    unambiguous.

Pairing trigger
---------------
All samples are first inserted, then trigger_pairing is run per tumor:
"automatic" for argos, "automatic" for dmp (under the DMP logic),
"manual" for tempo (beagle's TEMPO/CHRONOS
recomputes pairing on every run, consulting the previous run's pairing.tsv
as pre_pairing -- which is exactly what the tempo strategy's historical
tier reads). This mirrors beagle's on-demand compile_pairs against the full
fixture set (beagle computes pairing at query time over all loaded
fixtures; syzygy triggers at ingest time, so ingest order can affect
cross-request winners -- triggering over the complete dataset is the
order-independent comparison basis).

Expected pairs
--------------
    tempo: the TEMPO_EXPECTED_PAIRS constant (dummy data) below.
    argos: loaded from the local ground-truth file (see above).
    dmp:   loaded from the local ground-truth file (see above):
           EXPECTED_DMP_PAIRS (tumor -> [normal, why]) for DMP tumors
           that DO have a same-patient__dmp same-gene-panel counterpart
           in the fixture, and EXPECTED_DMP_UNPAIRED (tumor -> reason)
           for DMP tumors that have no such counterpart.

Usage
-----
    Export DATABASE_URL for your local syzygy db, then:

        python scripts/populate_from_beagle.py --scenario tempo   # self-contained
        python scripts/populate_from_beagle.py --scenario argos   # needs scripts/.beagle_ground_truth.json
        python scripts/populate_from_beagle.py --scenario dmp     # needs scripts/.beagle_ground_truth.json

Idempotent: wipes samples/pairings/pairing_logic, re-seeds logics
(ARGOS/IMPACT -> argos strategy; TEMPO/CHRONOS -> tempo strategy; DMP
-> dmp strategy; ACCESS -> default strategy, as main.py lifespan does),
seeds the tempo
historical pairings, loads the selected dataset(s), retriggers pairing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# The argos and dmp scenarios validate against REAL beagle fixtures, so their
# ground truth (fixture names, expected pairs, fixture paths) references real
# beagle identifiers. To keep those out of the public repo, they live in a
# local, gitignored file: scripts/.beagle_ground_truth.json (shape shown in
# scripts/.beagle_ground_truth.example.json). The tempo scenario is fully
# self-contained (TEMPO_SAMPLES below) and needs no such file.
LOCAL_GROUND_TRUTH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".beagle_ground_truth.json"
)

# Populated by load_ground_truth() from the local file (empty until then).
CANONICAL_FIXTURES: list[str] = []
EXPECTED_PAIRS: dict[str, tuple[str, str]] = {}
EXPECTED_UNPAIRED_NORMALS: dict[str, str] = {}
EXPECTED_DMP_UNPAIRED: dict[str, str] = {}
EXPECTED_DMP_PAIRS: dict[str, tuple[str, str]] = {}
DMP_CHECKS: list[tuple[str, str, str, str, str | None, str | None]] = []
BEAGLE_FIXTURES_DIR: str | None = None
DMP_FIXTURE_PATH: str | None = None


def load_ground_truth() -> bool:
    """Populate the argos/dmp ground-truth globals from the local file.

    Returns True if the file was found and parsed. A missing file is not an
    error by itself: the tempo scenario runs fine without it, and main()
    reports a clear message if argos/dmp is requested without it.
    """
    global CANONICAL_FIXTURES, EXPECTED_PAIRS, EXPECTED_UNPAIRED_NORMALS, \
        EXPECTED_DMP_UNPAIRED, EXPECTED_DMP_PAIRS, DMP_CHECKS, \
        BEAGLE_FIXTURES_DIR, DMP_FIXTURE_PATH
    if not os.path.exists(LOCAL_GROUND_TRUTH):
        return False
    try:
        with open(LOCAL_GROUND_TRUTH) as f:
            gt = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"ERROR: could not read {LOCAL_GROUND_TRUTH}: {e}\n"
            "Copy scripts/.beagle_ground_truth.example.json to "
            "scripts/.beagle_ground_truth.json and fill in your local values."
        )
    CANONICAL_FIXTURES = list(gt.get("canonical_fixtures", []))
    EXPECTED_PAIRS = {k: tuple(v) for k, v in gt.get("expected_pairs", {}).items()}
    EXPECTED_UNPAIRED_NORMALS = dict(gt.get("expected_unpaired_normals", {}))
    EXPECTED_DMP_UNPAIRED = dict(gt.get("expected_dmp_unpaired", {}))
    EXPECTED_DMP_PAIRS = {k: tuple(v) for k, v in gt.get("expected_dmp_pairs", {}).items()}
    DMP_CHECKS = [tuple(c) for c in gt.get("dmp_checks", [])]
    BEAGLE_FIXTURES_DIR = gt.get("fixtures_dir")
    DMP_FIXTURE_PATH = gt.get("dmp_fixture")
    return True

# ---------------------------------------------------------------------------
# TEMPO dummy dataset (derived from beagle's TEMPO/CHRONOS pairing logic;
# beagle has no fixtures for it). One isolated patient per case letter.
# ---------------------------------------------------------------------------
TEMPO_SAMPLES: list[dict] = [
    # Case A: historical/same-request pairing beats a newer patient-wide normal.
    dict(primary_id="TMP-A_T",  patient__cmo="C-TMP-A", request_id="TMP-REQ-A1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2018-05-01", case="A"),
    dict(primary_id="TMP-A_N1", patient__cmo="C-TMP-A", request_id="TMP-REQ-A1",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2018-05-01", case="A"),
    dict(primary_id="TMP-A_N2", patient__cmo="C-TMP-A", request_id="TMP-REQ-A2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000",   sequencing_date="2020-01-01", case="A"),
    # Case B: latest run_date wins among matching normals (across requests).
    dict(primary_id="TMP-B_T",  patient__cmo="C-TMP-B", request_id="TMP-REQ-B1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2019-03-01", case="B"),
    dict(primary_id="TMP-B_N1", patient__cmo="C-TMP-B", request_id="TMP-REQ-B2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2019-06-01", case="B"),
    dict(primary_id="TMP-B_N2", patient__cmo="C-TMP-B", request_id="TMP-REQ-B3",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2018-07-01", case="B"),
    # Case C: bait-set mismatch -> unpaired.
    dict(primary_id="TMP-C_T", patient__cmo="C-TMP-C", request_id="TMP-REQ-C1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="idt_v2",
         run_mode="HiSeq 4000", sequencing_date="2019-09-01", case="C"),
    dict(primary_id="TMP-C_N", patient__cmo="C-TMP-C", request_id="TMP-REQ-C2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2019-09-15", case="C"),
    # Case D: run-mode mismatch (bait matches) -> unpaired in beagle.
    dict(primary_id="TMP-D_T", patient__cmo="C-TMP-D", request_id="TMP-REQ-D1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-02-01", case="D"),
    dict(primary_id="TMP-D_N", patient__cmo="C-TMP-D", request_id="TMP-REQ-D2",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="agilent",
         run_mode="NovaSeq X 1000", sequencing_date="2020-03-01", case="D"),
    # Case E: patient with no normals -> unpaired.
    dict(primary_id="TMP-E_T", patient__cmo="C-TMP-E", request_id="TMP-REQ-E1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-04-01", case="E"),
    # Case F: same request (historical pairing) bypasses bait/run-mode checks.
    dict(primary_id="TMP-F_T", patient__cmo="C-TMP-F", request_id="TMP-REQ-F1",
         sample_type="Tumor",  gene_panel="IMPACT468", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-05-01", case="F"),
    dict(primary_id="TMP-F_N", patient__cmo="C-TMP-F", request_id="TMP-REQ-F1",
         sample_type="Normal", gene_panel="IMPACT468", bait_set="idt",
         run_mode="NovaSeq 6000", sequencing_date="2020-05-10", case="F"),
    # Case G: gene panel is not a pairing key in beagle TEMPO/CHRONOS.
    dict(primary_id="TMP-G_T", patient__cmo="C-TMP-G", request_id="TMP-REQ-G1",
         sample_type="Tumor",  gene_panel="TempoWES", bait_set="agilent",
         run_mode="HiSeq 4000", sequencing_date="2020-06-01", case="G"),
    dict(primary_id="TMP-G_N", patient__cmo="C-TMP-G", request_id="TMP-REQ-G2",
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

REQUIRED_NON_NULL = ("cmo_sample_name", "patient__cmo", "request_id",
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

    Rows lacking primaryId (some latest=False rows) are merged into the
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


def load_dmp_rows(path: str) -> tuple[list[dict], dict[str, dict], int]:
    """Parse a beagle DMP dump into unique DMP records keyed by ``sample``.

    Returns (all_records, samples, duplicate_count). Duplicate records
    (the same ``sample`` id appearing in multiple file groups) are
    merged, first non-null value wins.
    """
    with open(path) as f:
        dump = json.load(f)
    records = [
        obj["fields"]["metadata"]
        for obj in dump
        if obj.get("model") == "file_system.filemetadata"
    ]
    samples: dict[str, dict] = {}
    dupes = 0
    for meta in records:
        sid = meta.get("sample")
        if not sid:
            continue
        if sid in samples:
            dupes += 1
            for k, v in meta.items():
                if v not in (None, "") and samples[sid].get(k) in (None, ""):
                    samples[sid][k] = v
        else:
            samples[sid] = dict(meta)
    return records, samples, dupes


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
        patient__cmo=m.get("cmoPatientId"),
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
        patient__cmo=r["patient__cmo"],
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
    have_gt = load_ground_truth()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures-dir", default=BEAGLE_FIXTURES_DIR,
                    help="beagle fixtures/tests directory")
    ap.add_argument("--dmp-fixture", default=DMP_FIXTURE_PATH,
                    help="beagle DMP_data.json path")
    ap.add_argument("--scenario", choices=["argos", "tempo", "dmp", "both", "all"],
                    default="argos",
                    help="dataset(s) to load and validate")
    ap.add_argument("--skip-validate", action="store_true",
                    help="load data only; do not compare against expected pairs")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)

    from database import Base, SessionLocal, engine
    from dmp_translator import to_dmp_sample_kwargs
    from models import PairingLogic, PairingTable, Sample
    from pairing_service import trigger_pairing  # noqa: F811  (rebind global)

    want_argos = args.scenario in ("argos", "both", "all")
    want_tempo = args.scenario in ("tempo", "both", "all")
    want_dmp = args.scenario in ("dmp", "all")

    if (want_argos or want_dmp) and not have_gt:
        print(
            "ERROR: the argos/dmp scenarios validate against real beagle "
            "fixtures, so they need the local, gitignored ground-truth file "
            "scripts/.beagle_ground_truth.json (fixture names, expected "
            "pairs, fixture paths). Copy "
            "scripts/.beagle_ground_truth.example.json to "
            "scripts/.beagle_ground_truth.json and fill in your local beagle "
            "values. The tempo scenario is self-contained and needs no "
            "such file.",
            file=sys.stderr,
        )
        return 1

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
            "ARGOS": ("argos", "ARGOS/IMPACT (WES) T/N pairing aligned with beagle v2.3.0"),
            "TEMPO": ("tempo", "TEMPO/CHRONOS (WES) T/N pairing aligned with beagle tempo logic"),
            "DMP": ("dmp", "DMP (DMP) T/N pairing; patient__dmp + gene_panel hard keys"),
            "ACCESS": ("default", "ACCESS (cfDNA) T/N pairing; reference default strategy"),
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
        dmp_samples: list[Sample] = []
        dmp_records: list[dict] = []
        dmp: dict[str, dict] = {}
        dupes = 0

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

        if want_dmp:
            dmp_records, dmp, dupes = load_dmp_rows(args.dmp_fixture)
            print(f"[dmp] Parsed {len(dmp_records)} DMP records -> "
                  f"{len(dmp)} unique samples ({dupes} duplicates merged)")
            for sid in sorted(dmp):
                kwargs = to_dmp_sample_kwargs(sid, dmp[sid])
                s = Sample(**kwargs)
                db.add(s)
                db.commit()
                db.refresh(s)
                dmp_samples.append(s)

        if want_argos:
            pair_count = _trigger(db, argos_samples, logics["ARGOS"], "argos")
            print(f"[argos] Loaded {len(argos_samples)} samples, created {pair_count} pairings.")
        if want_tempo:
            pair_count = _trigger(db, tempo_samples, logics["TEMPO"], "tempo",
                                  trigger_type="manual")
            print(f"[tempo] Loaded {len(tempo_samples)} samples, created {pair_count} pairings.")
        if want_dmp:
            pair_count = _trigger(db, dmp_samples, logics["DMP"], "dmp")
            print(f"[dmp] Loaded {len(dmp_samples)} samples, created {pair_count} pairings.")
        db.commit()

        if args.skip_validate:
            print("Validation skipped (--skip-validate).")
            exit_code = 0
        else:
            exit_code = 0
            if want_argos:
                exit_code |= validate(db, argos_samples, logics["ARGOS"])
            if want_tempo:
                exit_code |= validate_tempo(db, tempo_samples, logics["TEMPO"])
            if want_dmp:
                exit_code |= validate_dmp(db, dmp_samples, dmp)
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


def validate_dmp(db, dmp_samples: list, source_records: dict[str, dict]) -> int:
    """Validate the DMP ingestion scenario.

    * Pairing (DMP logic): tumors in EXPECTED_DMP_PAIRS are paired with
      the listed normal; tumors in EXPECTED_DMP_UNPAIRED stay unpaired
      (no same-patient__dmp same-gene-panel counterpart in the fixture).
    * Completeness: the ENTIRE DMP record is stored — each sample's
      patient__cmo / patient__dmp / anon_id columns match the source
      record, and additional_metadata carries every source key that is
      not mapped to a column (verbatim, null/empty skipped).
    * Translation spot-checks on the fixture's tumor records.
    """
    from dmp_translator import unmapped_metadata
    from models import PairingLogic, PairingTable, Sample

    n_pass = n_fail = 0

    dmp_logic = db.query(PairingLogic).filter_by(name="DMP").first()
    actual: dict[str, str | None] = {}
    for s in dmp_samples:
        if s.tumor_or_normal != "TUMOR":
            continue
        p = (
            db.query(PairingTable).filter_by(
                tumor_id=s.id, pairing_logic_id=dmp_logic.id, latest=True
            ).first()
            if dmp_logic is not None
            else None
        )
        normal = db.get(Sample, p.normal_id) if p else None
        actual[s.primary_id] = normal.primary_id if normal else None

    for tumor_pid, (expected_pid, why) in EXPECTED_DMP_PAIRS.items():
        got = actual.get(tumor_pid)
        if got == expected_pid:
            n_pass += 1
            print(f"  [PASS]    tumor {tumor_pid:>17} -> {got}  # {why}")
        else:
            n_fail += 1
            print(f"  [FAIL]    tumor {tumor_pid:>17} -> {got} (expected {expected_pid})  # {why}")

    for tumor_pid, why in EXPECTED_DMP_UNPAIRED.items():
        got = actual.get(tumor_pid)
        if got is None:
            n_pass += 1
            print(f"  [PASS]    tumor {tumor_pid:>17} -> unpaired  # {why}")
        else:
            n_fail += 1
            print(f"  [FAIL]    tumor {tumor_pid:>17} -> {got} (expected unpaired)  # {why}")

    # Completeness: every DMP sample must carry its full source record.
    by_pid = {s.primary_id: s for s in dmp_samples}
    for sid, meta in sorted(source_records.items()):
        s = by_pid.get(sid)
        if s is None:
            n_fail += 1
            print(f"  [FAIL]    missing DMP sample {sid}")
            continue
        patient = meta.get("patient") or {}
        problems = []
        if patient.get("cmo") and s.patient__cmo != f"C-{patient['cmo']}":
            problems.append(f"patient__cmo={s.patient__cmo!r} expected C-{patient['cmo']}")
        if patient.get("dmp") and s.patient__dmp != patient["dmp"]:
            problems.append(f"patient__dmp={s.patient__dmp!r} expected {patient['dmp']!r}")
        if meta.get("anon_id") and s.anon_id != meta["anon_id"]:
            problems.append(f"anon_id={s.anon_id!r} expected {meta['anon_id']!r}")
        expected = unmapped_metadata(meta)
        got = s.additional_metadata or {}
        missing = sorted(k for k in expected if got.get(k) != expected[k])
        if missing:
            problems.append(f"additional_metadata missing/changed: {missing}")
        if problems:
            n_fail += 1
            print(f"  [FAIL]    {sid:>17}: " + "; ".join(problems))
        else:
            n_pass += 1
            print(f"  [PASS]    {sid:>17}: columns + {len(expected)} unmapped keys stored verbatim")

    checks = DMP_CHECKS
    for pid, patient, panel, ton, patient_dmp, anon in checks:
        s = by_pid.get(pid)
        if s is None:
            n_fail += 1
            print(f"  [FAIL]    missing DMP sample {pid}")
            continue
        expected = (patient, panel, ton, "DMP", patient_dmp, anon)
        got = (s.patient__cmo, s.gene_panel, s.tumor_or_normal, s.sample_type,
               s.patient__dmp, s.anon_id)
        if got == expected:
            n_pass += 1
            print(f"  [PASS]    {pid:>17}: patient__cmo={s.patient__cmo} gene_panel={s.gene_panel} "
                  f"tumor_or_normal={s.tumor_or_normal} sample_type={s.sample_type} "
                  f"patient__dmp={s.patient__dmp} anon_id={s.anon_id}")
        else:
            n_fail += 1
            print(f"  [FAIL]    {pid:>17}: got {got} (expected {expected})")

    total = n_pass + n_fail
    print(f"\n{n_pass}/{total} DMP expectations met.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
