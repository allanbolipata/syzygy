"""Tests for scripts/import_tempo_pairs.py (TSV pair-assignment importer).

Covers the TSV parser (header validation, CRLF, malformed rows), the
cmoSampleName -> UUID resolution layer (exact-match enforcement, zero and
ambiguous matches, caching), the beagle run-mode drift check (AST
extraction + alignment against TEMPO_RUN_MODE_MAP, using synthetic
tempo_sample.py trees), the --log pairing log, and the main() flow with a
stubbed httpx client. The final test runs the drift check against a real
beagle checkout when one is available (BEAGLE_REPO env var or
~/workspace/beagle) and skips otherwise.
"""

import os
import sys

import httpx
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import import_tempo_pairs as itp
from syzygy.pairing_strategies import TEMPO_RUN_MODE_MAP


# --------------------------------------------------------------------------
# parse_pairs_file
# --------------------------------------------------------------------------

def _write(tmp_path, text, name="pairs.tsv"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_parse_happy_path(tmp_path):
    path = _write(tmp_path, "NORMAL_ID\tTUMOR_ID\nN1\tT1\nN2\tT2\n")
    assert itp.parse_pairs_file(path) == [("N1", "T1"), ("N2", "T2")]


def test_parse_crlf_blank_lines_and_case_header(tmp_path):
    path = _write(tmp_path, "normal_id\ttumor_id\r\n\r\nN1\tT1\r\n\r\n")
    assert itp.parse_pairs_file(path) == [("N1", "T1")]


def test_parse_strips_whitespace(tmp_path):
    path = _write(tmp_path, "NORMAL_ID\tTUMOR_ID\n  N1 \t T1 \n")
    assert itp.parse_pairs_file(path) == [("N1", "T1")]


def test_parse_bad_header(tmp_path):
    path = _write(tmp_path, "TUMOR_ID\tNORMAL_ID\nN1\tT1\n")
    with pytest.raises(ValueError, match="expected header"):
        itp.parse_pairs_file(path)


def test_parse_wrong_field_count(tmp_path):
    path = _write(tmp_path, "NORMAL_ID\tTUMOR_ID\nN1\tT1\textra\n")
    with pytest.raises(ValueError, match="line 2"):
        itp.parse_pairs_file(path)


def test_parse_empty_file(tmp_path):
    path = _write(tmp_path, "")
    with pytest.raises(ValueError, match="empty"):
        itp.parse_pairs_file(path)


# --------------------------------------------------------------------------
# resolve_sample (cmoSampleName -> UUID)
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, json_data, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.ConnectError("boom")

    def json(self):
        return self._json


class FakeClient:
    """Stands in for httpx.Client: sample rows keyed by (name, side)."""

    def __init__(self, samples=None, post_results=None, possible_matches=None,
                 possible_matches_error=False):
        # samples: {cmo_sample_name: [row, ...]} — get() mimics the API's
        # substring filter by returning every row whose name contains the
        # query and whose side matches.
        self.samples = samples or {}
        self.post_results = list(post_results or [])
        self.possible_matches = possible_matches
        self.possible_matches_error = possible_matches_error
        self.get_calls = []
        self.post_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        params = params or {}
        if url.endswith("/possible-matches"):
            if self.possible_matches_error:
                return FakeResponse({}, status_code=500, text="boom")
            return FakeResponse(self.possible_matches or {})
        self.get_calls.append(params)
        name = params.get("cmo_sample_name", "")
        side = params.get("tumor_or_normal")
        rows = [
            row
            for row_name, row_list in self.samples.items()
            if name in row_name
            for row in row_list
            if row["tumor_or_normal"] == side
        ]
        return FakeResponse(rows)

    def post(self, url, json=None):
        self.post_calls.append(json)
        if self.post_results:
            return self.post_results.pop(0)
        return FakeResponse({}, status_code=500, text="unexpected post")


def _row(name, side, sid=None):
    return {"id": sid or f"uuid-{name}", "cmo_sample_name": name,
            "tumor_or_normal": side}


def test_resolve_exact_match_filters_substring_noise():
    client = FakeClient({
        "DEV-020-N": [_row("DEV-020-N", "NORMAL")],
        "DEV-020-N2": [_row("DEV-020-N2", "NORMAL")],
    })
    cache = {}
    assert itp.resolve_sample(client, "b", "DEV-020-N", "NORMAL", cache) == "uuid-DEV-020-N"


def test_resolve_zero_match():
    client = FakeClient({})
    with pytest.raises(ValueError, match="no normal sample"):
        itp.resolve_sample(client, "b", "NOPE", "NORMAL", {})


def test_resolve_ambiguous_match():
    client = FakeClient({
        "DUP": [_row("DUP", "NORMAL", "uuid-a"), _row("DUP", "NORMAL", "uuid-b")],
    })
    with pytest.raises(ValueError, match="ambiguous"):
        itp.resolve_sample(client, "b", "DUP", "NORMAL", {})


def test_resolve_caches():
    client = FakeClient({"N1": [_row("N1", "NORMAL")]})
    cache = {}
    itp.resolve_sample(client, "b", "N1", "NORMAL", cache)
    itp.resolve_sample(client, "b", "N1", "NORMAL", cache)
    assert len(client.get_calls) == 1


# --------------------------------------------------------------------------
# beagle run-mode drift check
# --------------------------------------------------------------------------

BEAGLE_SRC = '''
class TempoSample:
    def _map_run_modes(self):
        run_modes = self.metadata["runMode"]
        self.remapped_run_mode = set()

        for i in run_modes:
            if "novaseq" in i.lower():
                self.remapped_run_mode.add("NovaSeq")
            elif "hiseq" in i.lower():
                self.remapped_run_mode.add("HiSeq")
            else:
                self.remapped_run_mode.add(i)
'''

DRIFT_SRC = BEAGLE_SRC.replace('elif "hiseq" in i.lower():', 'elif "nextseq" in i.lower():')


def _beagle_tree(tmp_path, *sources):
    """Build a fake beagle root with one tempo_sample.py per source."""
    for i, src in enumerate(sources):
        d = tmp_path / "runner" / "operator" / f"op{i}" / "bin"
        d.mkdir(parents=True)
        (d / "tempo_sample.py").write_text(src)
    return str(tmp_path)


def test_extract_run_mode_rules(tmp_path):
    path = _write(tmp_path, BEAGLE_SRC, name="tempo_sample.py")
    assert itp.extract_beagle_run_mode_rules(path) == {
        "novaseq": "NovaSeq", "hiseq": "HiSeq",
    }


def test_extract_run_mode_rules_missing_function(tmp_path):
    path = _write(tmp_path, "class X:\n    def other(self):\n        pass\n")
    with pytest.raises(ValueError, match="no _map_run_modes"):
        itp.extract_beagle_run_mode_rules(path)


def test_alignment_ok(tmp_path):
    root = _beagle_tree(tmp_path, BEAGLE_SRC, BEAGLE_SRC)
    ok, lines = itp.check_run_mode_alignment(root)
    assert ok, lines
    assert all(line.startswith("OK") for line in lines)


def test_alignment_bucket_drift(tmp_path):
    src = BEAGLE_SRC.replace('.add("NovaSeq")', '.add("nextseq")')
    root = _beagle_tree(tmp_path, src)
    ok, lines = itp.check_run_mode_alignment(root)
    assert not ok
    assert any("bucket differs for 'novaseq'" in line for line in lines)


def test_alignment_extra_rule_drift(tmp_path):
    root = _beagle_tree(tmp_path, DRIFT_SRC)
    ok, lines = itp.check_run_mode_alignment(root)
    assert not ok
    assert any("'nextseq'" in line for line in lines)


def test_alignment_cross_operator_drift(tmp_path):
    root = _beagle_tree(tmp_path, BEAGLE_SRC, DRIFT_SRC)
    ok, lines = itp.check_run_mode_alignment(root)
    assert not ok
    assert any("operators disagree" in line for line in lines)


def test_alignment_no_files(tmp_path):
    ok, lines = itp.check_run_mode_alignment(str(tmp_path))
    assert not ok and any("no tempo_sample.py" in line for line in lines)


BEAGLE_REPO = os.environ.get("BEAGLE_REPO") or os.path.expanduser("~/workspace/beagle")


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(BEAGLE_REPO, "runner", "operator",
                                    "tempo_mpgen_operator", "bin", "tempo_sample.py")),
    reason="no beagle checkout (set BEAGLE_REPO)",
)
def test_real_beagle_aligned_with_syzygy():
    """The live drift gate: beagle's TEMPO/CHRONOS operators must still
    agree with syzygy's TEMPO_RUN_MODE_MAP."""
    ok, lines = itp.check_run_mode_alignment(BEAGLE_REPO)
    assert ok, "\n".join(lines)


