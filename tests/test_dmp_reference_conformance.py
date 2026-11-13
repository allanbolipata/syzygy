"""DMP reference conformance (T7, DMP Reference Alignment).

Feeds the parent task's 14-case fixture set
(``tests/fixtures/dmp_reference_conformance/cases/``) through the real
endpoint path — ``pairing_report`` and ``export_pair_report`` called
directly (repo convention: no TestClient) — and asserts the exact pair
rows, summary counts, and warning buckets against the pinned reference
expectations (``pair_tumor_with_normal.R`` output, byte-pinned per case).

The fixture payloads carry no ``group`` and no ``matched_tag`` (audited
absent from real DMP records — spec g), so four rendering-level
divergences between the reference files and the endpoint are expected and
applied as a documented transform (``endpoint_row``) before comparison.
The transform is the ONLY delta: the same reference formulas applied to
the raw pinned rows must reproduce the pinned summary/warning files
exactly (asserted per case below).

An optional cross-check (skipped when Rscript or the reference script is
unavailable — the suite never depends on R) runs the real
``pair_tumor_with_normal.R`` on each case's key file and requires it to
reproduce the pinned expectations byte-for-byte, guarding the fixture set
itself independently of the endpoint.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import syzygy.routers.pairings as pairings
from syzygy.dmp_translator import to_dmp_sample_kwargs
from syzygy.models import Base, PairingLogic, PairingTable, Sample
from syzygy.routers.pairings import export_pair_report, pairing_report

CASES_DIR = Path(__file__).resolve().parent / "fixtures" / "dmp_reference_conformance" / "cases"
NA = "NA"
BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)

CASE_IDS = sorted(p.name for p in CASES_DIR.iterdir() if p.is_dir())


@dataclass
class Case:
    name: str
    dir: Path
    records: list  # [(sample_id, meta), ...] in payload order
    bamnames: set  # the case's bam manifest
    pinned_header: list
    pinned_rows: list  # 15-cell rows, reference (key file) order
    pinned_summary: dict
    pinned_warnings: dict


def _load_case(name: str) -> Case:
    d = CASES_DIR / name
    payload = json.loads((d / "payload.json").read_text())
    records = [(r["fields"]["metadata"]["sample"], r["fields"]["metadata"]) for r in payload]
    bamnames = set(json.loads((d / "bam_manifest.json").read_text())["bamnames"])
    lines = (d / "expected/pair_rows.tsv").read_text().splitlines()
    header, rows = lines[0].split("\t"), [l.split("\t") for l in lines[1:] if l]
    return Case(
        name=name,
        dir=d,
        records=records,
        bamnames=bamnames,
        pinned_header=header,
        pinned_rows=rows,
        pinned_summary=json.loads((d / "expected/summary.json").read_text()),
        pinned_warnings=json.loads((d / "expected/warnings.json").read_text()),
    )


# ---------------------------------------------------------------------------
# DB + beagle stub (same in-memory pattern as tests/test_dmp_report_stats.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = Session(engine)
    with session.begin():
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
    yield session
    session.close()


class _StubBeagleCursor:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubBeagleConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return _StubBeagleCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubBeagleEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _StubBeagleConn(self._rows)


def _stub_beagle(monkeypatch, rows):
    """rows: (sample_name, has_path) pairs; absent names resolve to
    bam_exists "0" (the reference's unresolvable semantics)."""
    monkeypatch.setattr(
        pairings.settings,
        "beagle_database_url",
        "postgresql+psycopg2://beagle:beagle@localhost/beagle",
    )
    monkeypatch.setattr(pairings, "_beagle_engine_cache", _StubBeagleEngine(rows))


@pytest.fixture(params=CASE_IDS)
def case(db, engine, monkeypatch, request):
    """Seed the in-memory DB from the parametrized fixture: samples via the
    real ingest translator, pairings straight from the pinned pair rows.

    The frame orders the paired section by (pairing.created_date desc, id
    desc) and the unmatched section by (sample.created_date desc, id
    desc), so the i-th pinned row (0 = newest) gets a strictly newer
    created_date than row i+1 in its section.
    """
    c = _load_case(request.param)
    logic = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    db.add(logic)
    db.flush()
    by_name = {}
    for i, (sid, meta) in enumerate(c.records):
        # The fixture payloads omit the assay keys; inject a constant CMO
        # panel — the frame cells and the DMP pairing keys never read
        # gene_panel, it only satisfies samples.gene_panel NOT NULL.
        kwargs = to_dmp_sample_kwargs(sid, {**meta, "cmo_assay": "IMPACT468"})
        s = Sample(**kwargs, created_date=BASE_DATE - timedelta(seconds=36000 + i))
        db.add(s)
        by_name[sid] = s
    db.flush()
    for i, row in enumerate(c.pinned_rows):
        if row[9] != NA:  # pinned row with a normal -> one pairing row
            db.add(
                PairingTable(
                    tumor_id=by_name[row[0]].id,
                    normal_id=by_name[row[9]].id,
                    pairing_logic_id=logic.id,
                    latest=True,
                    # DMP is a fan-out strategy (MULTI_PAIR=True): one row
                    # per (tumor, normal) match; multi_pair rows are
                    # exempt from the one-latest-per-tumor unique index.
                    multi_pair=True,
                    trigger_type="automatic",
                    created_date=BASE_DATE - timedelta(seconds=i * 10),
                )
            )
        else:  # pinned row with NA normal -> unmatched tumor row
            by_name[row[0]].created_date = BASE_DATE - timedelta(seconds=72000 + i * 10)
    # Commit: the export stream opens its OWN session on the same
    # StaticPool connection and must see the data without a pending
    # transaction to roll back.
    db.commit()
    _stub_beagle(
        monkeypatch,
        [(sid, (meta.get("bamname") or meta.get("anon_id")) in c.bamnames) for sid, meta in c.records],
    )
    monkeypatch.setattr(pairings, "_open_stream_session", lambda: Session(engine))
    return c


# ---------------------------------------------------------------------------
# Reference formulas (spec d/e) — the same math the endpoint implements,
# applied to a 15-cell frame; used both to derive the expected endpoint
# output and to verify the pinned files against themselves.
# ---------------------------------------------------------------------------


def _num(cell):
    if cell == NA:
        return None
    try:
        v = float(cell)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dedup(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def frame_stats(frame):
    """(summary, warnings) over a 15-cell pair frame, frame order kept
    (first-seen-first dedup), exactly as pair_tumor_with_normal.R computes
    them. Note the warning-5 comparison is on the RENDERED cell (the
    reference is type-aware per column; the endpoint's lexicographic form
    agrees on every pinned fixture — case 13 pins the quirk)."""
    patients, tn_pairs, unmatched = set(), 0, 0
    w1, w3, w4, w5 = [], [], [], []
    row_bams, matched_counts = [], {}
    for c in frame:
        ts, matched = c[0], c[6] == "Matched"
        if matched:
            tn_pairs += 1
            patients.add(c[1])
            matched_counts[c[4]] = matched_counts.get(c[4], 0) + 1
            if c[10] == NA:
                w1.append(ts)
        else:
            unmatched += 1
        row_bams.append((c[4], ts))
        if c[7] == "0" or c[13] == "0":
            w3.append(ts)
            if c[9] != NA:
                w3.append(c[9])
        t_cov = _num(c[8])
        if (c[4] != NA and t_cov is None) or (t_cov is not None and t_cov < 100):
            if ts != NA:
                w4.append(ts)
        n_cov = c[14]
        if (c[10] != NA and n_cov == NA) or (n_cov != NA and n_cov < "50"):
            if c[9] != NA:
                w5.append(c[9])
    ambiguous = {b for b, n in matched_counts.items() if n > 1 and b != NA}
    w2 = [s for b, s in row_bams if b in ambiguous]
    summary = {"patients": len(patients), "tn_pairs": tn_pairs, "unmatched_tumors": unmatched}
    warnings = {
        "matched_tumors_without_normal": _dedup(w1),
        "tumor_samples_with_multiple_ambiguous_normals": _dedup(w2),
        "samples_with_bams_missing": _dedup(w3),
        "low_coverage_tumors": _dedup(w4),
        "low_coverage_normals": _dedup(w5),
    }
    return summary, warnings


def endpoint_row(pinned_row, tumor_meta):
    """One pinned reference row -> the row the endpoint renders.

    The four documented rendering divergences (underlying pairing data is
    identical in every case):
    1. tumor_group: the fixture payloads carry no ``group`` (the key file's
       GRP-DMP is reference-side only; audited absent from real records —
       spec g) -> the endpoint renders NA.
    2. patient: the stored ``patient__dmp`` column wins over the
       reference's name parse (spec g caveat 1) — case 10's unparseable id
       leaves the reference's patient cell empty while the stored column
       says P-7000011.
    3. missing values: the reference leaves failed parses / missing pools
       as EMPTY cells; the endpoint renders every missing value as NA.
    4. tumor_matched: the endpoint derives the flag from the pairing
       (``matched_tag`` audited absent); an unmatched tumor row renders
       Unmatched where the key file's is_matched can say Matched (case 06).
    """
    row = list(pinned_row)
    for i in range(len(row)):
        if row[i] == "":
            row[i] = NA
    patient = (tumor_meta.get("patient") or {}).get("dmp")
    if patient:
        row[1] = patient
    row[5] = NA
    if row[9] == NA:
        row[6] = "Unmatched"
    return row


def endpoint_frame_order(rows):
    """Transformed pinned rows in the endpoint's documented frame order:
    the paired section first, then the unmatched DMP tumor rows, each
    group keeping pinned relative order. The reference pair file keeps
    key-file order, which interleaves the two sections (spec b/c: the
    endpoint streams paired rows first). The ``case`` fixture seeds
    created_dates so each section comes out in pinned row order."""
    paired = [r for r in rows if r[9] != NA]
    unmatched = [r for r in rows if r[9] == NA]
    return paired + unmatched


def _drain(resp) -> str:
    """Drain a StreamingResponse body (sync or async generator)."""
    it = resp.body_iterator
    chunks = []
    if inspect.isasyncgen(it):

        async def _collect():
            async for chunk in it:
                chunks.append(chunk)

        asyncio.run(_collect())
    else:
        for chunk in it:
            chunks.append(chunk)
    return "".join(ch.decode() if isinstance(ch, (bytes, bytearray)) else ch for ch in chunks)


# ---------------------------------------------------------------------------
# Pair rows — the export stream is the endpoint's 15-column pair file
# ---------------------------------------------------------------------------


def test_pair_rows_conform_to_reference(case, db):
    """The export stream (the endpoint's 15-column pair file) reproduces
    the pinned reference pair rows exactly — all rows, in pinned order,
    all 15 cells — with the four documented rendering divergences
    applied."""
    resp = export_pair_report("DMP", q=None, columns=None, fmt="tsv", db=db)
    lines = _drain(resp).splitlines()
    got_header, got = lines[0].split("\t"), [l.split("\t") for l in lines[1:] if l]
    assert got_header == case.pinned_header
    meta_by_name = {sid: meta for sid, meta in case.records}
    expected = endpoint_frame_order(
        [endpoint_row(r, meta_by_name[r[0]]) for r in case.pinned_rows]
    )
    assert got == expected, "export pair rows differ from pinned reference rows (transformed)"


def test_summary_and_warnings_conform_to_reference(case, db):
    """pairing_report's summary counts + 5 warning buckets match the
    reference formulas over the case's frame (pinned expectations, with
    the documented divergences applied); the report rows carry the pinned
    tumor/normal samples in pinned order. The pinned files themselves are
    re-verified against the reference formulas, so the transform above is
    provably the only delta between the pinned files and the endpoint."""
    r = pairing_report("DMP", 1, 200, db, q=None)
    meta_by_name = {sid: meta for sid, meta in case.records}
    frame = endpoint_frame_order(
        [endpoint_row(row, meta_by_name[row[0]]) for row in case.pinned_rows]
    )
    exp_summary, exp_warnings = frame_stats(frame)
    assert r.summary is not None
    assert (r.summary.patients, r.summary.tn_pairs, r.summary.unmatched_tumors) == (
        exp_summary["patients"],
        exp_summary["tn_pairs"],
        exp_summary["unmatched_tumors"],
    )
    assert r.warnings is not None
    for key, values in exp_warnings.items():
        assert getattr(r.warnings, key) == values, f"warning bucket {key!r}"
    # report rows: pinned tumor/normal samples in the endpoint's frame
    # order (paired section first, then unmatched DMP tumors)
    assert r.total == len(case.pinned_rows)
    got_ids = [
        (row.tumor.primary_id, row.normal.primary_id if row.normal is not None else None)
        for row in r.rows
    ]
    exp_ids = [(row[0], row[9] if row[9] != NA else None) for row in frame]
    assert got_ids == exp_ids
    # the pinned files are internally consistent with the reference formulas
    pin_summary, pin_warnings = frame_stats(case.pinned_rows)
    assert pin_summary == case.pinned_summary
    assert pin_warnings == case.pinned_warnings


# ---------------------------------------------------------------------------
# Optional R cross-check — real reference script vs the pinned files
# ---------------------------------------------------------------------------

RSCRIPT = shutil.which("Rscript")
REF_R_SCRIPT = Path("~").expanduser() / "dev" / "bam-mirror-tools" / "scripts" / "pair_tumor_with_normal.R"
R_CHECK = pytest.mark.skipif(
    RSCRIPT is None or not REF_R_SCRIPT.is_file(),
    reason="Rscript or pair_tumor_with_normal.R unavailable (optional cross-check)",
)

KEY_HEADER = [
    "sample", "patient", "idx", "ver", "bamname", "bprefix", "bsuffix",
    "group", "bam_exists", "coverage", "pool_id", "is_matched",
]


def _write_tabbed(c: Case, out: Path):
    """key_file.csv (12-column, X = missing) + bam manifest -> the 12-column
    tabbed key TSV pair_tumor_with_normal.R consumes (the
    generate_dmp_tumor_normal_key.pl comma->tab conversion, with
    patient/idx/ver/bprefix/bsuffix re-derived exactly as the perl script
    derives them)."""
    lines = []
    for line in (c.dir / "key_file.csv").read_text().splitlines():
        if not re.match(r"^P-\d+", line):
            continue
        sp = line.split(",")
        m = re.match(r"(P-\d+)-(\w+)-(\w+)", sp[0])
        patient, idx, ver = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
        bm = re.match(r"(\w+)-(\w+)", sp[1])
        bprefix, bsuffix = (bm.group(1), bm.group(2)) if bm else ("", "")
        bam_exists = "1" if sp[1] in c.bamnames else "0"
        pool, coverage, matched = sp[-5], sp[-4], sp[-3]
        lines.append("\t".join(
            [sp[0], patient, idx, ver, sp[1], bprefix, bsuffix, sp[2], bam_exists, coverage, pool, matched]
        ))
    out.write_text("\t".join(KEY_HEADER) + "\n" + "\n".join(lines) + "\n")


@pytest.mark.parametrize("case_id", CASE_IDS)
@R_CHECK
def test_reference_rscript_reproduces_pinned(case_id, tmp_path):
    c = _load_case(case_id)
    work = tmp_path / case_id
    (work / "out").mkdir(parents=True)
    tabbed = work / "tabbed.txt"
    _write_tabbed(c, tabbed)
    pair_out, summary_out, out_dir = work / "paired.txt", work / "summary.txt", work / "out"
    proc = subprocess.run(
        [RSCRIPT, "--vanilla", str(REF_R_SCRIPT), str(tabbed), str(pair_out), str(summary_out), str(out_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"Rscript failed: {proc.stderr[-500:]}"
    got_lines = pair_out.read_text().splitlines()
    assert got_lines[0].split("\t") == c.pinned_header
    got_rows = [l.split("\t") for l in got_lines[1:] if l]
    assert got_rows == c.pinned_rows, "R pair rows differ from pinned expectations"

    warn_txt = summary_out.read_text()

    def grab(label):
        m = re.search(label + r":\s*(\d+)", warn_txt)
        return int(m.group(1)) if m else None

    got_summary = {
        "patients": grab("# of patients in the key file"),
        "tn_pairs": grab("# of Tumor-Normal pairs"),
        "unmatched_tumors": grab("# of unmatched tumors"),
    }
    assert got_summary == c.pinned_summary, f"R summary {got_summary} != pinned {c.pinned_summary}"

    def warn_list(fname, cols):
        """The reference's warning files hold FULL 15-column pair-frame
        rows (write.table of the filtered frame); the pinned buckets are
        the deduped sample names — tumor side = column 0, normal side =
        column 9 (bams_missing lists both sides of each flagged row,
        low_coverage_normals the normal side only)."""
        f = out_dir / fname
        if not f.exists():
            return []
        vals = []
        for l in f.read_text().splitlines()[1:]:
            if not l.strip():
                continue
            cells = l.split("\t")
            for ci in cols:
                v = cells[ci] if len(cells) > ci else NA
                if v != NA:
                    vals.append(v)
        return _dedup(vals)

    got_warn = {
        "matched_tumors_without_normal": warn_list("matched_tumors_without_normal.txt", (0,)),
        "tumor_samples_with_multiple_ambiguous_normals": warn_list(
            "tumor_samples_with_multiple_ambiguous_normals.txt", (0,)
        ),
        "samples_with_bams_missing": warn_list("samples_with_bams_missing.txt", (0, 9)),
        "low_coverage_tumors": warn_list("low_coverage_tumors.txt", (0,)),
        "low_coverage_normals": warn_list("low_coverage_normals.txt", (9,)),
    }
    assert got_warn == c.pinned_warnings, f"R warnings differ: {got_warn} != {c.pinned_warnings}"
