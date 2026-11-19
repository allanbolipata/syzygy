"""Tests for the append-only audit log (AUDIT_LOG_TICKETS.md, ticket AL1).

Covers: the table + database-level append-only guard, the writer
(syzygy/audit.py) behavior, and the write hooks — every mutation path
emits exactly one audit event (ids/names only in payloads).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import syzygy.audit as audit
from syzygy import nats_ingest, pairing_service
from syzygy.bait_sets import (
    ACCESS_BAIT_SETS,
    TEMPO_BAIT_SETS,
    resolve_bait_set_logic,
    seed_bait_sets,
)
from syzygy.database import Base, settings
from syzygy.models import AuditLog, BaitSet, PairingLogic, PairingTable, PendingNatsPublish, Sample
from syzygy.pairing_service import trigger_pairing, trigger_pairing_for_unpaired_tumors
from syzygy.pairing_strategies import DEFAULT_POOLED_MACHINES, seed_pooled_machines
from syzygy.routers.bait_sets import upsert_bait_set
from syzygy.routers.machines import upsert_machine
from syzygy.routers.pairing_logic import create_pairing_logic
from syzygy.routers.publish_outbox import MarkPublishedIn, mark_published
from syzygy.routers.samples import create_sample, redact_sample, unredact_sample
from syzygy.schemas import (
    BaitSetUpsert,
    PairingLogicCreate,
    PooledMachineUpsert,
    SampleCreate,
    TumorOrNormal,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        audit.reset_table_cache()
        session.close()
        engine.dispose()


def _events(db, action):
    return [
        row
        for row in db.query(AuditLog).all()
        if row.action == action
    ]


# ---------------------------------------------------------------------------
# Table + append-only guard
# ---------------------------------------------------------------------------


def test_audit_log_table_created(db):
    assert inspect(db.get_bind()).has_table("audit_log")


def test_audit_view_created_by_create_all_on_sqlite(db):
    """AL2/F1: the after_create listener must build a queryable
    audit_view on the SQLite dialect (the create_all-only dev/test
    fallback path — the same path 0007_audit_view must stay in
    lockstep with on Postgres)."""
    assert "audit_view" in inspect(db.get_bind()).get_view_names()
    assert db.execute(text("SELECT count(*) FROM audit_view")).scalar() == 0


def test_audit_view_ddl_is_dialect_specific():
    """F1 (defect 1): PostgreSQL supports only CREATE [OR REPLACE]
    VIEW and SQLite only CREATE VIEW IF NOT EXISTS — the SELECT list
    must be shared, the CREATE clause dialect-split. (A single
    'CREATE VIEW IF NOT EXISTS' string is a syntax error on real
    PostgreSQL; 'OR REPLACE' is a syntax error on SQLite — proven by
    probe on sqlite 3.53.1.)"""
    pg = audit.AUDIT_VIEW_DDL
    sq = audit.AUDIT_VIEW_DDL_SQLITE
    assert pg.startswith("CREATE OR REPLACE VIEW audit_view AS ")
    assert "IF NOT EXISTS" not in pg
    assert sq.startswith("CREATE VIEW IF NOT EXISTS audit_view AS ")
    assert "OR REPLACE" not in sq
    # The canonical SELECT list is identical on both sides.
    assert pg.split("AS ", 1)[1] == sq.split("AS ", 1)[1]
    assert audit.audit_view_ddl("sqlite") == sq
    assert audit.audit_view_ddl("postgresql") == pg


def test_append_only_guard_rejects_update_and_delete(db):
    row = audit.record(
        db,
        audit.SYSTEM_ACTOR,
        "test.event",
        entity_type="sample",
        entity_id="TUMOR-001",
        payload={"sample_name": "Tumor Sample 1"},
    )
    db.commit()
    assert row is not None
    with db.get_bind().connect() as conn:
        audit.install_guard(conn)
        try:
            with pytest.raises(Exception) as upd:
                conn.execute(
                    text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
                    {"id": row.id.hex},
                )
                conn.commit()
            assert "append-only" in str(upd.value).lower()
            with pytest.raises(Exception) as dele:
                conn.execute(
                    text("DELETE FROM audit_log WHERE id = :id"), {"id": row.id.hex}
                )
                conn.commit()
            assert "append-only" in str(dele.value).lower()
        finally:
            audit.drop_guard(conn)
    # The row survives: nothing was updated or deleted.
    assert db.scalar(text("SELECT count(*) FROM audit_log")) == 1


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def test_record_writes_row_with_actor_kind(db):
    row = audit.record(
        db,
        "alice@example.com",
        "sample.created",
        entity_type="sample",
        entity_id="TUMOR-001",
        payload={"sample_name": "Tumor Sample 1", "primary_id": "TUMOR-001"},
    )
    assert row is not None
    assert row.actor == "alice@example.com"
    assert row.actor_kind == "user"
    assert row.action == "sample.created"
    assert row.entity_type == "sample"
    assert row.entity_id == "TUMOR-001"
    assert row.payload == {"sample_name": "Tumor Sample 1", "primary_id": "TUMOR-001"}
    assert row.occurred_at is not None
    assert row.reverse_of is None


def test_record_system_actor(db):
    row = audit.record(
        db,
        None,
        "pairing.bulk",
        entity_type="pairing_logic",
        entity_id="TEMPO",
        payload={"scanned": 0},
    )
    assert row is not None
    assert row.actor == "system"
    assert row.actor_kind == "system"


def test_record_carries_reverse_of(db):
    first = audit.record(
        db,
        audit.SYSTEM_ACTOR,
        "sample.redacted",
        entity_type="sample",
        entity_id="TUMOR-001",
    )
    assert first is not None
    second = audit.record(
        db,
        "alice@example.com",
        "sample.unredacted",
        entity_type="sample",
        entity_id="TUMOR-001",
        reverse_of=first.id,
    )
    assert second is not None
    assert second.reverse_of == first.id


def test_actor_from_user_variants():
    assert audit.actor_from_user({"email": "bob@example.com"}) == "bob@example.com"
    assert audit.actor_from_user({"sub": "svc-123"}) == "svc-123"
    assert audit.actor_from_user({}) == "system"
    assert audit.actor_from_user(None) == "system"
    assert audit.actor_from_user("not-a-dict") == "system"


def test_record_never_raises_when_table_missing(db, monkeypatch):
    monkeypatch.setitem(audit._engine_cache, id(db.get_bind()), False)
    row = audit.record(
        db,
        audit.SYSTEM_ACTOR,
        "sample.created",
        entity_type="sample",
        entity_id="TUMOR-001",
    )
    assert row is None  # skipped gracefully, no exception


def test_record_payload_coerced_to_json_safe(db):
    sentinel = object()
    row = audit.record(
        db,
        audit.SYSTEM_ACTOR,
        "test.event",
        entity_type="sample",
        entity_id="TUMOR-001",
        payload={"weird": sentinel},
    )
    assert row is not None
    assert isinstance(row.payload, dict)
    assert "weird" in row.payload  # default=str coercion, never a raise


# ---------------------------------------------------------------------------
# Hook: sample.created (POST /v1/samples)
# ---------------------------------------------------------------------------


def _sample_payload(primary_id, patient__cmo="PATIENT-001", **kwargs):
    return SampleCreate(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id} name",
        patient__cmo=patient__cmo,
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR,
        **kwargs,
    )


def test_create_sample_emits_sample_created_with_user_actor(db):
    sample = create_sample(_sample_payload("TUMOR-AUD-1"), db=db, current_user={"email": "alice@example.com"})
    events = _events(db, "sample.created")
    assert len(events) == 1
    assert events[0].actor == "alice@example.com"
    assert events[0].actor_kind == "user"
    assert events[0].entity_type == "sample"
    assert events[0].entity_id == str(sample.id)
    assert events[0].payload["sample_name"] == "TUMOR-AUD-1 name"
    assert events[0].payload["primary_id"] == "TUMOR-AUD-1"


def test_create_sample_emits_sample_created_system_actor(db):
    create_sample(_sample_payload("TUMOR-AUD-2"), db=db, current_user=None)
    events = _events(db, "sample.created")
    assert len(events) == 1
    assert events[0].actor == "system"
    assert events[0].actor_kind == "system"


# ---------------------------------------------------------------------------
# Hook: pairing.created / pairing.retired
# ---------------------------------------------------------------------------


def _seed_pairing_inputs(db, second_normal=True):
    # Explicit tz-aware sequencing_dates (the convention in
    # test_pairing_service.py): on SQLite, in-memory created_date values
    # can mix aware/naive within one session, so _latest must always be
    # decided by sequencing_date, never fall through to created_date.
    logic = PairingLogic(name="TEMPO")
    db.add(logic)
    tumor = Sample(
        primary_id="TUMOR-P-1",
        cmo_sample_name="Tumor P1",
        patient__cmo="PATIENT-P1",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.TUMOR.value,
        sequencing_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    normal = Sample(
        primary_id="NORMAL-P-1",
        cmo_sample_name="Normal P1",
        patient__cmo="PATIENT-P1",
        sample_type="tissue",
        gene_panel="PANEL-A",
        bait_set="BAIT-1",
        tumor_or_normal=TumorOrNormal.NORMAL.value,
        sequencing_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    rows = [tumor, normal]
    if second_normal:
        rows.append(
            Sample(
                primary_id="NORMAL-P-2",
                cmo_sample_name="Normal P2",
                patient__cmo="PATIENT-P1",
                sample_type="tissue",
                gene_panel="PANEL-A",
                bait_set="BAIT-1",
                tumor_or_normal=TumorOrNormal.NORMAL.value,
                sequencing_date=datetime(2024, 1, 3, tzinfo=timezone.utc),
            )
        )
    db.add_all(rows)
    db.commit()
    db.refresh(logic)
    return logic, tumor, normal


def test_trigger_pairing_emits_pairing_created(db):
    logic, tumor, _normal = _seed_pairing_inputs(db, second_normal=False)
    pairing = trigger_pairing(db, tumor, logic, "manual", actor="alice@example.com")
    assert pairing is not None
    events = _events(db, "pairing.created")
    assert len(events) == 1
    assert events[0].actor == "alice@example.com"
    assert events[0].entity_type == "pairing"
    assert events[0].entity_id == str(pairing.id)
    assert events[0].pairing_logic == "TEMPO"
    assert events[0].payload["trigger_type"] == "manual"
    assert events[0].payload["tumor_name"] == "Tumor P1"
    assert events[0].payload["normal_name"] == "Normal P1"


def test_trigger_pairing_replacement_emits_retired_then_created(db):
    logic, tumor, normal = _seed_pairing_inputs(db)
    first = trigger_pairing(
        db, tumor, logic, "manual", normal_id=normal.id, actor="alice@example.com"
    )
    assert first is not None
    other_normal = db.query(Sample).filter_by(primary_id="NORMAL-P-2").one()
    second = trigger_pairing(
        db, tumor, logic, "manual", normal_id=other_normal.id, actor="alice@example.com"
    )
    assert first is not None and second is not None and first.id != second.id
    retired = _events(db, "pairing.retired")
    assert len(retired) == 1
    assert retired[0].entity_type == "pairing"
    assert retired[0].entity_id == str(first.id)
    assert retired[0].actor == "alice@example.com"
    created = _events(db, "pairing.created")
    assert len(created) == 2
    assert created[-1].payload.get("replaced_pairing_id") == str(first.id)


def test_mismatch_key_labels_strips_values():
    labels = pairing_service._mismatch_key_labels(
        [
            "patient__cmo mismatch (CMO-1 vs CMO-2)",
            "gene_panel mismatch (PANEL-A vs PANEL-B)",
        ]
    )
    assert labels == ["patient__cmo", "gene_panel"]
    assert pairing_service._mismatch_key_labels(None) is None
    assert pairing_service._mismatch_key_labels([]) is None
    # Only the key label survives — never the values.
    assert "CMO-1" not in str(labels)


# ---------------------------------------------------------------------------
# Hook: pairing.bulk (backfill pass — one summary per pass, capped details)
# ---------------------------------------------------------------------------


def test_backfill_pass_emits_bulk_summary_with_capped_details(db, monkeypatch):
    logic, _tumor, _normal = _seed_pairing_inputs(db, second_normal=False)
    db.add(BaitSet(bait_set="BAIT-1", pairing_logic_id=logic.id))
    # Three more unpaired tumors, each with its own matching normal.
    for i in range(2, 5):
        db.add_all(
            [
                Sample(
                    primary_id=f"TUMOR-P-{i}",
                    cmo_sample_name=f"Tumor P{i}",
                    patient__cmo=f"PATIENT-P{i - 1}",
                    sample_type="tissue",
                    gene_panel="PANEL-A",
                    bait_set="BAIT-1",
                    tumor_or_normal=TumorOrNormal.TUMOR.value,
                    sequencing_date=datetime(2024, 2, i, tzinfo=timezone.utc),
                ),
                Sample(
                    primary_id=f"NORMAL-P-{i}",
                    cmo_sample_name=f"Normal P{i}",
                    patient__cmo=f"PATIENT-P{i - 1}",
                    sample_type="tissue",
                    gene_panel="PANEL-A",
                    bait_set="BAIT-1",
                    tumor_or_normal=TumorOrNormal.NORMAL.value,
                    sequencing_date=datetime(2024, 2, i + 1, tzinfo=timezone.utc),
                ),
            ]
        )
    db.commit()
    monkeypatch.setattr(audit, "BULK_AUDIT_DETAIL_CAP", 2)
    summary = trigger_pairing_for_unpaired_tumors(db)
    assert summary["paired"] == 4
    bulk = _events(db, "pairing.bulk")
    assert len(bulk) == 1
    assert bulk[0].actor == "system"
    assert bulk[0].actor_kind == "system"
    assert bulk[0].entity_type == "pairing_logic"
    assert bulk[0].payload["scanned"] == 4
    assert bulk[0].payload["detail_events_recorded"] == 2
    assert bulk[0].payload["detail_cap"] == 2
    details = _events(db, "pairing.created")
    assert len(details) == 2  # capped by BULK_AUDIT_DETAIL_CAP
    assert all(d.actor == "system" for d in details)


# ---------------------------------------------------------------------------
# Hook: logic.created (POST /v1/pairing-logic)
# ---------------------------------------------------------------------------


def test_create_pairing_logic_emits_logic_created(db):
    logic = create_pairing_logic(
        PairingLogicCreate(name="IMPACT", strategy_name="default"),
        db=db,
        current_user={"email": "alice@example.com"},
    )
    events = _events(db, "logic.created")
    assert len(events) == 1
    assert events[0].actor == "alice@example.com"
    assert events[0].entity_type == "pairing_logic"
    assert events[0].entity_id == "IMPACT"
    assert events[0].payload == {
        "name": "IMPACT",
        "strategy_name": "default",
        "active": True,
    }


# ---------------------------------------------------------------------------
# Hook: machine.created (POST /v1/machines)
# ---------------------------------------------------------------------------


def test_upsert_machine_emits_machine_created(db):
    row = upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="hiseq"),
        db=db,
        current_user={"email": "alice@example.com"},
    )
    events = _events(db, "machine.created")
    assert len(events) == 1
    assert events[0].actor == "alice@example.com"
    assert events[0].entity_type == "machine"
    assert events[0].entity_id == str(row.id)
    assert events[0].payload == {"machine_name": "bono", "machine_class": "hiseq"}
    # Upsert (update path) does not re-emit machine.created.
    upsert_machine(
        PooledMachineUpsert(machine_name="bono", machine_class="novaseq"),
        db=db,
        current_user=None,
    )
    assert len(_events(db, "machine.created")) == 1


# ---------------------------------------------------------------------------
# Hook: cohort.published (outbox row leaves the queue)
# ---------------------------------------------------------------------------


def test_mark_published_emits_cohort_published(db, monkeypatch):
    monkeypatch.setattr(settings, "nats_ingest_token", "sekret", raising=False)
    cohort_id = "COHORT-AUD-1"
    outbox = PendingNatsPublish(cohort_id=cohort_id, subject="reports", report_json="{}")
    db.add(outbox)
    db.commit()
    db.refresh(outbox)
    mark_published(
        MarkPublishedIn(id=outbox.id),
        db=db,
        x_nats_token="sekret",
    )
    events = _events(db, "cohort.published")
    assert len(events) == 1
    assert events[0].actor == "system"
    assert events[0].entity_type == "cohort"
    assert events[0].entity_id == cohort_id
    assert events[0].payload["subject"] == "reports"
    db.refresh(outbox)
    assert outbox.published_at is not None


# ---------------------------------------------------------------------------
# Hook: sample.redacted / sample.unredacted (POST /v1/samples/{id}/...)
# ---------------------------------------------------------------------------


def test_redact_and_unredact_emit_linked_events(db):
    sample = create_sample(
        _sample_payload("TUMOR-RED-1"), db=db, current_user={"email": "alice@example.com"}
    )
    redact_sample(
        sample_id=sample.id, payload=None, db=db, current_user={"email": "alice@example.com"}
    )
    redacted = _events(db, "sample.redacted")
    assert len(redacted) == 1
    assert redacted[0].actor == "alice@example.com"
    assert redacted[0].actor_kind == "user"
    assert redacted[0].entity_type == "sample"
    assert redacted[0].entity_id == str(sample.id)
    # The clinical reason stays on the sample row, never in the log.
    assert redacted[0].payload == {"sample_name": "TUMOR-RED-1 name"}
    unredact_sample(sample_id=sample.id, db=db, current_user={"email": "bob@example.com"})
    unredacted = _events(db, "sample.unredacted")
    assert len(unredacted) == 1
    assert unredacted[0].actor == "bob@example.com"
    assert unredacted[0].reverse_of == redacted[0].id


# ---------------------------------------------------------------------------
# Hook: sample.updated + nats.ingest (cmo-sample-update message)
# ---------------------------------------------------------------------------


def test_sample_update_emits_sample_updated_and_nats_ingest(db):
    sample = create_sample(_sample_payload("NATS-UPD-1"), db=db, current_user=None)
    result = nats_ingest.dispatch(
        db,
        settings.nats_sample_update_subject,
        {
            "latestSampleMetadata": {
                "primaryId": "NATS-UPD-1",
                "cmoSampleName": "NATS-UPD-1 renamed",
            }
        },
    )
    assert result["status"] == "ok"
    updated = _events(db, "sample.updated")
    assert len(updated) == 1
    assert updated[0].actor == "system"
    assert updated[0].actor_kind == "system"
    assert updated[0].entity_type == "sample"
    assert updated[0].entity_id == str(sample.id)
    assert updated[0].payload["sample_name"] == "NATS-UPD-1 renamed"
    assert updated[0].payload["primary_id"] == "NATS-UPD-1"
    # One nats.ingest event per ingested message — subject/ids only.
    ingests = _events(db, "nats.ingest")
    assert len(ingests) == 1
    assert ingests[0].actor == "system"
    assert ingests[0].entity_type == "ingest"
    assert ingests[0].payload["subject"] == settings.nats_sample_update_subject


# ---------------------------------------------------------------------------
# Hook: bait_set.assigned (API upsert, lazy registration, bootstrap seed)
# ---------------------------------------------------------------------------


def test_upsert_bait_set_emits_bait_set_assigned(db):
    create_pairing_logic(
        PairingLogicCreate(name="IMPACT", strategy_name="default"), db=db, current_user=None
    )
    upsert_bait_set(BaitSetUpsert(bait_set="BAIT-X", pairing_logic_name="IMPACT"), db=db)
    events = _events(db, "bait_set.assigned")
    assert len(events) == 1
    assert events[0].actor == "system"
    assert events[0].entity_type == "bait_set"
    assert events[0].entity_id == "BAIT-X"
    assert events[0].pairing_logic == "IMPACT"
    assert events[0].payload == {"bait_set": "BAIT-X", "pairing_logic": "IMPACT"}
    # Reassignment (same name, different logic) emits a second event.
    upsert_bait_set(BaitSetUpsert(bait_set="BAIT-X", pairing_logic_name="IMPACT"), db=db)
    assert len(_events(db, "bait_set.assigned")) == 2


def test_bait_set_lazy_registration_emits_bait_set_assigned_once(db):
    create_pairing_logic(
        PairingLogicCreate(name="ACCESS", strategy_name="default"), db=db, current_user=None
    )
    logic = resolve_bait_set_logic(db, "MyAccessBaits")
    assert logic is not None and logic.name == "ACCESS"
    events = _events(db, "bait_set.assigned")
    assert len(events) == 1
    assert events[0].actor == "system"
    assert events[0].entity_id == "MyAccessBaits"
    assert events[0].pairing_logic == "ACCESS"
    # Second sight: already registered -> no new event.
    resolve_bait_set_logic(db, "MyAccessBaits")
    assert len(_events(db, "bait_set.assigned")) == 1


def test_bootstrap_seed_emits_machine_and_bait_set_events(db):
    create_pairing_logic(
        PairingLogicCreate(name="TEMPO", strategy_name="tempo"), db=db, current_user=None
    )
    create_pairing_logic(
        PairingLogicCreate(name="ACCESS", strategy_name="default"), db=db, current_user=None
    )
    seed_pooled_machines(db)
    seed_bait_sets(db)
    machines = _events(db, "machine.created")
    assert len(machines) == len(DEFAULT_POOLED_MACHINES)
    assert all(m.actor == "system" for m in machines)
    assert all(m.entity_type == "machine" for m in machines)
    bait_events = _events(db, "bait_set.assigned")
    assert len(bait_events) == len(TEMPO_BAIT_SETS) + len(ACCESS_BAIT_SETS)
    assert all(b.actor == "system" for b in bait_events)
    # Idempotent: re-running the seed adds no rows and no events.
    seed_pooled_machines(db)
    seed_bait_sets(db)
    assert len(_events(db, "machine.created")) == len(DEFAULT_POOLED_MACHINES)
    assert len(_events(db, "bait_set.assigned")) == len(TEMPO_BAIT_SETS) + len(ACCESS_BAIT_SETS)


# ---------------------------------------------------------------------------
# Sanitization: no patient/clinical values in any payload
# ---------------------------------------------------------------------------


def test_no_sensitive_values_in_any_payload(db):
    logic, tumor, normal = _seed_pairing_inputs(db)
    db.add(BaitSet(bait_set="BAIT-1", pairing_logic_id=logic.id))
    db.commit()
    create_sample(
        _sample_payload("TUMOR-SENS-1", patient__cmo="PATIENT-SENS"),
        db=db,
        current_user={"email": "alice@example.com"},
    )
    trigger_pairing(db, tumor, logic, "manual", actor="alice@example.com")
    trigger_pairing_for_unpaired_tumors(db)
    for row in db.query(AuditLog).all():
        blob = str(row.payload)
        # Payloads may carry ids and names only — never the CMO value.
        assert "PATIENT-SENS" not in blob
        assert "CMO-1" not in blob
