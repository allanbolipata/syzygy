import pytest

from dmp_translator import ASSAY_MAPPING, to_dmp_sample_kwargs


def _meta(**overrides):
    meta = {
        "type": "N",
        "assay": "IM6",
        "sample": "P-0001234-N01-IM6",
        "anon_id": "anon-should-not-leak",
        "patient": {"cmo": "8VK0V7", "dmp": "P-0001234"},
        "cmo_assay": "IMPACT468",
        "external_id": "EXT-0001234-N01",
        "sample_type": "0",
        "project_name": "proj",
        "coverage": 687,
        "major_allele_contamination": 0.451,
        "minor_allele_contamination": 0.00085,
        "tumor_type": "lung",
        "tissue_type": "fresh",
    }
    meta.update(overrides)
    return meta


def test_core_mapping():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    assert kw["primary_id"] == "P-0001234-N01-IM6"
    assert kw["cmo_sample_name"] == "EXT-0001234-N01"
    assert kw["patient_id"] == "C-8VK0V7"
    assert kw["gene_panel"] == "IMPACT468"
    assert kw["tumor_or_normal"] == "NORMAL"
    assert kw["sample_type"] == "DMP"
    assert kw["bait_set"] == kw["gene_panel"]
    assert kw["run_mode"] is None
    assert kw["request_id"] is None
    assert kw["preservation"] is None
    assert kw["sample_origin"] is None
    assert kw["sequencing_date"] is None
    assert kw["additional_metadata"] == {
        "coverage": 687,
        "major_allele_contamination": 0.451,
        "minor_allele_contamination": 0.00085,
    }


def test_anon_id_and_deferred_keys_not_stored():
    kw = to_dmp_sample_kwargs("P-0001234-N01-IM6", _meta())
    assert "anon_id" not in kw
    extra = kw["additional_metadata"] or {}
    for key in ("anon_id", "patient.dmp", "project_name", "sample_type.dmp", "patient"):
        assert key not in extra


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
    assert kw["patient_id"] is None
    # cmo_sample_name falls back to the DMP sample id when external_id is missing.
    kw2 = to_dmp_sample_kwargs("P-1-T01-IM6", _meta(external_id=None))
    assert kw2["cmo_sample_name"] == "P-1-T01-IM6"


def test_unknown_type_rejected():
    with pytest.raises(ValueError):
        to_dmp_sample_kwargs("P-1-X01-IM6", _meta(type="X"))


def test_argos_skips_run_mode_and_preservation_for_dmp():
    from models import Sample
    from pairing_strategies import strategies

    def mk(**kw):
        base = dict(patient_id="C-1", gene_panel="IMPACT468", bait_set="IMPACT468")
        base.update(kw)
        return Sample(**base)

    lims = mk(sample_type="Tumor", tumor_or_normal="TUMOR", run_mode="NovaSeq",
              preservation="FFPE")
    dmp = mk(sample_type="DMP", tumor_or_normal="NORMAL")
    argos = strategies["argos"]
    assert argos.matches(lims, dmp) is True
    # Non-DMP normal with missing run_mode/preservation is still rejected.
    lims_n = mk(sample_type="Normal", tumor_or_normal="NORMAL")
    assert argos.matches(lims, lims_n) is False
    # bait_set remains a hard key.
    assert argos.matches(lims, mk(sample_type="DMP", bait_set="OTHER")) is False
