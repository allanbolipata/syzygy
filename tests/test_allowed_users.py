"""Tests for the AllowedUser model (Keycloak login allow-list)."""

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import AllowedUser


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_allowed_users_table_created(db):
    assert inspect(db.bind).has_table("allowed_users")


def test_insert_and_active_default(db):
    user = AllowedUser(email="alice@example.com", display_name="Alice")
    db.add(user)
    db.commit()
    assert user.id is not None
    assert user.active is True
    assert user.created_date is not None
    row = db.scalar(select(AllowedUser).where(AllowedUser.email == "alice@example.com"))
    assert row is not None
    assert row.active is True


def test_duplicate_email_rejected(db):
    db.add(AllowedUser(email="bob@example.com"))
    db.commit()
    db.add(AllowedUser(email="bob@example.com"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
