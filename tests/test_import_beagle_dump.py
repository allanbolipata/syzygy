"""Unit tests for runId aggregation in scripts/import_beagle_dump.py.

Beagle filemetadata rows carry a ``runId`` per file (e.g.
``"zzmachine_A_L001_R1"``). The importer must aggregate the distinct
values per sample (across all of its file rows, including anonymous rows
that attach via igoRequestId) and persist them verbatim under
``additional_metadata["run_ids"]`` — normalization (lowercase, prefix
split) happens at pairing time, not at write time.

All values are synthetic; no real runId or sample identifiers.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from import_beagle_dump import merge_samples, to_sample_kwargs


def test_merge_samples_aggregates_run_ids_deduped():
    """runId values from a sample's file rows are aggregated into a
    distinct, order-preserving list; samples without a runId get no
    run_ids key."""
    rows = [
        ("LIMS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L001_R1"}),
        ("LIMS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L001_R1"}),  # duplicate
        ("ACCESS", {"primaryId": "P-1", "tumorOrNormal": "Tumor", "runId": "zzmachine_A_L002_R2"}),
        ("LIMS", {"primaryId": "P-2", "tumorOrNormal": "Normal", "runId": "zztank_B_L001_R1"}),
        ("LIMS", {"primaryId": "P-3", "tumorOrNormal": "Tumor"}),
    ]
    samples = merge_samples(rows)
    assert samples["P-1"]["kwargs"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L002_R2",
    ]
    assert samples["P-2"]["kwargs"]["run_ids"] == ["zztank_B_L001_R1"]
    assert "run_ids" not in samples["P-3"]["kwargs"]


def test_merge_samples_anonymous_run_ids_follow_target():
    """An anonymous row (no primaryId) carrying a runId attaches to the
    sample whose igoRequestId matches, and its runId accumulates into
    that sample's aggregated list."""
    rows = [
        (
            "LIMS",
            {
                "primaryId": "P-1",
                "tumorOrNormal": "Tumor",
                "igoRequestId": "IGO-1",
                "runId": "zzmachine_A_L001_R1",
            },
        ),
        (
            "ACCESS",
            {
                "igoRequestId": "IGO-1",
                "tumorOrNormal": "Tumor",
                "runId": "zzmachine_A_L003_R3",
            },
        ),
    ]
    samples = merge_samples(rows)
    assert "P-1" in samples
    assert samples["P-1"]["kwargs"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L003_R3",
    ]


def test_to_sample_kwargs_stores_run_ids_in_additional_metadata():
    """The aggregated run_ids land in additional_metadata["run_ids"],
    verbatim (no normalization at write time) and deduped; run_ids must
    NOT leak as a top-level Sample kwarg."""
    merged = {
        "primaryId": "P-1",
        "tumorOrNormal": "Tumor",
        "cmoSampleName": "P1 name",
        "genePanel": "HC_IMPACT",
        "baitSet": "IMPACT505_BAITS",
        "run_ids": ["zzmachine_A_L001_R1", "zzmachine_A_L002_R2", "zzmachine_A_L001_R1"],
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert kwargs["additional_metadata"]["run_ids"] == [
        "zzmachine_A_L001_R1",
        "zzmachine_A_L002_R2",
    ]
    assert "run_ids" not in kwargs


def test_to_sample_kwargs_run_ids_verbatim_no_normalization():
    """Raw runId casing is preserved (lowercasing happens at pairing
    time, not at write time)."""
    merged = {
        "primaryId": "P-2",
        "tumorOrNormal": "Normal",
        "cmoSampleName": "P2 name",
        "run_ids": ["ZzTank_S1_L005_R1"],
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert kwargs["additional_metadata"]["run_ids"] == ["ZzTank_S1_L005_R1"]


def test_to_sample_kwargs_without_run_ids_omits_key():
    """A sample record without runIds gets no run_ids key at all (not
    an empty list) — legacy rows keep their additional_metadata shape."""
    merged = {
        "primaryId": "P-3",
        "tumorOrNormal": "Tumor",
        "cmoSampleName": "P3 name",
    }
    kwargs, reason = to_sample_kwargs(merged)
    assert reason == ""
    assert "run_ids" not in (kwargs.get("additional_metadata") or {})
