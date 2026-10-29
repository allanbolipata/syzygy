import pytest

from dmp_translator import ASSAY_MAPPING, EXCLUDED_DMP_KEYS, to_dmp_sample_kwargs


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
    # Absent patient/anon_id -> None columns.
    kw2 = to_dmp_sample_kwargs("P-1-T01-IM6", _meta(patient=None, anon_id=None))
    assert kw2["patient__dmp"] is None
    assert kw2["anon_id"] is None
    assert kw2["patient__cmo"] is None


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
    from models import Sample
    from pairing_strategies import strategies

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
