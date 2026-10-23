"""Translate DMP sample records into syzygy Sample kwargs.

DMP records (beagle ``file_system.filemetadata`` dumps from the DMP BAM
file group) use a different schema from LIMS/IGO filemetadata. This
module is the single point of translation so the same mapping can be
used by the dev ingestion script (``scripts/populate_from_beagle.py``)
and, later, the real DMP connector.

Field mapping (DMP metadata -> syzygy Sample):
    sample              -> primary_id            (cmo_sample_name fallback)
    external_id         -> cmo_sample_name       (DMP's CMO sample label)
    patient.cmo         -> patient_id            ("C-" prefixed)
    cmo_assay           -> gene_panel            (fallback: ASSAY_MAPPING[assay])
    type ("T"/"N")      -> tumor_or_normal       (TUMOR / NORMAL)
    "DMP" (constant)    -> sample_type           (provenance marker)
    coverage,
    major_allele_contamination,
    minor_allele_contamination -> additional_metadata (allowlisted only)

Deliberately NOT stored:
    anon_id            -- patient-identifying; excluded for security.
    patient.dmp, project_name, sample_type (DMP's 1/0 metastasis flag)
                       -- deferred; move to additional_metadata later.

DMP records carry no IGO/LIMS keys, so request_id, run_mode,
preservation, sample_origin, and sequencing_date are None; bait_set is set
to the gene_panel (beagle logic). In the ARGOS strategy DMP samples are
manual-pairing only: the automatic cascade never selects them, and any
pair involving a DMP sample reports a permanent "DMP: manual pairing only"
mismatch reason. Same-patient DMP samples are listed as potential matches
on the Sample Pairing page (candidate scope, tier "dmp") so an operator
can pin them via an override.
"""
from __future__ import annotations

from typing import Optional

ASSAY_MAPPING: dict[str, str] = {
    # DMP assay : CMO assay
    "IM3": "IMPACT341",
    "IM5": "IMPACT410",
    "IM6": "IMPACT468",
    "IM7": "IMPACT505",
    "IH3": "HEMEPACT_v3",
    "IH4": "HEMEPACT_v4",
}

# DMP ``sample_type`` ("1"/"0") records whether the tumor metastasized;
# irrelevant for pairing, kept out of the schema for now.
EXCLUDED_DMP_KEYS = ("anon_id", "sample_type", "project_name")

ADDITIONAL_METADATA_KEYS = (
    "coverage",
    "major_allele_contamination",
    "minor_allele_contamination",
)


def resolve_gene_panel(meta: dict) -> Optional[str]:
    """DMP ``cmo_assay`` (already a CMO panel name), else ASSAY_MAPPING[assay]."""
    panel = meta.get("cmo_assay")
    if panel:
        return panel
    assay = meta.get("assay")
    return ASSAY_MAPPING.get(assay) if assay else None


def to_dmp_sample_kwargs(sample_id: str, meta: dict) -> dict:
    """Build ``Sample`` kwargs for one DMP record.

    Parameters
    ----------
    sample_id: str
        The DMP ``sample`` value (e.g. ``P-0001231-T01-IM6``).
    meta: dict
        The record's ``metadata`` dict from the beagle dump.
    """
    tumor_type = meta.get("type")
    if tumor_type not in ("T", "N"):
        raise ValueError(
            f"DMP record {sample_id!r} has unknown type {tumor_type!r}; expected 'T' or 'N'"
        )

    patient = meta.get("patient") or {}
    patient_cmo = patient.get("cmo")
    additional = {
        k: meta[k] for k in ADDITIONAL_METADATA_KEYS if meta.get(k) is not None
    }
    return dict(
        primary_id=sample_id,
        cmo_sample_name=meta.get("external_id") or sample_id,
        patient_id=f"C-{patient_cmo}" if patient_cmo else None,
        request_id=None,
        sample_type="DMP",
        gene_panel=resolve_gene_panel(meta),
        # Beagle logic: DMP has no bait set; its gene panel stands in so the
        # sample can match LIMS data on the (gene_panel, bait_set) keys.
        bait_set=resolve_gene_panel(meta),
        tumor_or_normal="TUMOR" if tumor_type == "T" else "NORMAL",
        run_mode=None,
        preservation=None,
        sample_origin=None,
        sequencing_date=None,
        additional_metadata=additional or None,
        errors=None,
    )
