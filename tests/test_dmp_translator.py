import pytest

from syzygy.dmp_translator import (
    ASSAY_MAPPING,
    DMP_NAME_RE,
    DMP_REF_COLUMNS,
    EXCLUDED_DMP_KEYS,
    flatten_acela_record,
    parse_dmp_patient,
    remap_dmp_record,
    to_dmp_sample_kwargs,
)
from syzygy.models import Sample


def _meta(**overrides):
    # A full DMP record (beagle "DMP BAMs" filemetadata), anonymized.
    meta = {
        "type": "N",
        "assay": "IM6",
        "sample": "P-0001234-N01-IM6",
        "anon_id": "anon-0001234",
        "patient": {"cmo": "TEST123", "dmp": "P-0001234"},
        "updated": "2024-04-30T12:00:00",
        "version": 1,
        "coverage": 687,
        "imported": "2023-02-21T09:00:00",
        "cmo_assay": "IMPACT468",
        "tumor_type": "lung",
        "external_id": "EXT-0001234-N01",
        # DMP's 1/0 metastasis flag — NOT a sample type; stored verbatim.
        "sample_type": "0",
        "tissue_type": "fresh",
        "primary_site": None,
        "project_name": "proj",
        "part_c_consent": 0,
        "metastasis_site": None,
        "somatic_calling_status": None,
        "major_allele_contamination": 0.451,
        "minor_allele_contamination": 0.00085,
    }
    meta.update(overrides)
    return meta


def test_core_mapping():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    assert kw["primary_id"] == "P-0001234-N01-IM6"
    assert kw["cmo_sample_name"] == "EXT-0001234-N01"
    assert kw["patient__cmo"] == "C-TEST123"
    assert kw["gene_panel"] == "IMPACT468"
    assert kw["tumor_or_normal"] == "NORMAL"
    assert kw["sample_type"] == "DMP"
    assert kw["bait_set"] == kw["gene_panel"]
    assert kw["run_mode"] is None
    assert kw["request_id"] is None
    assert kw["preservation"] is None
    assert kw["sample_origin"] is None
    assert kw["sequencing_date"] is None


def test_identifiers_now_stored_in_columns():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    # patient__dmp is stored as-is (no prefix); anon_id in its own column.
    assert kw["patient__dmp"] == "P-0001234"
    assert kw["anon_id"] == "anon-0001234"
    # "C-" prefix rule on patient__cmo is unchanged.
    assert kw["patient__cmo"] == "C-TEST123"
    # Absent patient/anon_id: patient__dmp is imputed from the sample
    # name's reference P- prefix (t_9e6c5cd6); anon_id stays None.
    kw2 = to_dmp_sample_kwargs("P-1-T01-IM6", _meta(patient=None, anon_id=None))
    assert kw2["patient__dmp"] == "P-1"
    assert kw2["anon_id"] is None
    assert kw2["patient__cmo"] is None
    # An unparseable sample name has nothing to impute from -> None.
    kw3 = to_dmp_sample_kwargs("ISFAKE-T01", _meta(patient=None))
    assert kw3["patient__dmp"] is None


def test_patient_dmp_imputed_from_name_when_absent():
    # Beagle DMP records carry the P- id only in the sample name (no
    # explicit patient.dmp): ingest imputes patient__dmp with the
    # reference parse so HARMONY's stored-column check can see the row
    # (t_9e6c5cd6). An explicit patient.dmp always wins over the parse.
    kw = to_dmp_sample_kwargs("P-7000011-T01-IM6", _meta(patient=None, type="T"))
    assert kw["patient__dmp"] == "P-7000011"
    kw2 = to_dmp_sample_kwargs(
        "P-7000011-T01-IM6", _meta(patient={"dmp": "P-0009999"}, type="T")
    )
    assert kw2["patient__dmp"] == "P-0009999"
    # An empty-string patient.dmp is absent: the imputation applies.
    kw3 = to_dmp_sample_kwargs(
        "P-7000011-T01-IM6", _meta(patient={"dmp": ""}, type="T")
    )
    assert kw3["patient__dmp"] == "P-7000011"


