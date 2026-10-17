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
    bait_set: str
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
    bait_set: str
    tumor_or_normal: TumorOrNormal
    run_mode: Optional[str] = None
    preservation: Optional[Any] = None
    sample_origin: Optional[Any] = None
    additional_metadata: Optional[dict[str, Any]] = None
    errors: Optional[list[str]] = None
    sequencing_date: Optional[datetime] = None
    created_date: datetime
    modified_date: datetime


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
    # How the pairing was created: "manual", "override", or "automatic"
    # (legacy rows: None).
    trigger_type: Optional[str] = None
    # Keys that did not match on a forced (override) pairing.
    mismatched_keys: Optional[list[str]] = None
    # Tier/selection notes (candidates considered, why others were skipped).
    notes: Optional[list[str]] = None
    created_date: datetime
    modified_date: datetime
    latest: bool
