import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PairingLogic(Base):
    __tablename__ = "pairing_logic"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    strategy_name: Mapped[str] = mapped_column(sa.String(100), default="default")
    config: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    primary_id: Mapped[str] = mapped_column(sa.String(255), index=True)
    cmo_sample_name: Mapped[str] = mapped_column(sa.String(255))
    patient_id: Mapped[str] = mapped_column(sa.String(255), index=True)
    sample_type: Mapped[str] = mapped_column(sa.String(255))
    gene_panel: Mapped[str] = mapped_column(sa.String(255))
    bait_set: Mapped[str] = mapped_column(sa.String(255))
    tumor_or_normal: Mapped[str] = mapped_column(sa.Enum("TUMOR", "NORMAL", name="tumor_or_normal"))
    additional_metadata: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    errors: Mapped[Optional[list[str]]] = mapped_column(sa.JSON, nullable=True)
    sequencing_date: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PairingTable(Base):
    __tablename__ = "pairing_table"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pairing_logic_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), sa.ForeignKey("pairing_logic.id"), index=True)
    tumor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), sa.ForeignKey("samples.id"), index=True)
    normal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), sa.ForeignKey("samples.id"), index=True)
    tumor: Mapped[Sample] = relationship(foreign_keys=[tumor_id], lazy="joined")
    normal: Mapped[Sample] = relationship(foreign_keys=[normal_id], lazy="joined")
    pairing_logic_obj: Mapped[PairingLogic] = relationship(foreign_keys=[pairing_logic_id], lazy="joined")
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    latest: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    @property
    def pairing_logic(self) -> str:
        return self.pairing_logic_obj.name if self.pairing_logic_obj is not None else ""

    __table_args__ = (
        sa.Index(
            "uq_pairing_table_tumor_pairing_logic_latest",
            "tumor_id",
            "pairing_logic_id",
            unique=True,
            postgresql_where=sa.text("latest"),
            sqlite_where=sa.text("latest"),
        ),
        sa.Index(
            "uq_pairing_table_tumor_normal_pairing_logic_latest",
            "tumor_id",
            "normal_id",
            "pairing_logic_id",
            unique=True,
            postgresql_where=sa.text("latest"),
            sqlite_where=sa.text("latest"),
        ),
    )
