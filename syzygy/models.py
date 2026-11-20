import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import DDL, Uuid, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from syzygy.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Name of the pairing logic under which DMP samples pair automatically
#: (see pairing_service.DMP_LOGIC_NAME comment / trigger_dmp_pairing).
#: Lives on the models layer so SQL-side listing filters (bait_sets
#: logic_membership_condition) can reference it without importing
#: pairing_service (which itself imports bait_sets).
DMP_LOGIC_NAME = "DMP"

#: Name of the pairing logic under which HARMONY samples pair
#: automatically (see pairing_service.trigger_harmony_pairing). HARMONY is
#: its OWN pairing logic row and pairing space, separate from DMP, but it
#: reuses the ``dmp`` strategy and owns its own NULL-bait_set sample
#: family (``sample_type`` "HARMONY") the way DMP owns ``sample_type``
#: "DMP". Lives on the models layer for the same reason DMP_LOGIC_NAME
#: does: so the SQL-side listing filter (bait_sets
#: logic_membership_condition) and the HARMONY pairing trigger share one
#: source of truth.
HARMONY_LOGIC_NAME = "HARMONY"


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
    cmo_sample_name: Mapped[str] = mapped_column(sa.String(255), index=True)
    # Nullable: shared (pooled) normals belong to no patient.
    patient__cmo: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
    # DMP-only (sample_type == "DMP"): the DMP "P-" patient id
    # (patient.dmp in the DMP metadata); None for LIMS/IGO samples.
    patient__dmp: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True, index=True)
    # DMP-only (sample_type == "DMP"): the DMP anonymous sample id
    # (anon_id in the DMP metadata); None for LIMS/IGO samples.
    anon_id: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    # DMP-only (sample_type == "DMP"): whether the patient consented; DMP
    # samples default to False (placeholder until the DMP payload carries
    # consent); None for LIMS/IGO samples.
    consent: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)
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
    # Redaction (spec R1, REDACTION_TICKETS.md): a redacted sample is
    # excluded from every strategy's candidate query and the bulk
    # pairing sweeps, and explicit pairing triggers on it return 400;
    # candidate listings still show it, flagged. Redaction never retires
    # existing pairings (pairing history is immutable). Together with
    # redacted_at / redacted_by / redaction_reason these four columns
    # ARE the redaction audit trail (this branch predates the
    # audit-log feature).
    redacted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    redacted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    redacted_by: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    redaction_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # DMP sargable pairing keys (DMP bulk-pairing performance, item 1).
    # On Postgres these are GENERATED ALWAYS ... STORED columns (scripts/
    # migrate_dmp_generated_columns.py) materializing the exact key
    # expressions remap_dmp_record computes (docs/dmp_reference_alignment.md
    # section g): dmp_patient_key = COALESCE(patient__dmp, group 1 of the
    # (P-\d+) name parse), dmp_ver_key = group 3 (the IM_ver),
    # dmp_pool_id_key = additional_metadata pool_id (non-empty string) else
    # project_name, sample_type_norm = trim(upper(sample_type)). The DMP
    # strategy's candidate query filters on them via a partial index over
    # DMP rows instead of scanning the table. Generated columns recompute
    # on every update of the source columns — never written by application
    # code, and patient__dmp is never written either. On non-Postgres
    # dialects (the SQLite test fallback) create_all makes these plain
    # nullable columns left NULL; the strategy detects that (the generated
    # columns are absent) and uses the legacy Python remap path, so
    # behavior is identical.
    dmp_patient_key: Mapped[Optional[str]] = mapped_column(
        sa.Text, nullable=True, server_default=sa.FetchedValue()
    )
    dmp_ver_key: Mapped[Optional[str]] = mapped_column(
        sa.Text, nullable=True, server_default=sa.FetchedValue()
    )
    dmp_pool_id_key: Mapped[Optional[str]] = mapped_column(
        sa.Text, nullable=True, server_default=sa.FetchedValue()
    )
    sample_type_norm: Mapped[Optional[str]] = mapped_column(
        sa.Text, nullable=True, server_default=sa.FetchedValue()
    )
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

