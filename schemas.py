from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TumorOrNormal(str, Enum):
    TUMOR = "TUMOR"
    NORMAL = "NORMAL"


class SampleCreate(BaseModel):
    primary_id: str
    cmo_sample_name: str
    # Optional: shared (pooled) normals belong to no patient.
    patient_id: Optional[str] = None
    request_id: Optional[str] = None
    sample_type: str
    gene_panel: str
    # Optional: DMP samples carry no bait-set key.
    bait_set: Optional[str] = None
    tumor_or_normal: TumorOrNormal
    # Raw run-mode / sequencer string (e.g. "NovaSeq X 1000"); normalized
    # into a bucket at pairing time (argos strategy).
    run_mode: Optional[str] = None
    # Raw preservation data, e.g. ["FFPE"] or "FFPE"; and sample origin,
    # e.g. ["Fresh or Frozen"]. Resolved to ffpe/frozen at pairing time
    # (argos strategy).
    preservation: Optional[Any] = None
    sample_origin: Optional[Any] = None
    additional_metadata: Optional[dict[str, Any]] = None
    errors: Optional[list[str]] = None
    sequencing_date: Optional[datetime] = None
    # Provenance: source beagle file group (beagle imports only).
    file_group: Optional[str] = None
    pairing_logic_id: Optional[UUID] = None
    pairing_logic_name: Optional[str] = None


class SampleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    primary_id: str
    cmo_sample_name: str
    patient_id: Optional[str] = None
    request_id: Optional[str] = None
    sample_type: str
    gene_panel: str
    bait_set: Optional[str] = None
    tumor_or_normal: TumorOrNormal
    run_mode: Optional[str] = None
    preservation: Optional[Any] = None
    sample_origin: Optional[Any] = None
    additional_metadata: Optional[dict[str, Any]] = None
    errors: Optional[list[str]] = None
    sequencing_date: Optional[datetime] = None
    file_group: Optional[str] = None
    created_date: datetime
    modified_date: datetime


class PatientRead(BaseModel):
    # Patients are derived from samples (``patient_id`` non-null); there is
    # no separate patient table yet.
    patient_id: str
    sample_count: int
    tumor_count: int
    normal_count: int
    # Distinct request_ids across the patient's samples.
    requests: list[str]
    # Patient-level fields derived from the samples' additional_metadata
    # (first non-null value per key; tumor samples take priority).
    metadata: dict


class RequestRead(BaseModel):
    # Requests are derived from samples (``request_id`` non-null); there is
    # no separate request table yet.
    request_id: str
    sample_count: int
    tumor_count: int
    normal_count: int
    # Distinct patient_ids across the request's samples (non-null).
    patients: list[str]
    # Distinct sample_types / gene_panels across the request's samples.
    sample_types: list[str]
    gene_panels: list[str]


class PairingLogicCreate(BaseModel):
    name: str
    description: Optional[str] = None
    active: bool = True
    strategy_name: str = "default"
    config: Optional[dict[str, Any]] = None


class PairingLogicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str] = None
    active: bool
    strategy_name: str
    config: Optional[dict[str, Any]] = None
    created_date: datetime


class PairingTriggerCreate(BaseModel):
    tumor_id: UUID
    trigger_type: Literal["manual", "override", "automatic"]
    pairing_logic_id: Optional[UUID] = None
    pairing_logic_name: Optional[str] = None
    # Pin a specific normal. When given, the pairing is forced even if
    # matching keys differ (see ``strict``); recorded in mismatched_keys.
    normal_id: Optional[UUID] = None
    # For a pinned normal_id: True rejects the pairing with 400 if any
    # matching key differs; False (default) creates it anyway and records
    # the mismatches in the pairing's mismatched_keys.
    strict: bool = False


class PairingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pairing_logic_id: UUID
    pairing_logic: str
    tumor_id: UUID
    normal_id: UUID
    tumor: SampleRead
    normal: SampleRead
    # How this pairing was created: "manual", "override" or "automatic"
    # (legacy rows: None).
    trigger_type: Optional[str] = None
    # Keys that did not match on a forced (override) pairing.
    mismatched_keys: Optional[list[str]] = None
    # Tier/selection notes (candidates considered, why others were skipped).
    notes: Optional[list[str]] = None
    created_date: datetime
    modified_date: datetime
    latest: bool


class TriggerAllRead(BaseModel):
    """Summary of a POST /pairing/trigger-all backfill run.

    ``scanned`` is the total number of tumor samples processed and the
    per-outcome counts sum to it. ``new_pairings`` lists the pairings
    created by this run (tumors that previously had no latest pairing
    under their bait-set-resolved logic).
    """

    model_config = ConfigDict(from_attributes=True)

    scanned: int
    already_paired: int
    paired: int
    unpaired: int
    skipped_dmp: int
    skipped_no_logic: int
    skipped_inactive_logic: int
    new_pairings: list[PairingRead]


class PossibleMatchCandidate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sample: SampleRead
    # Cascade tier the candidate would be considered in: "same_request",
    # "other_request", or "pooled".
    tier: str
    # True when the candidate passes every matching key of the strategy.
    matched: bool
    # Why the candidate does not match (empty when ``matched`` is true).
    mismatched_keys: Optional[list[str]] = None
    # For a tumor input: True when the strategy's cascade would pick this
    # candidate (False for normal inputs).
    would_be_selected: bool = False


class CurrentMatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pairing_id: UUID
    trigger_type: Optional[str] = None
    mismatched_keys: Optional[list[str]] = None
    notes: Optional[list[str]] = None
    sample: SampleRead


class PossibleMatchesRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sample: SampleRead
    pairing_logic: str
    # First/primary current match (backward compatibility). Prefer
    # ``current_matches``: a tumor has at most one, a normal matches
    # 1-to-many and lists every currently-paired tumor here.
    current_match: Optional[CurrentMatchRead] = None
    current_matches: list[CurrentMatchRead] = []
    # ``other_matches`` is a bounded page (default 500) of the
    # strategy's candidate scope; the fields below describe the full
    # scope regardless of the page.
    other_matches: list[PossibleMatchCandidate] = []
    other_matches_total: int = 0
    other_matches_matched: int = 0
    other_matches_tiers: dict[str, int] = {}
    other_matches_breakdown: list[dict] = []


class PooledMachineUpsert(BaseModel):
    machine_name: str
    machine_class: str
    machine_type: Optional[str] = None


class PooledMachineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    machine_name: str
    machine_class: str
    machine_type: Optional[str] = None
    created_date: datetime
    modified_date: datetime


class BaitSetUpsert(BaseModel):
    bait_set: str
    pairing_logic_name: str


class BaitSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bait_set: str
    pairing_logic_id: UUID
    pairing_logic: str
    created_date: datetime
    modified_date: datetime
