#!/usr/bin/env python3
"""Maintain the syzygy allowed-users allow-list (allowed_users table).

The allow-list holds the Keycloak users allowed to log in to syzygy
(see models.AllowedUser, maintained ahead of the Keycloak login
integration). All changes are soft: ``remove`` deactivates a row
(active=False) rather than deleting it, so ``enable`` can restore the
user and history is kept.

Commands
--------
add --email EMAIL [--display-name NAME]
    Upsert: insert the user, or on an existing row update display_name
    (when given) and set active=True.
remove --email EMAIL
    Set active=False (soft disable; the row is kept).
enable --email EMAIL
    Set active=True.
list
    Print the allow-list as a table: email, display_name, active.

remove/enable on an unknown email print an error to stderr and exit
non-zero. The database is selected by DATABASE_URL (env var or .env)
via the settings in database.py -- no connection string is hardcoded
here.

Usage
-----
    DATABASE_URL=... python scripts/manage_allowed_users.py add \
        --email alice@example.com --display-name "Alice"
    DATABASE_URL=... python scripts/manage_allowed_users.py list
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import select


def _get_user(db, email: str):
    from models import AllowedUser
    return db.scalar(select(AllowedUser).where(AllowedUser.email == email))


def cmd_add(db, email: str, display_name: str | None = None) -> str:
    """Upsert an allow-list entry.

    Insert a new row (active=True), or on an existing row update
    display_name (only when given) and set active=True. Returns a
    human-readable description of the action taken.
    """
    from models import AllowedUser
    user = _get_user(db, email)
    if user is None:
        user = AllowedUser(email=email, display_name=display_name)
        db.add(user)
        db.commit()
        detail = f" display_name={display_name!r}" if display_name is not None else ""
        return f"added {email}{detail} (active=True)"
    changed = []
    if display_name is not None and user.display_name != display_name:
        user.display_name = display_name
        changed.append(f"display_name={display_name!r}")
    if not user.active:
        user.active = True
        changed.append("active=True")
    db.commit()
    if not changed:
        return f"no change for {email} (already active, display_name={user.display_name!r})"
    return f"updated {email}: {', '.join(changed)}"


def cmd_remove(db, email: str) -> str | None:
    """Soft-disable an allow-list entry (active=False, row kept).

    Returns a description of the action taken, or None if the email is
    unknown.
    """
    user = _get_user(db, email)
    if user is None:
        return None
    if not user.active:
        return f"no change for {email} (already inactive)"
    user.active = False
    db.commit()
    return f"deactivated {email} (row kept)"


def cmd_enable(db, email: str) -> str | None:
    """Re-enable an allow-list entry (active=True).

    Returns a description of the action taken, or None if the email is
    unknown.
    """
    user = _get_user(db, email)
    if user is None:
        return None
    if user.active:
        return f"no change for {email} (already active)"
    user.active = True
    db.commit()
    return f"activated {email}"


def cmd_list(db) -> list:
    """Return all allow-list entries, ordered by email."""
    from models import AllowedUser
    return list(db.scalars(select(AllowedUser).order_by(AllowedUser.email)).all())


def format_table(users) -> str:
    """Render allow-list rows as an aligned email/display_name/active table."""
    rows = [(u.email, u.display_name or "", str(u.active)) for u in users]
    headers = ("email", "display_name", "active")
    widths = [
        max([len(h)] + [len(r[i]) for r in rows])
        for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Maintain the syzygy allowed-users allow-list (allowed_users table).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser(
        "add", help="upsert: insert, or update display_name and set active=True")
    p_add.add_argument("--email", required=True, help="user email address")
    p_add.add_argument("--display-name", default=None,
                       help="display name (set only when given)")

    p_remove = sub.add_parser(
        "remove", help="soft-disable: set active=False (row kept)")
    p_remove.add_argument("--email", required=True, help="user email address")

    p_enable = sub.add_parser("enable", help="set active=True")
    p_enable.add_argument("--email", required=True, help="user email address")

    sub.add_parser("list", help="print the allow-list: email, display_name, active")

    args = ap.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)

    from database import Base, SessionLocal, engine
    import models  # noqa: F401  (register AllowedUser on Base.metadata before create_all)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if args.command == "add":
            print(cmd_add(db, args.email, args.display_name))
            return 0
        if args.command in ("remove", "enable"):
            fn = cmd_remove if args.command == "remove" else cmd_enable
            msg = fn(db, args.email)
            if msg is None:
                print(f"error: no allowed user with email {args.email!r}",
                      file=sys.stderr)
                return 1
            print(msg)
            return 0
        if args.command == "list":
            users = cmd_list(db)
            print(format_table(users) if users else "No allowed users.")
            return 0
        return 1  # unreachable: argparse requires a known subcommand
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
