"""Publish-outbox endpoint tests.

Covers the drain contract the listener-side flusher relies on: GET
/pending returns unpublished rows oldest-first (already-published rows
excluded), POST /mark-published stamps a row so it drops out of the
pending list, and the shared-secret X-NATS-Token check is fail-closed
exactly like the ingest endpoint. Router functions are called directly,
per the suite's pattern.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
from database import Base
from models import PendingNatsPublish
from routers.publish_outbox import MarkPublishedIn, list_pending, mark_published

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
SUBJECT = "MDB_STREAM.server.cpt-gateway.cohort-report"


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(pg_engine):
    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with pg_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


def _seed(db):
    """Two unpublished rows (created at distinct times) + one published."""
    rows = []
    for i, cohort in enumerate(("old", "new")):
        row = PendingNatsPublish(
            cohort_id=cohort,
            subject=SUBJECT,
            report_json='{"cohort_id": "%s"}' % cohort,
            created_at=T0 + timedelta(minutes=i),
        )
        db.add(row)
        rows.append(row)
    done = PendingNatsPublish(
        cohort_id="done",
        subject=SUBJECT,
        report_json='{"cohort_id": "done"}',
        created_at=T0 - timedelta(days=1),  # older, but already published
        published_at=T0,
    )
    db.add(done)
    db.commit()
    for row in rows:
        db.refresh(row)
    db.refresh(done)
    return rows, done


def test_pending_excludes_published_and_is_oldest_first(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    _, done = _seed(db)
    result = list_pending(db, 20, x_nats_token="secret")
    assert [r["cohort_id"] for r in result] == ["old", "new"]
    assert str(done.id) not in {r["id"] for r in result}
    assert result[0]["subject"] == SUBJECT
    assert result[0]["report_json"] == '{"cohort_id": "old"}'


def test_pending_respects_limit(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    _seed(db)
    result = list_pending(db, 1, x_nats_token="secret")
    assert [r["cohort_id"] for r in result] == ["old"]


def test_mark_published_flips_row(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    rows, _ = _seed(db)
    target = rows[0]
    result = mark_published(
        MarkPublishedIn(id=target.id), db, x_nats_token="secret"
    )
    assert result["id"] == str(target.id)
    assert result["published_at"]
    db.refresh(target)
    assert target.published_at is not None
    # The stamped row drops out of the pending list.
    remaining = list_pending(db, 20, x_nats_token="secret")
    assert [r["cohort_id"] for r in remaining] == ["new"]


def test_mark_published_unknown_id_is_404(monkeypatch, db):
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        mark_published(
            MarkPublishedIn(id=uuid.uuid4()), db, x_nats_token="secret"
        )
    assert exc.value.status_code == 404


def test_fail_closed_token(monkeypatch, db):
    """Wrong/missing token => 403; unconfigured token => 403 on both."""
    monkeypatch.setattr(database.settings, "nats_ingest_token", "secret")
    _seed(db)
    body = MarkPublishedIn(id=uuid.uuid4())
    for token in ("nope", None):
        with pytest.raises(HTTPException) as exc:
            list_pending(db, 20, x_nats_token=token)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            mark_published(body, db, x_nats_token=token)
        assert exc.value.status_code == 403
    # Fail-closed: no token configured => 403 even with a presented header.
    monkeypatch.setattr(database.settings, "nats_ingest_token", "")
    with pytest.raises(HTTPException) as exc:
        list_pending(db, 20, x_nats_token="anything")
    assert exc.value.status_code == 403
