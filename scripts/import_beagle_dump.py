#!/usr/bin/env python3
"""Import a local beagle PostgreSQL dump database into the local Syzygy db.

Reads the RESTORED beagle database (a plain local Postgres database —
``pg_restore``'d from the dump; no network data leaves this machine) and
merges its sample metadata into the Syzygy ``samples`` table, keyed by
``primary_id`` (DMP samples keyed by their ``sample`` id). Existing Syzygy
rows — including the dashboard's dev fixtures and any user-created pairings
— are preserved: rows are upserted by primary_id, and automatic pairing is
only ever *created*, never clobbered.

Record sources (all from ``file_system_filemetadata`` latest rows):

* **DMP samples** — rows whose file group is ``DMP BAMs``. These carry the
  DMP data structure and are converted with
  :func:`dmp_translator.to_dmp_sample_kwargs`, which stores the ENTIRE
  record: every field mapped to a column (including anon_id and patient.dmp)
  goes to its column, and every remaining key goes verbatim into
  additional_metadata. DMP samples are auto-paired ONLY when --trigger-dmp
  is set (under the "DMP" pairing logic); without the flag they are
  imported unpaired. Manual DMP pairing (dashboard pin or
  ``POST /v1/pairing/trigger``) still exists and always takes precedence:
  existing pairings — automatic, manual or override — are returned
  untouched, never replaced, on re-runs.
* **LIMS/IGO samples** — every other latest row carrying ``primaryId`` (or
  an ``igoRequestId`` + ``tumorOrNormal`` pair for records with no
  primaryId). These groups share the LIMS/IGO filemetadata structure
  (LIMS itself, plus the ACCESS cfDNA / CMO-CH / Illumina output groups,
  which repeat the same sample metadata keys). Values are trimmed; rows
  with no usable ``tumorOrNormal`` (pure QC/output records) are skipped
  and counted.
* **Pooled normals** — the ``file_system_poolednormal`` rows
  (machine/bait-set/gene-panel/preservation/run-date), stored as shared
  NORMAL samples with no patient so the argos cascade's pooled tier can
  reach them.

Provenance: every imported sample records its source beagle file group in
``Sample.file_group`` (e.g. "LIMS", "DMP BAMs", "Pooled Normals"). For
samples merged across several groups, the group whose row is seen first
in the deterministic merge order wins (LIMS rows are sorted first, then
groups alphabetically).

Usage:
    python scripts/import_beagle_dump.py [--beagle-dsn DSN]
                                          [--argos-name NAME]
                                          [--trigger]
                                          [--trigger-dmp]
                                          [--dmp-name NAME]

Defaults: beagle DSN ``postgresql+psycopg2://app:app@127.0.0.1:5434/beagle`` and
the ``ARGOS`` pairing logic. ``--trigger`` runs automatic pairing (argos)
for the imported LIMS/IGO tumors and normals after the upsert, and
``--trigger-dmp`` runs automatic pairing under the ``DMP`` logic (named by
``--dmp-name``) for the imported DMP tumors and normals after the DMP
upsert; both are safe to re-run (existing pairings are returned, never
replaced, so MANUAL pairings always win and survive re-runs).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database import engine
from dmp_translator import to_dmp_sample_kwargs
from models import PairingLogic, Sample
from pairing_service import trigger_pairing, trigger_pairing_for_normal

# File group holding the DMP data structure.
DMP_FILEGROUP = "DMP BAMs"
# File groups whose records are LIMS/IGO sample metadata (everything else
# that carries sample keys is included automatically — no whitelist needed
# because the structural filter below does the work).
PULLED_TON = {"Tumor", "Normal", "PooledNormal"}


def to_sample_kwargs(metadata: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    """Map one LIMS/IGO-structure filemetadata dict to Sample kwargs.

    Returns ``(kwargs, "")`` on success or ``(None, reason)`` when the
    record is not a usable sample record (e.g. a QC/output row with no
    ``tumorOrNormal``).
    """
    primary_id = str(metadata.get("primaryId") or "").strip()
    igo_id = str(metadata.get("igoRequestId") or "").strip()
    if not primary_id and not igo_id:
        return None, "no primaryId / igoRequestId"

    ton_raw = str(metadata.get("tumorOrNormal") or "").strip()
    if ton_raw not in PULLED_TON:
        return None, f"unknown tumorOrNormal {ton_raw or None!r}"
    pooled = ton_raw == "PooledNormal"

    cmo_name = str(metadata.get("cmoSampleName") or "").strip()
    cmo_patient = str(metadata.get("cmoPatientId") or "").strip()
    gene_panel = str(metadata.get("genePanel") or "").strip()
    if not cmo_name:
        cmo_name = primary_id or igo_id
    if not gene_panel:
        gene_panel = "unknown"

    sample_class = str(metadata.get("sampleClass") or "").strip()
    specimen_type = str(metadata.get("specimenType") or "").strip()
    sample_type_key = str(metadata.get("sampleType") or "").strip()
    if pooled:
        # Fixed marker so the argos pooled tier matches these rows.
        sample_type = "Pooled Normal"
    else:
        sample_type = sample_class or specimen_type or sample_type_key or "Unknown"

    run_mode = str(metadata.get("runMode") or "").strip() or None
    run_date = metadata.get("runDate")
    if isinstance(run_date, str):
        run_date = run_date.strip() or None
        if run_date:
            try:
                run_date = datetime.fromisoformat(run_date)
            except ValueError:
                run_date = None

    preservation = metadata.get("preservation")
    sample_origin = metadata.get("sampleOrigin")

    additional: dict[str, Any] = {}
    for key in (
        "baitSet",
        "barcodeId",
        "platform",
        "flowCellId",
        "flowCellLanes",
        "readLength",
        "sequencingCenter",
        "libraryIgoId",
        "oncotreeCode",
        "sex",
        "species",
    ):
        value = metadata.get(key)
        if value is not None:
            additional[key] = str(value).strip() if isinstance(value, str) else value

    kwargs: dict[str, Any] = {
        "primary_id": primary_id or igo_id,
        "cmo_sample_name": cmo_name,
        # Pooled normals belong to no patient.
        "patient__cmo": None if (pooled or not cmo_patient) else cmo_patient,
        "request_id": igo_id or None,
        "sample_type": sample_type,
        "gene_panel": gene_panel,
        "bait_set": str(metadata.get("baitSet") or "").strip() or None,
        "tumor_or_normal": "NORMAL" if ton_raw in ("Normal", "PooledNormal") else "TUMOR",
        "preservation": preservation,
        "sample_origin": sample_origin,
        "run_mode": run_mode,
        "additional_metadata": additional,
        "errors": None,
        "sequencing_date": run_date,
    }
    return kwargs, ""


def to_pooled_normal_kwargs(row: Any) -> dict[str, Any]:
    """Map one ``file_system_poolednormal`` row to Sample kwargs."""
    machine = str(row.machine or "").strip()
    bait_set = str(row.bait_set or "").strip()
    gene_panel = str(row.gene_panel or "").strip()
    preservation = str(row.preservation_type or "").strip()
    run_date = row.run_date
    key = f"pooled-{machine}-{bait_set}-{preservation}"
    return {
        "primary_id": key,
        "cmo_sample_name": key,
        "patient__cmo": None,
        "request_id": None,
        "sample_type": "Pooled Normal",
        "gene_panel": gene_panel or "unknown",
        "bait_set": bait_set or None,
        "tumor_or_normal": "NORMAL",
        "preservation": preservation or None,
        "sample_origin": None,
        "run_mode": machine or None,
        "additional_metadata": {},
        "errors": None,
        "sequencing_date": run_date,
        "file_group": "Pooled Normals",
    }


def merge_dmp_samples(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Merge DMP-structure rows keyed by their ``sample`` id (DMP records
    carry no ``primaryId``). First-non-null-wins across a sample's files."""
    samples: dict[str, dict[str, Any]] = {}
    for _fg, meta in rows:
        key = str(meta.get("sample") or "").strip()
        if not key:
            continue
        entry = samples.get(key)
        if entry is None:
            samples[key] = {"kwargs": {}}
            entry = samples[key]
        kwargs = entry["kwargs"]
        for k, v in meta.items():
            if v is None:
                continue
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    continue
            if kwargs.get(k) in (None, ""):
                kwargs[k] = v
    return samples


