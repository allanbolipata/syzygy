from typing import Dict, Optional, Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import PairingLogic, Sample
from schemas import TumorOrNormal


class PairingStrategy(Protocol):
    def matches(self, tumor: Sample, normal: Sample) -> bool:
        ...

    def select_latest_normal(self, db: Session, tumor: Sample) -> Optional[Sample]:
        ...

    def select_matching_tumors(self, db: Session, normal: Sample) -> list[Sample]:
        ...


strategies: Dict[str, PairingStrategy] = {}


def register_pairing_strategy(name: str):
    def decorator(cls):
        strategies[name] = cls()
        return cls
    return decorator


@register_pairing_strategy("default")
class DefaultPairingStrategy:
    def matches(self, tumor: Sample, normal: Sample) -> bool:
        return (
            tumor.patient_id == normal.patient_id
            and tumor.sample_type == normal.sample_type
            and tumor.gene_panel == normal.gene_panel
            and tumor.bait_set == normal.bait_set
        )

    def select_latest_normal(self, db: Session, tumor: Sample) -> Optional[Sample]:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.NORMAL.value,
            Sample.patient_id == tumor.patient_id,
            Sample.sample_type == tumor.sample_type,
            Sample.gene_panel == tumor.gene_panel,
            Sample.bait_set == tumor.bait_set,
        ).order_by(
            Sample.sequencing_date.desc().nulls_last(),
            Sample.created_date.desc(),
            Sample.id.desc(),
        )
        return db.scalar(stmt)

    def select_matching_tumors(self, db: Session, normal: Sample) -> list[Sample]:
        stmt = select(Sample).where(
            Sample.tumor_or_normal == TumorOrNormal.TUMOR.value,
            Sample.patient_id == normal.patient_id,
            Sample.sample_type == normal.sample_type,
            Sample.gene_panel == normal.gene_panel,
            Sample.bait_set == normal.bait_set,
        )
        return db.scalars(stmt).all()


for name in ("IMPACT", "TEMPO", "ACCESS"):
    strategies[name] = strategies["default"]


def get_pairing_strategy(logic: PairingLogic) -> PairingStrategy:
    strategy = strategies.get(logic.strategy_name)
    if strategy is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pairing strategy: {logic.strategy_name}",
        )
    return strategy