def test_parse_dmp_patient_matches_reference_parse():
    # The imputation parse is the remap_dmp_record name parse (spec g):
    # group 1 of the P-<digits>-<code>-<ver> pattern, leftmost/unanchored.
    assert parse_dmp_patient("P-7000011-T01-IM6") == "P-7000011"
    assert parse_dmp_patient("P-0001234-N01-IM6") == "P-0001234"
    assert parse_dmp_patient("X-P-42-A1-B2") == "P-42"
    # No full triple -> no patient (matches the dmp_patient_key
    # generated column's regexp_match returning NULL).
    assert parse_dmp_patient("P-42-A1") is None
    assert parse_dmp_patient("ISFAKE-T01") is None
    assert parse_dmp_patient(None) is None
    assert parse_dmp_patient("") is None
    # A P- id containing letters is not a reference patient id.
    assert parse_dmp_patient("P-00ISFAK01-T01-IM6") is None
    # Shared regex keeps the same capture groups remap_dmp_record uses.
    m = DMP_NAME_RE.search("P-7000011-T01-IM6")
    assert m is not None
    assert m.groups() == ("P-7000011", "T01", "IM6")


def test_consent_placeholder_false():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    # PLACEHOLDER default (user decision 2026-11-05): every DMP sample is
    # recorded as non-consented until the DMP payload carries the real
    # consent value.
    assert kw["consent"] is False
    # The placeholder does NOT read any payload key (the real source field
    # name is not agreed yet): a truthy consent-ish flag in the payload
    # still yields False and stays verbatim in additional_metadata.
    kw2 = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta(part_c_consent=1))
    assert kw2["consent"] is False
    assert kw2["additional_metadata"]["part_c_consent"] == 1


def test_additional_metadata_carrying_every_unmapped_key_verbatim():
    meta = _meta()
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", meta)
    extra = kw["additional_metadata"]
    # Every unmapped key that is present and non-null lands verbatim.
    for key in (
        "updated",
        "version",
        "coverage",
        "imported",
        "tumor_type",
        "tissue_type",
        "project_name",
        "part_c_consent",
        "sample_type",
        "major_allele_contamination",
        "minor_allele_contamination",
    ):
        assert key in extra, f"{key} missing from additional_metadata"
        assert extra[key] == meta[key], f"{key} changed: {extra[key]!r} != {meta[key]!r}"
    # Null values are skipped.
    for key in ("primary_site", "metastasis_site", "somatic_calling_status"):
        assert key not in extra


def test_column_mapped_keys_not_duplicated_into_metadata():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    extra = kw["additional_metadata"]
    for key in EXCLUDED_DMP_KEYS:
        assert key not in extra, f"column-mapped key {key!r} leaked into additional_metadata"


def test_gene_panel_fallback_to_assay_mapping():
    meta = _meta()
    del meta["cmo_assay"]
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", meta)
    assert kw["gene_panel"] == ASSAY_MAPPING["IM6"] == "IMPACT468"
    for assay, panel in ASSAY_MAPPING.items():
        m = _meta(assay=assay)
        del m["cmo_assay"]
        assert to_dmp_sample_kwargs("s", m)["gene_panel"] == panel


def test_tumor_type_t_and_fallbacks():
    kw = to_dmp_sample_kwargs("P-1-T01-IM6", _meta(type="T", patient=None))
    assert kw["tumor_or_normal"] == "TUMOR"
    assert kw["patient__cmo"] is None
    # cmo_sample_name falls back to the DMP sample id when external_id is missing.
    kw2 = to_dmp_sample_kwargs("P-1-T01-IM6", _meta(external_id=None))
    assert kw2["cmo_sample_name"] == "P-1-T01-IM6"


def test_unknown_type_rejected():
    with pytest.raises(ValueError):
        to_dmp_sample_kwargs("P-1-X01-IM6", _meta(type="X"))


