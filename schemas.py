from datetime import datetime
from enum import Enum
from typing import Any, Optional
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
    created_date: datetime
    modified_date: datetime


class PairingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pairing_logic: str
    tumor_id: UUID
    normal_id: UUID
    tumor: SampleRead
    normal: SampleRead
    created_date: datetime
    modified_date: datetime
    latest: bool