# --------------------------------------------------------------------------
# main() flow with a stubbed client
# --------------------------------------------------------------------------

def _run_main(tmp_path, monkeypatch, rows_text, samples, post_results, extra=(),
              possible_matches=None, pm_error=False):
    path = _write(tmp_path, rows_text)
    log_path = str(tmp_path / "pairing_log.tsv")
    client = FakeClient(samples, post_results,
                        possible_matches=possible_matches,
                        possible_matches_error=pm_error)
    monkeypatch.setattr(itp.httpx, "Client", lambda **kw: client)
    argv = [path, "--token", "t", "--log", log_path, *extra]
    code = itp.main(argv)
    with open(log_path) as fh:
        log_lines = [ln.rstrip("\n").split("\t") for ln in fh]
    return code, log_lines, client


def test_main_all_clean_exit_zero(tmp_path, monkeypatch, capsys):
    samples = {
        "N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")],
    }
    posts = [FakeResponse({"pairing_logic": "TEMPO", "mismatched_keys": None})]
    code, log_lines, client = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, posts)
    assert code == 0
    assert log_lines == [list(itp.LOG_HEADER)]  # header only, no failures
    assert client.post_calls[0]["pairing_logic_name"] == "TEMPO"
    assert client.post_calls[0]["trigger_type"] == "override"
    assert "1 pinned" in capsys.readouterr().out