# Generated-key columns: on Postgres the migration turns these into
# GENERATED ALWAYS AS ... STORED, so the ORM must never emit them in
# INSERT/UPDATE. server_default=FetchedValue() (set on the mapped_column
# above) is SQLAlchemy's marker for DB-generated values: the ORM omits
# the columns from INSERT/UPDATE and fetches them back after INSERT.
# It emits no DDL, so create_all on SQLite still makes plain nullable
# columns (the documented legacy-path fallback).


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
    # Fan-out flag: the DMP two-tier strategy
    # (docs/dmp_reference_alignment.md, section b) emits one row per
    # (tumor, normal) match — an ambiguous tumor carries several latest
    # rows under the DMP logic. multi_pair=True rows are exempt from the
    # one-latest-per-tumor unique index below; every other logic keeps
    # single-winner semantics.
    multi_pair: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())

    @property
    def pairing_logic(self) -> str:
        return self.pairing_logic_obj.name if self.pairing_logic_obj is not None else ""

    __table_args__ = (
        sa.Index(
            "uq_pairing_table_tumor_pairing_logic_latest",
            "tumor_id",
            "pairing_logic_id",
            unique=True,
            # One latest row per tumor — except multi_pair (DMP fan-out)
            # rows, which are one per matched normal.
            postgresql_where=sa.text("latest AND COALESCE(multi_pair, FALSE) = FALSE"),
            sqlite_where=sa.text("latest AND COALESCE(multi_pair, 0) = 0"),
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
    # AL2 (AUDIT_LOG_TICKETS.md, decision 5): audit-log visibility.
    # ``role`` is 'user' or 'admin'; ``logic_groups`` is a list of
    # pairing-logic names. An admin sees every audit event; a user sees
    # events of the pairing logics in their groups plus their own
    # actions; a user with no groups sees only their own actions.
    role: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="user", server_default="user"
    )
    logic_groups: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Append-only audit log (AUDIT_LOG_TICKETS.md, ticket AL1).

    One row per sensitive write point, written ONLY by
    ``syzygy/audit.py::record`` inside the caller's transaction, so the
    event commits atomically with the mutation it documents.  Revision
    0006_audit_log REVOKEs UPDATE/DELETE and installs a guard trigger
    that rejects them at the database level — never UPDATE or DELETE
    from application code.

    ``payload`` carries ids and names ONLY — never patient, clinical or
    variant data (binding sanitization rule, DECIDED #5).  ``actor`` is
    an e-mail address for user actions and ``"system"`` for
    machine-driven paths; ``pairing_logic`` is the AL2 visibility-scope
    key (NULL = admin-only, unless it is the actor's own action).
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )
    actor: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    actor_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    pairing_logic: Mapped[Optional[str]] = mapped_column(sa.String(80), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reverse_of: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(sa.String(200), nullable=True)

    __table_args__ = (
        sa.Index("ix_audit_log_occurred_at", "occurred_at"),
        sa.Index("ix_audit_log_pairing_logic_occurred_at", "pairing_logic", "occurred_at"),
        sa.Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_actor_occurred_at", "actor", "occurred_at"),
    )


# AUDIT_LOG_TICKETS.md (ticket AL2): the read-only audit_view over
# audit_log, created here on every dialect create_all runs on (SQLite
# dev/test fallback and Postgres) — but the DDL is dialect-split,
# because the two engines' CREATE VIEW grammars are mutually
# exclusive: PostgreSQL supports only CREATE [OR REPLACE] VIEW (no IF
# NOT EXISTS), SQLite only CREATE VIEW IF NOT EXISTS (no OR REPLACE).
# syzygy.audit.audit_view_ddl() picks the right one for the connection's
# dialect.  The app's create_all fallback and the Alembic-managed
# schema (revision 0007_audit_view, which also runs GRANT SELECT ...
# TO PUBLIC) expose the same object — the schema snapshot tests compare
# the two, and views show up in information_schema.columns, so both
# sides must create it.  The PG trigger/function/REVOKE statements stay
# alembic-only (0006), exactly like the 0005 redaction GRANT.
@event.listens_for(AuditLog.__table__, "after_create")
def _create_audit_view(target, connection, **kw):
    # Lazy import: syzygy.audit imports syzygy.models at module level,
    # so a top-level import here would be a cycle.
    from syzygy.audit import audit_view_ddl

    connection.execute(DDL(audit_view_ddl(connection.dialect.name)))
