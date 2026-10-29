"""Translate DMP sample records into syzygy Sample kwargs.

DMP records (beagle ``file_system.filemetadata`` dumps from the DMP BAM
file group) use a different schema from LIMS/IGO filemetadata. This
module is the single point of translation so the same mapping can be
used by the dev ingestion script (``scripts/populate_from_beagle.py``)
and, later, the real DMP connector.

The ENTIRE DMP record is stored. Field mapping (DMP metadata ->
syzygy Sample):
    sample              -> primary_id            (cmo_sample_name fallback)
    external_id         -> cmo_sample_name       (DMP's CMO sample label)
    patient.cmo         -> patient__cmo            ("C-" prefixed)
    patient.dmp         -> patient__dmp            (as-is, no prefix)
    anon_id             -> anon_id
    cmo_assay           -> gene_panel            (fallback: ASSAY_MAPPING[assay])
    type ("T"/"N")      -> tumor_or_normal       (TUMOR / NORMAL)
    "DMP" (constant)    -> sample_type           (provenance marker)
    every other key     -> additional_metadata   (verbatim under the
                                                  original key; null/empty
                                                  values skipped)

Only the column-mapped fields (EXCLUDED_DMP_KEYS) are excluded from
additional_metadata; everything else is kept, including project_name and
the DMP "sample_type" key — note that this key is a 1/0 metastasis flag,
NOT a sample type; storing it verbatim under its original name is
intentional.

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

# Fields mapped to Sample columns (or used to build them). These are the
# ONLY keys excluded from additional_metadata; every other key in the
# DMP record is stored there verbatim.
EXCLUDED_DMP_KEYS = (
    "sample",
    "external_id",
    "type",
    "patient",
    "cmo_assay",
    "assay",
    "anon_id",
)


def resolve_gene_panel(meta: dict) -> Optional[str]:
    """DMP ``cmo_assay`` (already a CMO panel name), else ASSAY_MAPPING[assay]."""
    panel = meta.get("cmo_assay")
    if panel:
        return panel
    assay = meta.get("assay")
    return ASSAY_MAPPING.get(assay) if assay else None


def unmapped_metadata(meta: dict) -> dict:
    """The DMP record's keys that are not mapped to columns, verbatim.

    Null and empty-string values are skipped; ``0`` / ``False`` are kept.
    """
    return {
        k: v for k, v in meta.items()
        if k not in EXCLUDED_DMP_KEYS and v is not None and v != ""
    }


def to_dmp_sample_kwargs(sample_id: str, meta: dict) -> dict:
    """Build ``Sample`` kwargs for one DMP record (the entire record).

    Parameters
    ----------
    sample_id: str
        The DMP ``sample`` value (e.g. ``P-0001234-N01-IM6``).
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
    return dict(
        primary_id=sample_id,
        cmo_sample_name=meta.get("external_id") or sample_id,
        patient__cmo=f"C-{patient_cmo}" if patient_cmo else None,
        # Stored as-is (e.g. "P-0045965") — no prefix; None when absent.
        patient__dmp=patient.get("dmp") or None,
        anon_id=meta.get("anon_id") or None,
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
        additional_metadata=unmapped_metadata(meta) or None,
        errors=None,
    )
