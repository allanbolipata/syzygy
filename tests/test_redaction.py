"""Spec R1 (REDACTION_TICKETS.md): redaction — model/API/pairing exclusion.

Redaction is a boolean flag plus audit columns (``redacted`` /
``redacted_at`` / ``redacted_by`` / ``redaction_reason``). A redacted
sample is excluded from all automatic pairing (strategy candidate
queries + bulk sweeps), explicit pairing triggers on it return 400,
candidate listings still show it (flagged), and redaction never retires
existing pairings. Un-redaction restores pairing eligibility while
preserving the audit fields.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.database import Base
from syzygy.models import BaitSet, PairingLogic, PairingTable, Sample
from syzygy.pairing_service import (
    possible_matches,
    trigger_pairing,
    trigger_pairing_for_normal,
    trigger_pairing_for_unpaired_tumors,
)
from syzygy.routers.samples import list_samples, redact_sample, unredact_sample
from syzygy.schemas import RedactionRequest, TumorOrNormal

USER = {"email": "redactor@lab.test"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    logic = PairingLogic(name="ACCESS", strategy_name="default")
    session.add(logic)
    session.flush()
    session.add(BaitSet(bait_set="B1", pairing_logic_id=logic.id))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _logic(db):
    return db.scalar(select(PairingLogic).where(PairingLogic.name == "ACCESS"))


def _sample(db, role, cmo="CMO-1", panel="P1", bait="B1", request_id=None,
            sample_type="tissue", **kw):
    s = Sample(
        primary_id=f"PID-{uuid4().hex[:10]}",
        cmo_sample_name=f"CMO-{uuid4().hex[:10]}",
        patient__cmo=cmo,
        gene_panel=panel,
        bait_set=bait,
        request_id=request_id,
        tumor_or_normal=role,
        sample_type=sample_type,
        **kw,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _redact(db, sample):
    return redact_sample(sample_id=sample.id, payload=None, db=db, current_user=USER)


def _pairing_count(db):
    return int(db.scalar(select(func.count()).select_from(PairingTable)) or 0)


def _entry(e):
    """Normalize an ``other_matches`` entry (dict or pydantic model)."""
    if isinstance(e, dict):
        return e
    return {
        "sample": e.sample,
        "tier": e.tier,
        "matched": e.matched,
        "mismatched_keys": e.mismatched_keys,
        "would_be_selected": e.would_be_selected,
    }


# -- redaction API (flag + audit trail) ---------------------------------------


def test_redact_sets_audit_fields(db):
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    out = redact_sample(
        sample_id=tumor.id,
        payload=RedactionRequest(reason="consent withdrawn"),
        db=db,
        current_user=USER,
    )
    assert out.redacted is True
    assert out.redacted_at is not None
    assert out.redacted_by == "redactor@lab.test"
    assert out.redaction_reason == "consent withdrawn"
    db.refresh(tumor)
    assert tumor.redacted is True


def test_reredact_preserves_original_audit(db):
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    first = _redact(db, tumor)
    second = redact_sample(
        sample_id=tumor.id,
        payload=RedactionRequest(reason="late reason"),
        db=db,
        current_user={"email": "other@lab.test"},
    )
    assert second.redacted is True
    assert second.redaction_reason == first.redaction_reason
    assert second.redacted_by == first.redacted_by


def test_unredact_flips_flag_and_preserves_audit(db):
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    redact_sample(
        sample_id=tumor.id,
        payload=RedactionRequest(reason="r1"),
        db=db,
        current_user=USER,
    )
    out = unredact_sample(sample_id=tumor.id, db=db, current_user=USER)
    assert out.redacted is False
    assert out.redacted_at is not None
    assert out.redacted_by == "redactor@lab.test"
    assert out.redaction_reason == "r1"


def test_unredact_non_redacted_is_noop(db):
    tumor = _sample(db, TumorOrNormal.NORMAL.value)
    out = unredact_sample(sample_id=tumor.id, db=db, current_user=USER)
    assert out.redacted is False


def test_redact_unknown_sample_404(db):
    with pytest.raises(HTTPException) as ei:
        redact_sample(sample_id=uuid4(), payload=None, db=db, current_user=USER)
    assert ei.value.status_code == 404


def test_listing_shows_flag_and_supports_redacted_filter(db):
    a = _sample(db, TumorOrNormal.TUMOR.value)
    b = _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, a)
    rows = list_samples(limit=None, offset=None, db=db, response=Response())
    assert {s.id for s in rows} == {a.id, b.id}
    flags = {s.id: s.redacted for s in rows}
    assert flags[a.id] is True
    assert flags[b.id] is False
    assert (
        [s.id for s in list_samples(
            limit=None, offset=None, db=db, redacted=True, response=Response()
        )]
        == [a.id]
    )
    assert (
        [s.id for s in list_samples(
            limit=None, offset=None, db=db, redacted=False, response=Response()
        )]
        == [b.id]
    )


# -- pairing exclusion ---------------------------------------------------------


def test_explicit_trigger_on_redacted_tumor_400(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, tumor)
    with pytest.raises(HTTPException) as ei:
        trigger_pairing(db, tumor, logic, "explicit", commit=False)
    assert ei.value.status_code == 400
    assert "redacted" in str(ei.value.detail).lower()
    assert _pairing_count(db) == 0


def test_explicit_pin_of_redacted_normal_400(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    normal = _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, normal)
    with pytest.raises(HTTPException) as ei:
        trigger_pairing(
            db, tumor, logic, "explicit", normal_id=normal.id, commit=False
        )
    assert ei.value.status_code == 400
    assert "redacted" in str(ei.value.detail).lower()
    assert _pairing_count(db) == 0


def test_automatic_trigger_on_redacted_tumor_skips(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, tumor)
    assert trigger_pairing(db, tumor, logic, "automatic", commit=False) is None
    assert _pairing_count(db) == 0


def test_redacted_normal_excluded_from_cascade(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value, request_id="REQ-1")
    red_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    good_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    _redact(db, red_n)
    pairing = trigger_pairing(db, tumor, logic, "automatic")
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == good_n.id


def test_redacted_normal_never_paired_via_normal_entry(db):
    logic = _logic(db)
    _sample(db, TumorOrNormal.TUMOR.value)
    normal = _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, normal)
    assert trigger_pairing_for_normal(db, normal, logic) is None
    assert _pairing_count(db) == 0


def test_redacted_tumor_excluded_from_bulk_sweep(db):
    t1 = _sample(db, TumorOrNormal.TUMOR.value)
    t2 = _sample(db, TumorOrNormal.TUMOR.value)
    _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, t1)
    summary = trigger_pairing_for_unpaired_tumors(db)
    assert summary["scanned"] == 1
    assert summary["paired"] == 1
    assert summary["unpaired"] == 0
    rows = db.scalars(select(PairingTable)).all()
    assert len(rows) == 1
    assert rows[0].tumor_id == t2.id
    assert all(r.tumor_id != t1.id for r in rows)


def test_redaction_does_not_retire_existing_pairing(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    normal = _sample(db, TumorOrNormal.NORMAL.value)
    pairing = trigger_pairing(db, tumor, logic, "automatic")
    assert pairing is not None
    _redact(db, normal)
    latest = db.scalar(
        select(PairingTable).where(
            PairingTable.tumor_id == tumor.id,
            PairingTable.latest.is_(True),
        )
    )
    assert latest is not None
    assert latest.normal_id == normal.id


def test_unredact_restores_auto_pairing(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value)
    normal = _sample(db, TumorOrNormal.NORMAL.value)
    _redact(db, normal)
    assert trigger_pairing(db, tumor, logic, "automatic", commit=False) is None
    unredact_sample(sample_id=normal.id, db=db, current_user=USER)
    pairing = trigger_pairing(db, tumor, logic, "automatic")
    assert pairing is not None
    assert pairing.normal_id == normal.id


# -- candidate flagging ---------------------------------------------------------


def test_possible_matches_lists_and_flags_redacted_candidate(db):
    logic = _logic(db)
    tumor = _sample(db, TumorOrNormal.TUMOR.value, request_id="REQ-1")
    red_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    _redact(db, red_n)
    result = possible_matches(db, tumor, logic)
    assert result["other_matches_total"] == 1
    entries = [_entry(e) for e in result["other_matches"]]
    cand = next(e for e in entries if e["sample"].id == red_n.id)
    assert cand["sample"].redacted is True
    assert cand["matched"] is False
    assert cand["would_be_selected"] is False
    assert any(
        "redacted" in r.lower() for r in (cand["mismatched_keys"] or [])
    )
    # A redacted candidate can never match — it must not count as matched.
    assert result["other_matches_matched"] == 0


# -- per-strategy exclusion (spec R1 build item 6: default, argos, tempo, dmp) -


def _latest_rows(db, logic, tumor):
    """The tumor's latest pairing rows under ``logic`` (one per matched
    normal for the multi_pair DMP logic)."""
    return list(
        db.scalars(
            select(PairingTable).where(
                PairingTable.tumor_id == tumor.id,
                PairingTable.pairing_logic_id == logic.id,
                PairingTable.latest == True,  # noqa: E712
            )
        ).all()
    )


def _make_logic(db, name, strategy_name):
    logic = PairingLogic(name=name, strategy_name=strategy_name)
    db.add(logic)
    db.commit()
    db.refresh(logic)
    return logic


def _dmp_sample(db, name, role, patient="P-100", pool="POOLA", **kw):
    """A DMP-group sample. ``name`` is the reference sample name
    (``P-<id>-<type><seq>-<IM_ver>``); ``pool`` is the join pool
    (``additional_metadata.project_name``)."""
    s = Sample(
        primary_id=name,
        cmo_sample_name=name,
        patient__dmp=patient,
        patient__cmo=patient,
        sample_type="DMP",
        gene_panel="IMPACT468",
        tumor_or_normal=role,
        additional_metadata={"project_name": pool},
        **kw,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_argos_redacted_normal_not_auto_selected(db):
    argos = _make_logic(db, "ARGOS", "argos")
    tumor = _sample(db, TumorOrNormal.TUMOR.value, request_id="REQ-1")
    red_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    good_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    _redact(db, red_n)
    pairing = trigger_pairing(db, tumor, argos, "automatic")
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == good_n.id


def test_tempo_redacted_normal_not_auto_selected(db):
    tempo = _make_logic(db, "TEMPO", "tempo")
    tumor = _sample(db, TumorOrNormal.TUMOR.value, request_id="REQ-1")
    red_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    good_n = _sample(db, TumorOrNormal.NORMAL.value, request_id="REQ-1")
    _redact(db, red_n)
    pairing = trigger_pairing(db, tumor, tempo, "automatic")
    assert pairing is not None
    assert pairing.tumor_id == tumor.id
    assert pairing.normal_id == good_n.id


def test_dmp_redacted_normal_not_in_row_set(db):
    dmp = _make_logic(db, "DMP", "dmp")
    tumor = _dmp_sample(db, "P-100-T01-IM6", TumorOrNormal.TUMOR.value)
    red_n = _dmp_sample(db, "P-100-N01-IM6", TumorOrNormal.NORMAL.value)
    _redact(db, red_n)
    # Only a redacted normal in the pool -> the row set stays empty.
    assert trigger_pairing(db, tumor, dmp, "automatic") is None
    assert _pairing_count(db) == 0
    # A good normal in the pool enters the row set; the redacted one does not.
    good_n = _dmp_sample(db, "P-100-N02-IM7", TumorOrNormal.NORMAL.value)
    pairing = trigger_pairing(db, tumor, dmp, "automatic")
    assert pairing is not None
    assert {r.normal_id for r in _latest_rows(db, dmp, tumor)} == {good_n.id}


def test_dmp_possible_matches_flags_redacted_candidate(db):
    dmp = _make_logic(db, "DMP", "dmp")
    tumor = _dmp_sample(db, "P-100-T01-IM6", TumorOrNormal.TUMOR.value)
    red_n = _dmp_sample(db, "P-100-N01-IM6", TumorOrNormal.NORMAL.value)
    _redact(db, red_n)
    result = possible_matches(db, tumor, dmp)
    entries = [_entry(e) for e in result["other_matches"]]
    cand = next(e for e in entries if e["sample"].id == red_n.id)
    assert cand["sample"].redacted is True
    assert cand["matched"] is False
    assert cand["would_be_selected"] is False
    assert any(
        "redacted" in r.lower() for r in (cand["mismatched_keys"] or [])
    )
    # A redacted candidate can never match — it must not count as matched.
    assert result["other_matches_matched"] == 0