def test_argos_dmp_pairs_are_manual_pairing_only():
    from syzygy.models import Sample
    from syzygy.pairing_strategies import strategies

    def mk(**kw):
        base = dict(patient__cmo="C-1", gene_panel="IMPACT468", bait_set="IMPACT468")
        base.update(kw)
        return Sample(**base)

    lims = mk(sample_type="Tumor", tumor_or_normal="TUMOR", run_mode="NovaSeq",
              preservation="FFPE")
    dmp = mk(sample_type="DMP", tumor_or_normal="NORMAL")
    argos = strategies["argos"]
    # A DMP pair never matches, even when every other key agrees: the
    # permanent manual-only reason applies, and the run_mode/preservation
    # gates are skipped for DMP (no reasons from them).
    reasons = argos.mismatch_reasons(lims, dmp)
    assert argos.matches(lims, dmp) is False
    assert reasons == ["DMP: manual pairing only"]
    # Non-DMP normal with missing run_mode/preservation is still rejected
    # by the regular gates.
    lims_n = mk(sample_type="Normal", tumor_or_normal="NORMAL")
    assert argos.matches(lims, lims_n) is False
    # bait_set is still a hard key, reported alongside the manual reason.
    reasons = argos.mismatch_reasons(lims, mk(sample_type="DMP", bait_set="OTHER"))
    assert argos.matches(lims, mk(sample_type="DMP", bait_set="OTHER")) is False
    assert any("bait_set" in r for r in reasons)
    assert "DMP: manual pairing only" in reasons


# ---------------------------------------------------------------------------
# remap_dmp_record — the section g reference-column contract (ticket 2/8).
# 3/8 (two-tier strategy) and 4/8 (Pair Report) consume this helper.
# ---------------------------------------------------------------------------

def _dmp_sample(**meta_overrides):
    """Build a stored DMP Sample row the way ingest (or the beagle import)
    would, so remap tests run against realistic rows."""
    meta = _meta()
    meta.update(meta_overrides)
    return Sample(**to_dmp_sample_kwargs("P-0001234-N01-IM6", meta))


def test_remap_full_record_all_twelve_columns():
    out = remap_dmp_record(_dmp_sample())
    assert tuple(out.keys()) == DMP_REF_COLUMNS
    assert out["sample"] == "P-0001234-N01-IM6"
    assert out["patient"] == "P-0001234"        # stored patient__dmp wins
    assert out["idx"] == "N01"                  # parsed from the sample id
    assert out["ver"] == "IM6"                  # IM_ver for the rescue tier
    assert out["bamname"] == "anon-0001234"     # anon_id stand-in
    assert out["bprefix"] == "anon"
    assert out["bsuffix"] == "0001234"
    assert out["group"] is None                 # gap: absent from real records
    assert out["bam_exists"] == 0               # default: unresolvable
    assert out["coverage"] == 687               # as-is
    assert out["pool_id"] == "proj"             # project_name stand-in
    assert out["is_matched"] is None            # gap: no matched_tag


def test_remap_explicit_fields_take_precedence():
    s = _dmp_sample(bamname="P-777-BA9", pool_id="POOL9",
                    matched_tag="Matched", group="grp1")
    out = remap_dmp_record(s, bam_exists=1)
    assert out["bamname"] == "P-777-BA9"        # explicit beats anon_id
    assert out["bprefix"] == "P"
    assert out["bsuffix"] == "777"
    assert out["pool_id"] == "POOL9"            # explicit beats project_name
    assert out["is_matched"] == "Matched"       # explicit tag passes through
    assert out["group"] == "grp1"
    assert out["bam_exists"] == 1               # caller-resolved override


def test_remap_patient_falls_back_to_name_parse():
    # Ingest now imputes patient__dmp from the name (t_9e6c5cd6), so a
    # NULL stored column only exists on pre-imputation rows: simulate
    # one to exercise remap_dmp_record's own name-parse fallback.
    s = _dmp_sample(patient=None)
    s.patient__dmp = None
    assert s.patient__dmp is None
    assert remap_dmp_record(s)["patient"] == "P-0001234"


