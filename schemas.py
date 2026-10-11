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
    patient_id: str
    sample_type: str
    gene_panel: str
    bait_set: str
    tumor_or_normal: TumorOrNormal
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
    patient_id: str
    sample_type: str
    gene_panel: str
    bait_set: str
    tumor_or_normal: TumorOrNormal
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
    trigger_type: Literal["manual", "override"]
    pairing_logic_id: Optional[UUID] = None
    pairing_logic_name: Optional[str] = None
    normal_id: Optional[UUID] = None
    override: bool = False


class PairingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pairing_logic_id: UUID
    pairing_logic: str
    tumor_id: UUID
    normal_id: UUID
    tumor: SampleRead
    normal: SampleRead
    created_date: datetime
    modified_date: datetime
    latest: bool
