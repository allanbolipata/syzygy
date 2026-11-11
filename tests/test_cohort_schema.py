"""Schema vendoring + validation helpers (T3).

Covers the G3 preprocessing contract for ``validate_cohort_request``
(raw SMILE payload in → schema-validated, legacy-preprocessed cohort out,
ready for ``cohort_crf.to_crf``), the schema's rejection paths, and the
report self-check against the fixed validator schema.
"""

import copy

import jsonschema
import pytest

from cohort_schema import strip_pi_name, validate_cohort_request, validate_report


def _valid_payload():
    """A raw SMILE cohort request: SMILE always sends both primaryId and
    cmoId per sample (documented assumption, G7e), plus the keys the
    legacy pipeline strips (piName/status/date) and a null optional."""
    return {
        "cohortId": "99910",
        "piName": "Dr. Investigator",
        "status": "PASS",
        "date": "2026-10-29",
        "endUsers": ["analyst@example.org"],
        "pmUsers": ["pm@example.org"],
        "projectTitle": "Schema Test Cohort",
        "projectSubtitle": None,
        "samples": [
            {
                "cmoId": "s_C_PPPP99_X001_d01",
                "normalCmoId": "C-PPPP99-X001-n01",
                "primaryId": "99910_A_1_1_1",
            }
        ],
    }


# ---------------------------------------------------------------------------
# strip_pi_name
# ---------------------------------------------------------------------------


def test_strip_pi_name_copies_and_strips():
    raw = _valid_payload()
    original = copy.deepcopy(raw)
    out = strip_pi_name(raw)
    assert out is not raw
    for key in ("piName", "status", "date"):
        assert key not in out
    for key in ("cohortId", "samples", "projectTitle"):
        assert key in out
    # input untouched
    assert raw == original


# ---------------------------------------------------------------------------
# validate_cohort_request
# ---------------------------------------------------------------------------


def test_valid_cohort_passes_and_comes_back_cleaned():
    raw = _valid_payload()
    out = validate_cohort_request(raw)
    # stripped by design
    assert "piName" not in out
    assert "status" not in out
    assert "date" not in out
    # G3: legacy Cohort.__init__ semantics — exactly what to_crf expects
    assert out["type"] == "investigator"  # defaulted when absent
    assert "projectSubtitle" not in out  # null removed by clean_nones
    assert out["samples"][0]["cmoId"] == "C-PPPP99-X001-d01"  # normalize_id
    assert out["cohortId"] == "99910"
    # input not mutated
    assert "piName" in raw and "status" in raw
    assert raw["samples"][0]["cmoId"] == "s_C_PPPP99_X001_d01"


def test_missing_required_field_raises():
    raw = _valid_payload()
    del raw["projectTitle"]
    with pytest.raises(jsonschema.ValidationError):
        validate_cohort_request(raw)


def test_unknown_top_level_key_raises():
    raw = _valid_payload()
    raw["bogusKey"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        validate_cohort_request(raw)


def test_sample_embargo_date_accepted():
    """Per-sample embargoDate is accepted by the schema (and must be
    ignored downstream, G9)."""
    raw = _valid_payload()
    raw["samples"][0]["embargoDate"] = "2027-01-31"
    out = validate_cohort_request(raw)
    assert out["samples"][0]["embargoDate"] == "2027-01-31"


# ---------------------------------------------------------------------------
# validate_report (self-check against the fixed validator schema)
# ---------------------------------------------------------------------------


def _passing_report():
    return {
        "cohortId": "99910",
        "jsonSchemaValidated": True,
        "passesCheck": False,
        "samples": [
            {
                "primaryId": "99910_A_1_1_1",
                "cmoId": "C-PPPP99-X001-d01",
                "conflictReason": "mismatched_keys",
            }
        ],
    }


def test_passing_report_validates():
    validate_report(_passing_report())  # must not raise


def test_report_missing_passes_check_raises():
    report = _passing_report()
    del report["passesCheck"]
    with pytest.raises(jsonschema.ValidationError):
        validate_report(report)