def merge_samples(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Merge ``(filegroup, metadata)`` rows into one sample per primaryId.

    Rows are processed in a deterministic order (LIMS rows first, then
    groups alphabetically, then primaryId) with first-non-null-wins; the
    sample's ``file_group`` is the first group seen. Records with no
    primaryId (e.g. ``SAMPLE_01``) attach to the candidate sample whose
    igoRequestId matches; they take no keys the candidate already has.
    """

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple:
        fg, meta = item
        pid = str(meta.get("primaryId") or "").strip()
        return (0 if fg == "LIMS" else 1, fg, pid)

    order = sorted(rows, key=sort_key)
    samples: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []

    for fg, meta in order:
        primary_id = str(meta.get("primaryId") or "").strip()
        if primary_id:
            entry = samples.get(primary_id)
            if entry is None:
                samples[primary_id] = {"primary_id": primary_id, "kwargs": {}, "file_group": fg}
                entry = samples[primary_id]
            kwargs = entry["kwargs"]
            for key, value in meta.items():
                if value is None:
                    continue
                if isinstance(value, str):
                    value = value.strip()
                if not value:
                    continue
                if kwargs.get(key) in (None, ""):
                    kwargs[key] = value
        else:
            igo_id = str(meta.get("igoRequestId") or "").strip()
            if igo_id and str(meta.get("tumorOrNormal") or "").strip() in PULLED_TON:
                anonymous.append(meta)

    for meta in anonymous:
        igo_id = str(meta.get("igoRequestId") or "").strip()
        target = None
        for sample in samples.values():
            if sample["kwargs"].get("igoRequestId") == igo_id:
                target = sample
                break
        if target is None:
            continue
        kwargs = target["kwargs"]
        for key, value in meta.items():
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            if not value:
                continue
            if kwargs.get(key) in (None, ""):
                kwargs[key] = value
    return samples


def build_samples(
    samples: dict[str, dict[str, Any]], is_dmp: bool
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Convert merged records to final Sample kwargs; returns
    (sample dicts, skip reasons, unknown-ton count)."""
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    for primary_id, entry in samples.items():
        if is_dmp:
            sid = str(entry["kwargs"].get("sample") or "").strip()
            if not sid:
                skipped.append("missing DMP sample id")
                continue
            try:
                kwargs = to_dmp_sample_kwargs(sid, entry["kwargs"])
            except ValueError as exc:
                skipped.append(str(exc))
                continue
            if not kwargs.get("gene_panel"):
                kwargs["gene_panel"] = "unknown"
            kwargs["file_group"] = DMP_FILEGROUP
        else:
            kwargs, reason = to_sample_kwargs(entry["kwargs"])
            if kwargs is None:
                skipped.append(reason)
                continue
            kwargs["file_group"] = entry["file_group"]
        out.append(kwargs)
    return out, skipped, len(skipped)


def upsert_samples(db: Session, samples: list[dict[str, Any]]) -> tuple[int, int]:
    """Upsert sample kwargs dicts keyed by primary_id; batched commits.

    Returns (created, updated)."""
    existing: dict[str, Any] = {}
    pids = [s["primary_id"] for s in samples]
    for i in range(0, len(pids), 2000):
        chunk = pids[i : i + 2000]
        rows = db.execute(
            sa.select(Sample.id, Sample.primary_id).where(Sample.primary_id.in_(chunk))
        ).all()
        for row in rows:
            existing[row.primary_id] = row.id

    created = updated = 0
    for idx, kwargs in enumerate(samples, 1):
        row = db.get(Sample, existing.get(kwargs["primary_id"]))
        if row is None:
            db.add(Sample(**kwargs))
            created += 1
        else:
            for key, value in kwargs.items():
                setattr(row, key, value)
            updated += 1
        if idx % 2000 == 0:
            db.flush()
            print(f"  ...{idx}/{len(samples)} upserted", flush=True)
    db.commit()
    return created, updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--beagle-dsn",
        default="postgresql+psycopg2://app:app@127.0.0.1:5434/beagle",
        help="DSN of the RESTORED beagle database (local only).",
    )
    parser.add_argument("--argos-name", default="ARGOS", help="Auto-pairing logic name.")
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="Run automatic (argos) pairing for imported LIMS/IGO tumors/normals.",
    )
    parser.add_argument(
        "--trigger-dmp",
        action="store_true",
        help="Run automatic DMP pairing (under the --dmp-name logic) for "
        "imported DMP tumors/normals after the DMP upsert. Idempotent: "
        "existing pairings — including MANUAL ones — are returned, never "
        "replaced.",
    )
    parser.add_argument(
        "--dmp-name",
        default="DMP",
        help="Name of the pairing logic to use with --trigger-dmp (default: DMP).",
    )
    args = parser.parse_args()

    print("Importing beagle data into syzygy — all data stays on this machine.")
    beagle_engine = sa.create_engine(args.beagle_dsn)
    beagle_session = sessionmaker(bind=beagle_engine, future=True)

    db = Session(engine, future=True)
    try:
        argos_logic = (
            db.execute(sa.select(PairingLogic).where(PairingLogic.name == args.argos_name)).scalar_one_or_none()
            if args.trigger
            else None
        )
        dmp_logic = (
            db.execute(sa.select(PairingLogic).where(PairingLogic.name == args.dmp_name)).scalar_one_or_none()
            if args.trigger_dmp
            else None
        )

        # ---- Phase 1: DMP samples (file group "DMP BAMs") -----------------
        print(f"[1/4] Reading DMP records (file group {DMP_FILEGROUP!r}) ...")
        dmp_rows: list[tuple[str, dict[str, Any]]] = []
        with beagle_session() as bs:
            for (fg, meta) in bs.execute(
                sa.text(
                    """
                    select fg.name, fm.metadata
                      from file_system_filegroup fg
                      join file_system_file f on f.file_group_id = fg.id
                      join file_system_filemetadata fm on fm.file_id = f.id and fm.latest
                     where fg.name = :name
                    """
                ),
                {"name": DMP_FILEGROUP},
            ):
                dmp_rows.append((fg, meta))
        print(f"  rows: {len(dmp_rows)}")
        dmp_samples = merge_dmp_samples(dmp_rows)
        del dmp_rows
        dmp_list, dmp_skipped, _ = build_samples(dmp_samples, is_dmp=True)
        print(f"  unique samples: {len(dmp_list)} (skipped: {len(dmp_skipped)})")
        if dmp_skipped:
            from collections import Counter

            for reason, count in Counter(dmp_skipped).most_common(10):
                print(f"    - {count} x {reason}")
        dmp_created, dmp_updated = upsert_samples(db, dmp_list)
        print(f"  upserted: {dmp_created} created, {dmp_updated} updated")
        dmp_pids = [s["primary_id"] for s in dmp_list]
        del dmp_list, dmp_samples

        # ---- Phase 2: optional automatic DMP pairing ----------------------
        if args.trigger_dmp:
            if dmp_logic is None:
                print(f"[2/5] SKIP: pairing logic {args.dmp_name!r} not found.")
            else:
                print(f"[2/5] Automatic pairing (logic {dmp_logic.name!r}) for DMP samples ...")
                dmp_paired = 0
                for i, pid in enumerate(dmp_pids, 1):
                    sample = db.execute(
                        sa.select(Sample).where(Sample.primary_id == pid)
                    ).scalar_one_or_none()
                    if sample is None:
                        continue
                    try:
                        if sample.tumor_or_normal == "TUMOR":
                            trigger_pairing(db, sample, dmp_logic, "automatic")
                        else:
                            trigger_pairing_for_normal(db, sample, dmp_logic)
                    except ValueError:
                        db.rollback()
                        dmp_paired += 1
                        continue
                    dmp_paired += 1
                    if i % 2000 == 0:
                        print(f"  ...{i}/{len(dmp_pids)}", flush=True)
                print(f"  pairing attempts: {dmp_paired}")
        else:
            print("[2/5] No --trigger-dmp: skipping automatic DMP pairing.")

        # ---- Phase 3: LIMS/IGO samples (every other sample-structure row) -
        print("[3/5] Reading LIMS/IGO records (primaryId rows, all other groups) ...")
        lims_rows: list[tuple[str, dict[str, Any]]] = []
        skipped_no_key = 0
        with beagle_session() as bs:
            for (fg, meta) in bs.execute(
                sa.text(
                    """
                    select fg.name, fm.metadata
                      from file_system_filegroup fg
                      join file_system_file f on f.file_group_id = fg.id
                      join file_system_filemetadata fm on fm.file_id = f.id and fm.latest
                     where fg.name <> :dmp
                       and (fm.metadata ? 'primaryId'
                            or (fm.metadata ? 'igoRequestId' and fm.metadata ? 'tumorOrNormal'))
                    """
                ),
                {"dmp": DMP_FILEGROUP},
            ):
                lims_rows.append((fg, meta))
        print(f"  rows: {len(lims_rows)}")
        lims_samples = merge_samples(lims_rows)
        del lims_rows
        lims_list, lims_skipped, _ = build_samples(lims_samples, is_dmp=False)
        print(f"  unique samples: {len(lims_list)} (skipped: {len(lims_skipped)})")
        if lims_skipped:
            from collections import Counter

            for reason, count in Counter(lims_skipped).most_common(10):
                print(f"    - {count} x {reason}")
        lims_created, lims_updated = upsert_samples(db, lims_list)
        print(f"  upserted: {lims_created} created, {lims_updated} updated")
        lims_pids = [s["primary_id"] for s in lims_list]
        del lims_list, lims_samples

        # ---- Phase 4: pooled normals ---------------------------------------
        print("[4/5] Reading pooled normals (file_system_poolednormal) ...")
        pooled_list: list[dict[str, Any]] = []
        with beagle_session() as bs:
            for row in bs.execute(
                sa.text(
                    """
                    select machine, bait_set, gene_panel, preservation_type, run_date
                      from file_system_poolednormal
                    """
                )
            ):
                pooled_list.append(to_pooled_normal_kwargs(row))
        pooled_created, pooled_updated = upsert_samples(db, pooled_list)
        print(f"  upserted: {pooled_created} created, {pooled_updated} updated")

        # ---- Phase 5: optional automatic pairing (LIMS/IGO) ---------------
        paired = 0
        if args.trigger:
            if argos_logic is None:
                print(f"[5/5] SKIP: pairing logic {args.argos_name!r} not found.")
            else:
                print(f"[5/5] Automatic pairing (logic {argos_logic.name!r}) for LIMS/IGO samples ...")
                for i, pid in enumerate(lims_pids, 1):
                    sample = db.execute(
                        sa.select(Sample).where(Sample.primary_id == pid)
                    ).scalar_one_or_none()
                    if sample is None:
                        continue
                    try:
                        if sample.tumor_or_normal == "TUMOR":
                            trigger_pairing(db, sample, argos_logic, "automatic")
                        else:
                            trigger_pairing_for_normal(db, sample, argos_logic)
                    except ValueError:
                        db.rollback()
                        paired += 1
                        continue
                    paired += 1
                    if i % 2000 == 0:
                        print(f"  ...{i}/{len(lims_pids)}", flush=True)
                print(f"  pairing attempts: {paired}")
        else:
            print("[5/5] No --trigger: skipping automatic pairing.")

        total = db.execute(sa.select(sa.func.count(Sample.id))).scalar_one()
        by_ton = dict(
            db.execute(
                sa.select(Sample.tumor_or_normal, sa.func.count(Sample.id)).group_by(Sample.tumor_or_normal)
            ).all()
        )
        by_group = dict(
            db.execute(
                sa.select(Sample.file_group, sa.func.count(Sample.id)).group_by(Sample.file_group)
            ).all()
        )
        pair_count = db.execute(sa.select(sa.func.count(PairingLogic.id))).scalar_one()
        print("Done.")
        print(f"  samples total: {total}")
        print(f"  by tumor_or_normal: {by_ton}")
        print(f"  by file_group: {by_group}")
        print(f"  pairing logics: {pair_count}")
        if dmp_skipped or lims_skipped:
            print(f"  skipped DMP records: {len(dmp_skipped)}; LIMS/IGO records: {len(lims_skipped)}")
        beagle_engine.dispose()
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
