"""Tests for scripts/backfill_patient_dmp.py (t_9e6c5cd6).

The backfill fills ``patient__dmp`` on DMP rows that have none, from
the sample name's reference P- prefix — the same parse
``remap_dmp_record`` and the ``dmp_patient_key`` generated column use.
Stored non-empty values are never touched (idempotent), non-DMP rows
are never touched, and unparseable names stay NULL.
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backfill_patient_dmp import backfill_patient_dmp
from syzygy.database import Base
from syzygy.models import Sample


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
    db.add_all(
        [
            # DMP tumor, NULL patient__dmp, parseable name -> imputed.
            Sample(
                primary_id="P-7000011-T01-IM6",
                cmo_sample_name="P-7000011-T01-IM6",
                sample_type="DMP",
                gene_panel="IMPACT468",
                tumor_or_normal="TUMOR",
                patient__dmp=None,
            ),
            # DMP normal, EMPTY-STRING patient__dmp, parseable -> imputed.
            Sample(
                primary_id="P-7000011-N01-IM6",
                cmo_sample_name="P-7000011-N01-IM6",
                sample_type="DMP",
                gene_panel="IMPACT468",
                tumor_or_normal="NORMAL",
                patient__dmp="",
            ),
            # DMP tumor with a stored non-empty value -> never touched.
            Sample(
                primary_id="P-7000012-T01-IM6",
                cmo_sample_name="P-7000012-T01-IM6",
                sample_type="DMP",
                gene_panel="IMPACT468",
                tumor_or_normal="TUMOR",
                patient__dmp="P-7000012",
            ),
            # DMP tumor, NULL patient__dmp, unparseable name -> stays NULL.
            Sample(
                primary_id="ISFAKE-T01",
                cmo_sample_name="ISFAKE-T01",
                sample_type="DMP",
                gene_panel="IMPACT468",
                tumor_or_normal="TUMOR",
                patient__dmp=None,
            ),
            # Non-DMP row -> never touched.
            Sample(
                primary_id="LIMS-1",
                cmo_sample_name="LIMS-1",
                sample_type="Tumor",
                gene_panel="AMP123",
                tumor_or_normal="TUMOR",
                patient__dmp=None,
            ),
        ]
    )
    db.commit()


def _by_id(db) -> dict:
    return {s.primary_id: s.patient__dmp for s in db.scalars(select(Sample)).all()}


def test_backfill_imputes_null_and_empty_dmp_rows(db):
    _seed(db)
    updated = backfill_patient_dmp(db)
    assert updated == 2
    by_id = _by_id(db)
    assert by_id["P-7000011-T01-IM6"] == "P-7000011"
    assert by_id["P-7000011-N01-IM6"] == "P-7000011"
    # Stored non-empty value untouched; unparseable stays NULL.
    assert by_id["P-7000012-T01-IM6"] == "P-7000012"
    assert by_id["ISFAKE-T01"] is None
    # Non-DMP row untouched.
    assert by_id["LIMS-1"] is None


def test_backfill_idempotent_second_run_updates_zero(db):
    _seed(db)
    assert backfill_patient_dmp(db) == 2
    assert backfill_patient_dmp(db) == 0


def test_backfill_respects_batching(db):
    # More candidates than one batch: batched commits still update all.
    for i in range(7):
        db.add(
            Sample(
                primary_id=f"P-800000{i}-T01-IM6",
                cmo_sample_name=f"P-800000{i}-T01-IM6",
                sample_type="DMP",
                gene_panel="IMPACT468",
                tumor_or_normal="TUMOR",
                patient__dmp=None,
            )
        )
    db.commit()
    assert backfill_patient_dmp(db, batch_size=3) == 7
    for i in range(7):
        row = db.scalar(select(Sample).where(Sample.primary_id == f"P-800000{i}-T01-IM6"))
        assert row is not None
        assert row.patient__dmp == f"P-800000{i}"