def test_remap_anon_id_without_dash_leaves_prefix_suffix_empty():
    # Real anon_id values are dash-free letter tokens (2/8 fixture audit):
    # bprefix/bsuffix come back "" and must not be read as a T/N signal.
    out = remap_dmp_record(_dmp_sample(anon_id="abcdefgh"))
    assert out["bamname"] == "abcdefgh"
    assert out["bprefix"] == ""
    assert out["bsuffix"] == ""


def test_remap_bam_exists_default_and_override():
    s = _dmp_sample()
    assert remap_dmp_record(s)["bam_exists"] == 0
    assert remap_dmp_record(s, bam_exists=1)["bam_exists"] == 1


def test_remap_coverage_none_is_not_zero():
    # The reference treats missing coverage as a distinct warning case —
    # never coerce NA to 0.
    s = _dmp_sample(coverage=None)
    assert remap_dmp_record(s)["coverage"] is None


# ---------------------------------------------------------------------------
# ACELA DMP bam-file envelope (contract supplied by Allan, 2026-11-20).
# Synthetic values only.
# ---------------------------------------------------------------------------

def _acela(**overrides):
    record = {
        "path": "/dmp/mirror/impact468/I/N/test.bam",
        "metadata": _meta(type="T", sample="P-0001234-T01-IM6", part_c_consent=1),
        "pathChecks": {
            "mirror": {"status": "OK", "size_bytes": 4361320375,
                       "checked_at": "2026-05-28T21:00:21"},
            "dms": {"status": "OK", "size_bytes": 4361320375,
                    "checked_at": "2026-05-28T21:00:21"},
        },
    }
    record.update(overrides)
    return record


def test_acela_envelope_flattens():
    flat = flatten_acela_record(_acela())
    assert flat["sample"] == "P-0001234-T01-IM6"
    assert flat["bam_path"] == "/dmp/mirror/impact468/I/N/test.bam"
    assert flat["bam_exists"] == 1
    assert flat["path_checks"] == _acela()["pathChecks"]
    # metadata keys survive verbatim into the flat record
    assert flat["tumor_type"] == "lung"
    assert flat["part_c_consent"] == 1


def test_acela_failed_path_check_zeroes_bam_exists():
    record = _acela()
    record["pathChecks"]["dms"]["status"] = "MISSING"
    assert flatten_acela_record(record)["bam_exists"] == 0


def test_acela_empty_path_zeroes_bam_exists():
    assert flatten_acela_record(_acela(path=""))["bam_exists"] == 0


def test_non_envelope_record_passes_through_unchanged():
    legacy = {"sample": "P-1-T01-IM6", "type": "T"}
    assert flatten_acela_record(legacy) == legacy


def test_part_c_consent_does_NOT_drive_consent():
    # Allan 2026-11-20: part_c_consent is NOT the consent source (the real
    # field comes from ACELA later, TBD). It is stored verbatim as metadata
    # and must never wire into Sample.consent — placeholder False stands.
    kw = to_dmp_sample_kwargs("P-0001234-T01-IM6", flatten_acela_record(_acela()))
    assert kw["consent"] is False          # even though part_c_consent == 1
    assert kw["additional_metadata"]["part_c_consent"] == 1  # stored verbatim


def test_remap_bam_exists_falls_back_to_stored_metadata():
    sample = Sample(primary_id="P-0001234-T01-IM6", patient__dmp="P-0001234",
                    anon_id="anon-0001234",
                    additional_metadata={"bam_exists": 1, "coverage": 687,
                                         "project_name": "proj"})
    # caller default (0 = unresolved) falls back to the stored value
    assert remap_dmp_record(sample)["bam_exists"] == 1
    assert remap_dmp_record(sample, bam_exists=0)["bam_exists"] == 1
    # an explicit caller resolution still wins over metadata
    sample0 = Sample(primary_id="P-0001234-T01-IM6",
                     additional_metadata={"bam_exists": 0})
    assert remap_dmp_record(sample0, bam_exists=1)["bam_exists"] == 1
