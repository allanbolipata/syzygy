import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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
    # Nullable: shared (pooled) normals belong to no patient.
    patient__cmo: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
    # DMP-only (sample_type == "DMP"): the DMP "P-" patient id
    # (patient.dmp in the DMP metadata); None for LIMS/IGO samples.
    patient__dmp: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
    # DMP-only (sample_type == "DMP"): the DMP anonymous sample id
    # (anon_id in the DMP metadata); None for LIMS/IGO samples.
    anon_id: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
    sample_type: Mapped[str] = mapped_column(sa.String(255))
    gene_panel: Mapped[str] = mapped_column(sa.String(255))
    # Nullable: DMP samples carry no bait-set key.
    bait_set: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    tumor_or_normal: Mapped[str] = mapped_column(sa.Enum("TUMOR", "NORMAL", name="tumor_or_normal"))
    # Raw preservation data (e.g. ["FFPE"]) and sample origin (e.g.
    # ["Fresh or Frozen"]); either a list or a scalar string. Resolved to
    # ffpe/frozen at pairing time (see pairing_strategies.resolve_preservation).
    preservation: Mapped[Optional[Any]] = mapped_column(sa.JSON, nullable=True)
    sample_origin: Mapped[Optional[Any]] = mapped_column(sa.JSON, nullable=True)
    # Raw run-mode / sequencer string (e.g. "NovaSeq X 1000"); normalized
    # into a bucket at pairing time (see pairing_strategies.normalize_run_mode).
    run_mode: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    additional_metadata: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    errors: Mapped[Optional[list[str]]] = mapped_column(sa.JSON, nullable=True)
    sequencing_date: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Provenance: the beagle file group this record came from (e.g. "LIMS",
    # "DMP BAMs", "Pooled Normals"); None for rows created outside a beagle
    # import (dashboard fixtures, manual API creates).
    file_group: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
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
    # Keys that do not match on a forced (override) pairing; None/empty
    # means the pair satisfies every matching key.
    mismatched_keys: Mapped[Optional[list[str]]] = mapped_column(sa.JSON, nullable=True)
    # Tier/selection notes from the strategy (which candidates were
    # considered and why others were skipped); None when nothing notable.
    notes: Mapped[Optional[list[str]]] = mapped_column(sa.JSON, nullable=True)
    # How this pairing was created: "manual", "override", or
    # "automatic"; legacy rows are None.
    trigger_type: Mapped[Optional[str]] = mapped_column(sa.String(20), nullable=True)
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


class PooledMachine(Base):
    """Machine reference data for ARGOS pooled-normal run-mode
    resolution: the machine name (a pooled normal's raw ``run_mode``
    value, e.g. "diana") maps to a machine class (``novaseq``/
    ``hiseq``) and a display type. Seeded at startup by
    ``pairing_strategies.seed_pooled_machines`` and maintainable via
    the ``/v1/machines`` API. Used by ``ArgosPairingStrategy`` for
    pooled-tier run-mode matching."""
    __tablename__ = "pooled_machines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    machine_name: Mapped[str] = mapped_column(sa.String(100), unique=True, index=True)
    machine_class: Mapped[str] = mapped_column(sa.String(50), index=True)
    machine_type: Mapped[Optional[str]] = mapped_column(sa.String(100), nullable=True)
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BaitSet(Base):
    """Bait set -> primary pairing logic assignment. Seeded at startup by
    ``bait_sets.seed_bait_sets``; maintainable via ``/v1/bait-sets``."""
    __tablename__ = "bait_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bait_set: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    pairing_logic_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), sa.ForeignKey("pairing_logic.id"), index=True)
    pairing_logic_obj: Mapped[PairingLogic] = relationship(foreign_keys=[pairing_logic_id], lazy="joined")
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def pairing_logic(self) -> str:
        return self.pairing_logic_obj.name if self.pairing_logic_obj is not None else ""


class PendingNatsPublish(Base):
    """Outbox row for a NATS publish the listener could not complete.

    The listener acks every inbound message unconditionally, so a failed
    publish is recorded here instead of dropped; the listener-side
    flusher drains these rows (oldest first) and stamps ``published_at``
    once the republish lands (routers/publish_outbox.py)."""

    __tablename__ = "pending_nats_publishes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[str] = mapped_column(sa.String(255), index=True)
    subject: Mapped[str] = mapped_column(sa.String(255))
    report_json: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class AllowedUser(Base):
    """Keycloak users allowed to log in (allow-list for auth, maintained
    ahead of the Keycloak integration)."""

    __tablename__ = "allowed_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