def test_main_logs_mismatch_and_fail(tmp_path, monkeypatch):
    samples = {
        "N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")],
        "N2": [_row("N2", "NORMAL")], "T2": [_row("T2", "TUMOR")],
    }
    posts = [FakeResponse({"pairing_logic": "TEMPO",
                           "mismatched_keys": ["run_mode mismatch (hiseq vs novaseq)"]})]
    code, log_lines, client = _run_main(
        tmp_path, monkeypatch,
        "NORMAL_ID\tTUMOR_ID\nN2\tT2\nN404\tT1\n", samples, posts)
    assert code == 1
    assert log_lines[0] == list(itp.LOG_HEADER)
    assert log_lines[1][0] == "MISMATCH" and log_lines[1][1:3] == ["N2", "T2"]
    assert "run_mode mismatch" in log_lines[1][3]
    assert log_lines[2][0] == "FAIL" and log_lines[2][1:3] == ["N404", "T1"]
    assert len(log_lines) == 3  # clean rows never appear in the log


def test_main_http_error_logged(tmp_path, monkeypatch):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    posts = [FakeResponse({}, status_code=404, text="No valid pairing\nsecond line")]
    code, log_lines, _ = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, posts)
    assert code == 1
    assert log_lines[1][0] == "FAIL"
    # multi-line API detail is flattened so the TSV stays one row per line
    assert "\n" not in log_lines[1][3] and "No valid pairing" in log_lines[1][3]


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    client = FakeClient(samples, [])
    monkeypatch.setattr(itp.httpx, "Client", lambda **kw: client)
    path = _write(tmp_path, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n")
    assert itp.main([path, "--dry-run"]) == 0
    assert client.post_calls == []


def test_main_check_beagle_drift_aborts(tmp_path, monkeypatch):
    root = _beagle_tree(tmp_path, DRIFT_SRC)
    path = _write(tmp_path, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n")
    client = FakeClient({}, [])
    monkeypatch.setattr(itp.httpx, "Client", lambda **kw: client)
    code = itp.main([path, "--token", "t", "--check-beagle", root])
    assert code == 2
    assert client.post_calls == []  # nothing imported


# --------------------------------------------------------------------------
# multiple candidate normals (MULTI)
# --------------------------------------------------------------------------

def _pm(current=None, matched=(), unmatched=(), pick=None):
    """Build a possible-matches payload for a tumor."""
    def cand(name, m, sel=False):
        return {"sample": {"cmo_sample_name": name}, "tier": "same_request",
                "matched": m, "mismatched_keys": None if m else ["key"],
                "would_be_selected": sel}
    return {
        "current_match": ({"sample": {"cmo_sample_name": current},
                           "mismatched_keys": None} if current else None),
        "other_matches_matched": len(matched),
        "other_matches": [cand(n, True, n == pick) for n in matched]
                         + [cand(n, False) for n in unmatched],
    }


def test_candidate_summary_counts_matched_only():
    client = FakeClient(possible_matches=_pm(
        current="N0", matched=["N1", "N2"], unmatched=["N3"], pick="N2"))
    count, names, pick = itp.candidate_normal_summary(client, "b", "T1", "TEMPO")
    assert count == 3  # clean current match + 2 matched candidates
    assert names == ["N0", "N1", "N2"]
    assert pick == "N2"


def test_candidate_summary_empty_payload():
    client = FakeClient(possible_matches={})
    assert itp.candidate_normal_summary(client, "b", "T1", "TEMPO") == (0, [], None)


def test_main_multi_candidate_still_pins_and_logs(tmp_path, monkeypatch, capsys):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    posts = [FakeResponse({"pairing_logic": "TEMPO", "mismatched_keys": None})]
    pm = _pm(matched=["N1", "N2"], pick="N2")
    code, log_lines, client = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, posts,
        possible_matches=pm)
    assert code == 0
    assert len(client.post_calls) == 1  # curated pair pinned despite ambiguity
    out = capsys.readouterr().out
    assert "MULTI N1 -> T1: tumor has 2 matching candidate normals" in out
    assert "cascade would pick N2" in out
    assert "1 with multiple candidate normals" in out
    assert log_lines[1][0] == "MULTI" and log_lines[1][1:3] == ["N1", "T1"]


def test_main_multi_plus_mismatch_combines_detail(tmp_path, monkeypatch):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    posts = [FakeResponse({"pairing_logic": "TEMPO",
                           "mismatched_keys": ["run_mode mismatch (a vs b)"]})]
    code, log_lines, _ = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, posts,
        possible_matches=_pm(matched=["N1", "N2"]))
    assert code == 0
    assert log_lines[1][0] == "MISMATCH"
    assert "run_mode mismatch" in log_lines[1][3]
    assert "| tumor has 2 matching candidate normals" in log_lines[1][3]


def test_main_possible_matches_failure_does_not_block(tmp_path, monkeypatch):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    posts = [FakeResponse({"pairing_logic": "TEMPO", "mismatched_keys": None})]
    code, log_lines, client = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, posts,
        pm_error=True)
    assert code == 0
    assert len(client.post_calls) == 1  # pin proceeds when possible-matches 500s
    assert log_lines == [list(itp.LOG_HEADER)]  # no MULTI row, no failure


def test_main_dry_run_flags_multi(tmp_path, monkeypatch, capsys):
    samples = {"N1": [_row("N1", "NORMAL")], "T1": [_row("T1", "TUMOR")]}
    code, log_lines, client = _run_main(
        tmp_path, monkeypatch, "NORMAL_ID\tTUMOR_ID\nN1\tT1\n", samples, [],
        extra=("--dry-run",), possible_matches=_pm(matched=["N1", "N2"]))
    assert code == 0
    assert client.post_calls == []
    out = capsys.readouterr().out
    assert "resolves (dry run) — tumor has 2 matching candidate normals" in out
    assert log_lines[1][0] == "MULTI"
