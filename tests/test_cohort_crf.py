"""Golden-file byte-identity tests for the vendored CRF serialization (T2).

The goldens in ``tests/fixtures/crf/*.cohort.txt`` were generated from the
real legacy pipeline: a fresh clone of ``mskcc/ccs_cohort_utils`` (main
6e92d89) with ``cohort_utils.model.cohort.Cohort(crj=...).to_crf()``. The
fixtures are RAW request dicts; the tests run them through
``cohort_crf.prepare_cohort`` (the legacy ``Cohort.__init__`` preprocessing)
before serializing — the input contract T3/T8 apply — and compare bytes
against the goldens.
"""

import copy
import importlib.util
from pathlib import Path

import pytest

import cohort_crf
from cohort_crf import clean_nones, normalize_id, prepare_cohort, to_crf, write_crf

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crf"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    if spec is None:
        raise ImportError(f"cannot load fixture module {name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FULL_COHORT = _load("full_cohort").FULL_COHORT
MINIMAL_COHORT = _load("minimal_cohort").MINIMAL_COHORT
UPDATE = _load("update_cohort")


def _golden(name):
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Golden byte-identity (to_crf output == legacy-generated golden file)
# ---------------------------------------------------------------------------


class TestGoldenByteIdentity:
    def test_full_4col(self):
        cohort = prepare_cohort(copy.deepcopy(FULL_COHORT))
        assert to_crf(cohort).encode("utf-8") == _golden("full_4col.cohort.txt")

    def test_full_2col(self):
        cohort = prepare_cohort(copy.deepcopy(FULL_COHORT))
        out = to_crf(cohort, keep_primary_ids=False)
        assert out.encode("utf-8") == _golden("full_2col.cohort.txt")
        assert "#TUMOR_ID\tNORMAL_ID\n" in out
        assert "PRIMARY_ID" not in out

    def test_minimal_4col(self):
        cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
        assert to_crf(cohort).encode("utf-8") == _golden("minimal_4col.cohort.txt")

    def test_minimal_2col(self):
        cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
        out = to_crf(cohort, keep_primary_ids=False)
        assert out.encode("utf-8") == _golden("minimal_2col.cohort.txt")

    def test_minimal_g3_headers(self):
        """G3: type absent + projectSubtitle null -> #type:investigator, no
        projectSubtitle line (byte-identical to the legacy pipeline)."""
        out = to_crf(prepare_cohort(copy.deepcopy(MINIMAL_COHORT)))
        assert "#type:investigator\n" in out
        assert "#projectSubtitle:" not in out

    def test_full_header_passthrough(self):
        """Explicit non-default type and present projectSubtitle pass through."""
        out = to_crf(prepare_cohort(copy.deepcopy(FULL_COHORT)))
        assert "#type:MSKWESRP\n" in out
        assert "#projectSubtitle:All four id fields on every sample\n" in out

    def test_never_emits_none(self):
        for raw in (FULL_COHORT, MINIMAL_COHORT):
            cohort = prepare_cohort(copy.deepcopy(raw))
            for s in (to_crf(cohort), to_crf(cohort, keep_primary_ids=False)):
                assert "None" not in s

    def test_zero_samples_header_only(self):
        cohort = {
            "cohortId": "X",
            "endUsers": ["a"],
            "pmUsers": ["p"],
            "projectTitle": "T",
            "type": "investigator",
            "samples": [],
        }
        assert (
            to_crf(cohort)
            == "#endUsers:a\n#pmUsers:p\n#projectTitle:T\n#type:investigator\n"
            "#TUMOR_ID\tNORMAL_ID\tPRIMARY_ID\tNORMAL_PRIMARY_ID\n"
        )


# ---------------------------------------------------------------------------
# prepare_cohort (legacy Cohort.__init__ preprocessing)
# ---------------------------------------------------------------------------


def test_prepare_cohort_defaults_type_and_strips():
    raw = copy.deepcopy(MINIMAL_COHORT)
    assert "type" not in raw
    out = prepare_cohort(raw)
    assert out["type"] == "investigator"
    assert "status" not in out
    assert "projectSubtitle" not in out  # null value removed by clean_nones
    # ids normalized
    assert out["samples"][1]["cmoId"] == "C-PPPP20-X002-d02"
    # raw input not mutated
    assert "type" not in raw
    assert "status" in raw
    assert "projectSubtitle" in raw
    assert raw["samples"][1]["cmoId"] == "s_C_PPPP20_X002_d02"


def test_prepare_cohort_strips_status_and_date():
    raw = copy.deepcopy(FULL_COHORT)
    assert "status" in raw and "date" in raw
    out = prepare_cohort(raw)
    assert "status" not in out
    assert "date" not in out
    assert out["type"] == "MSKWESRP"  # explicit type preserved
    # raw s_-underscore and IGO- ids normalized
    assert out["samples"][0]["cmoId"] == "C-PPPP16-X001-d01"
    assert out["samples"][1]["cmoId"] == "C-PPPP17-X002-d02"
    # already-dashed id unchanged
    assert out["samples"][2]["cmoId"] == "C-PPPP18-X003-d03"


# ---------------------------------------------------------------------------
# write_crf: submit, update merge, atomicity
# ---------------------------------------------------------------------------


def test_submit_writes_full_crf(tmp_path):
    cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
    rows = [
        ("C-PPPP19-X001-d01", "C-PPPP19-X001-n01"),
        ("C-PPPP21-X003-d03", "C-PPPP21-X003-n03"),
    ]
    path = write_crf(str(tmp_path), cohort, rows)
    assert Path(path).name == "99902.cohort.txt"
    out = Path(path).read_text(encoding="utf-8")
    expected = (
        cohort_crf.crf_header(cohort, keep_primary_ids=False)
        + "C-PPPP19-X001-d01\tC-PPPP19-X001-n01\n"
        + "C-PPPP21-X003-d03\tC-PPPP21-X003-n03\n"
    )
    assert out == expected
    # 2-column variant only
    assert "#TUMOR_ID\tNORMAL_ID\n" in out
    assert "PRIMARY_ID" not in out


def test_submit_creates_cohort_dir(tmp_path):
    cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
    nested = tmp_path / "sub" / "crf"
    path = write_crf(str(nested), cohort, [("t", "n")])
    assert Path(path).is_file()
    assert Path(path).parent == nested


def test_update_merge_replaces_headers_preserves_rows(tmp_path):
    cohort = prepare_cohort(copy.deepcopy(UPDATE.UPDATE_COHORT))
    existing = UPDATE.EXISTING_CRF
    # the file fixture and the constant must agree
    assert _golden("update_existing.cohort.txt").decode("utf-8") == existing

    # rows are ignored on the update path (header-only merge)
    path = write_crf(str(tmp_path), cohort, [("ignored", "rows")], existing=existing)
    out = Path(path).read_text(encoding="utf-8")
    assert Path(path).name == "99903.cohort.txt"

    # new header block (no projectSubtitle line — 5 # lines, was 6)
    new_header = (
        "#endUsers:analyst4@example.org\n"
        "#pmUsers:pm3@example.org,pm4@example.org\n"
        "#projectTitle:Updated Title\n"
        "#type:operational\n"
        "#TUMOR_ID\tNORMAL_ID\n"
    )
    assert out.startswith(new_header)
    header_lines = [ln for ln in out.splitlines() if ln.startswith("#")]
    assert header_lines == new_header.splitlines()

    # old header lines are gone
    assert "old_user@example.org" not in out
    assert "Original Title" not in out
    assert "Original Subtitle" not in out
    assert "#type:investigator" not in out

    # data rows preserved verbatim, including the empty-normal cell
    data_lines = [ln for ln in out.splitlines() if not ln.startswith("#")]
    assert data_lines == [
        "C-PPPP22-X001-d01\tC-PPPP22-X001-n01",
        "C-PPPP22-X002-d02\tC-PPPP22-X002-n02",
        "C-PPPP22-X003-d03\t",
    ]
    assert out == new_header + "".join(ln + "\n" for ln in data_lines)
    assert "\tignored\n" not in out


def test_atomic_no_partial_file_on_failure(tmp_path, monkeypatch):
    cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
    target = tmp_path / "99902.cohort.txt"

    def boom(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(cohort_crf.os, "replace", boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        write_crf(str(tmp_path), cohort, [("t", "n")])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []  # no leftover temp file


def test_atomic_failure_preserves_existing_file(tmp_path, monkeypatch):
    cohort = prepare_cohort(copy.deepcopy(MINIMAL_COHORT))
    target = tmp_path / "99902.cohort.txt"
    target.write_text("old content\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(cohort_crf.os, "replace", boom)
    with pytest.raises(OSError):
        write_crf(str(tmp_path), cohort, [("t", "n")], existing="old content\n")
    assert target.read_text(encoding="utf-8") == "old content\n"
    assert list(tmp_path.iterdir()) == [target]


# ---------------------------------------------------------------------------
# normalize_id / clean_nones unit cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("C-PPPP16-X001-d01", "C-PPPP16-X001-d01"),  # plain, no change
        ("s_C_PPPP16_X001_d01", "C-PPPP16-X001-d01"),  # s_ prefix + underscores
        ("IGO_C_PPPP17_X002_d02", "C-PPPP17-X002-d02"),  # IGO- prefix
        ("s_C-PPPP16-X001-d01", "C-PPPP16-X001-d01"),  # s- prefix, dashes only
        ("s_IGO_C_AAAAAB_P001_d", "C-AAAAAB-P001-d"),  # both prefixes stacked
        ("P-12345", "P-12345"),  # no prefix, no underscores
    ],
)
def test_normalize_id(raw, expected):
    assert normalize_id(raw) == expected


def test_clean_nones_nested():
    src = {"a": None, "b": [1, None, {"c": None, "d": "x"}], "e": "y"}
    assert clean_nones(src) == {"b": [1, {"d": "x"}], "e": "y"}
    # input untouched
    assert src == {"a": None, "b": [1, None, {"c": None, "d": "x"}], "e": "y"}


def test_clean_nones_scalar_passthrough():
    assert clean_nones("x") == "x"
    assert clean_nones(5) == 5
