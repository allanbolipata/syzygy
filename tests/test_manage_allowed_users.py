"""Tests for scripts/manage_allowed_users.py (allowed-users allow-list CLI).

Covers the add -> upsert (same email, new display_name) -> remove ->
enable -> list round trip by calling the script's functions directly
against an in-memory database, plus subprocess checks of the CLI wiring
(argparse + DATABASE_URL env var + exit codes) against a temporary
file-backed database.
"""

import os
import subprocess
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

from database import Base
from manage_allowed_users import (
    cmd_add,
    cmd_enable,
    cmd_list,
    cmd_remove,
    format_table,
)
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


def _get(db, email):
    return db.scalar(select(AllowedUser).where(AllowedUser.email == email))


def test_add_inserts_new_user(db):
    msg = cmd_add(db, "alice@example.com", display_name="Alice")
    assert msg.startswith("added alice@example.com")
    u = _get(db, "alice@example.com")
    assert u is not None
    assert u.active is True
    assert u.display_name == "Alice"
    assert u.created_date is not None


def test_add_without_display_name(db):
    cmd_add(db, "carol@example.com")
    u = _get(db, "carol@example.com")
    assert u.display_name is None
    assert u.active is True


def test_add_upsert_updates_display_name(db):
    cmd_add(db, "alice@example.com", display_name="Alice")
    cmd_add(db, "alice@example.com", display_name="Alice R.")
    rows = db.scalars(select(AllowedUser)).all()
    assert len(rows) == 1  # upsert, not a duplicate row
    assert rows[0].display_name == "Alice R."
    assert rows[0].active is True


def test_add_upsert_keeps_display_name_when_not_given(db):
    cmd_add(db, "alice@example.com", display_name="Alice")
    cmd_remove(db, "alice@example.com")
    cmd_add(db, "alice@example.com")  # no --display-name
    u = _get(db, "alice@example.com")
    assert u.active is True
    assert u.display_name == "Alice"  # not blanked


def test_remove_soft_disables(db):
    cmd_add(db, "bob@example.com")
    msg = cmd_remove(db, "bob@example.com")
    assert "deactivated" in msg
    u = _get(db, "bob@example.com")
    assert u is not None  # row kept
    assert u.active is False


def test_remove_unknown_email(db):
    assert cmd_remove(db, "nobody@example.com") is None


def test_enable_reactivates(db):
    cmd_add(db, "bob@example.com")
    cmd_remove(db, "bob@example.com")
    msg = cmd_enable(db, "bob@example.com")
    assert "activated" in msg
    assert _get(db, "bob@example.com").active is True


def test_enable_unknown_email(db):
    assert cmd_enable(db, "nobody@example.com") is None


def test_list_orders_by_email(db):
    cmd_add(db, "z@x.com", display_name="Zed")
    cmd_add(db, "a@x.com")
    cmd_remove(db, "a@x.com")
    users = cmd_list(db)
    assert [(u.email, u.display_name, u.active) for u in users] == [
        ("a@x.com", None, False),
        ("z@x.com", "Zed", True),
    ]
    table = format_table(users)
    assert table.splitlines()[0].split() == ["email", "display_name", "active"]
    assert "z@x.com" in table
    assert "False" in table


def test_add_upsert_remove_enable_list_round_trip(db):
    # add: insert
    msg = cmd_add(db, "dave@example.com", display_name="Dave")
    assert "added" in msg
    # upsert: same email, new display_name
    msg = cmd_add(db, "dave@example.com", display_name="David")
    assert "updated" in msg
    # remove: soft disable, row kept
    assert cmd_remove(db, "dave@example.com") is not None
    u = _get(db, "dave@example.com")
    assert u is not None
    assert u.active is False
    # enable
    assert cmd_enable(db, "dave@example.com") is not None
    # list
    users = cmd_list(db)
    assert [(u.email, u.display_name, u.active) for u in users] == [
        ("dave@example.com", "David", True)
    ]


# --- CLI wiring (argparse + DATABASE_URL env var + exit codes) ----------


def _run_cli(tmp_path, *argv):
    db_file = tmp_path / "cli_test.sqlite3"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_file}")
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "manage_allowed_users.py"), *argv],
        capture_output=True, text=True, cwd=REPO, env=env, timeout=60,
    )


def test_cli_round_trip(tmp_path):
    r = _run_cli(tmp_path, "add", "--email", "cli@example.com",
                 "--display-name", "CLI User")
    assert r.returncode == 0
    assert "added" in r.stdout

    r = _run_cli(tmp_path, "remove", "--email", "cli@example.com")
    assert r.returncode == 0
    assert "deactivated" in r.stdout

    r = _run_cli(tmp_path, "enable", "--email", "cli@example.com")
    assert r.returncode == 0
    assert "activated" in r.stdout

    r = _run_cli(tmp_path, "list")
    assert r.returncode == 0
    assert "cli@example.com" in r.stdout
    assert "CLI User" in r.stdout
    assert "True" in r.stdout


def test_cli_remove_unknown_email_nonzero(tmp_path):
    r = _run_cli(tmp_path, "remove", "--email", "ghost@example.com")
    assert r.returncode != 0
    assert "error" in r.stderr


def test_cli_enable_unknown_email_nonzero(tmp_path):
    r = _run_cli(tmp_path, "enable", "--email", "ghost@example.com")
    assert r.returncode != 0
    assert "error" in r.stderr
