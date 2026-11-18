"""Audit-log query + reversal API (AUDIT_LOG_TICKETS.md, ticket AL2).

Mounted at ``/v1/audit`` with the standard Keycloak dependency (see
``syzygy/main.py``). Endpoints (spec "Query API" + decision 6/8):

GET /
    Query the log with server-side filters: ``from``, ``to`` (ISO
    datetime on ``occurred_at``), ``actor``, ``action``,
    ``entity_type``, ``entity_id``, ``pairing_logic``, ``limit``
    (<= 500) and ``offset``. Newest first. Every row is filtered
    through the decision-5 visibility rules and carries a computed
    ``reversible`` flag.
GET /entity/{entity_type}/{entity_id}
    The event timeline for one entity (same visibility, same row
    shape, ``limit``/``offset`` only).
GET /me
    The viewer's own visibility settings (role + logic_groups).
POST /{id}/reverse
    Reversal of one event: never modifies or deletes the original —
    the compensating action runs the normal code path and the new
    event carries ``reverse_of`` pointing at the reversed one.

Visibility (decision 5): an admin (``allowed_users.role == 'admin'``)
sees every row; a user sees rows whose effective pairing logic is in
their ``logic_groups`` plus every row they themselves wrote. A user
with no groups sees only their own actions. A row's "effective logic"
is its ``pairing_logic`` column when set, else the
``payload.pairing_logic`` fallback key — so a NULL column means
admin-only unless it is the viewer's own action. Non-HTTP callers
(CLI/system, no email) are unrestricted — the row-level rules only
ever narrow, and they are applied here in the API, not in storage.

The read path queries ``audit_log`` directly (with the visibility
predicate); the ``audit_view`` created by revision 0007 is a stable
read-only surface for ops tooling, not the API's data source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from syzygy.auth import get_current_user
from syzygy.database import get_db
from syzygy.models import AllowedUser, AuditLog, PairingLogic, PairingTable, Sample
from syzygy.pairing_service import trigger_pairing
from syzygy.routers.samples import unredact_sample
from syzygy.schemas import AuditLogRead, AuditMeRead, AuditQueryRead

router = APIRouter()

# Decision 7: the query API caps ``limit`` at 500.
MAX_AUDIT_LIMIT = 500
DEFAULT_AUDIT_LIMIT = 100

ROLES = ("user", "admin")
REVERSIBLE_ACTIONS = (
    "pairing.created",      # override only — restore the replaced pairing
    "sample.redacted",      # unredact
    "pair_assignment.imported",  # per-pair reversal — AL3 (see below)
)


@dataclass(frozen=True)
class _Viewer:
    """Resolved visibility for one request.

    ``email is None`` marks a non-HTTP caller (CLI/system context):
    unrestricted, because the decision-5 rules only ever narrow and
    there is no logged-in user to narrow for.
    """

    email: Optional[str]
    role: str
    groups: tuple


def _viewer_for(db: Session, current_user) -> _Viewer:
    if not isinstance(current_user, dict) or not current_user.get("email"):
        return _Viewer(None, "admin", ())
    email = current_user["email"]
    row = db.scalar(select(AllowedUser).where(AllowedUser.email == email))
    if row is None or not row.active:
        # Unknown/inactive viewer: least privilege — own actions only.
        return _Viewer(email, "user", ())
    role = row.role or "user"
    return _Viewer(email, role, tuple(row.logic_groups or ()))


def _effective_logic(row: AuditLog) -> Optional[str]:
    """The row's pairing logic: column first, payload key as fallback."""
    if row.pairing_logic:
        return row.pairing_logic
    payload = row.payload or {}
    logic = payload.get("pairing_logic")
    return logic if isinstance(logic, str) and logic else None


def audit_row_visible(row: AuditLog, viewer: _Viewer) -> bool:
    """Decision 5, row level (also used to gate POST /{id}/reverse)."""
    if viewer.email is None or viewer.role == "admin":
        return True
    if row.actor == viewer.email:
        return True
    logic = _effective_logic(row)
    return logic is not None and logic in viewer.groups


def _visibility_predicate(viewer: _Viewer):
    """SQL form of :func:`audit_row_visible` (None = no restriction)."""
    if viewer.email is None or viewer.role == "admin":
        return None
    conds = [AuditLog.actor == viewer.email]
    if viewer.groups:
        conds.append(
            or_(
                AuditLog.pairing_logic.in_(list(viewer.groups)),
                AuditLog.payload["pairing_logic"].astext.in_(list(viewer.groups)),
            )
        )
    return or_(*conds)


def _logic_is(logic_name: str):
    """Query filter for a pairing logic (column, or payload fallback)."""
    return or_(
        AuditLog.pairing_logic == logic_name,
        AuditLog.payload["pairing_logic"].astext == logic_name,
    )


def _is_reversible(row: AuditLog) -> bool:
    """Decision 6: which rows the Reverse button applies to.

    ``pairing.created`` counts only for overrides that actually
    replaced a previous pairing — restoring is only possible when the
    payload still carries the replaced row's id. ``pair_assignment.
    imported`` is listed in decision 6 but its events (and the import
    endpoint they come from) are ticket AL3, so the flag is False
    until then.
    """
    payload = row.payload or {}
    if row.action == "sample.redacted":
        return True
    if row.action == "pairing.created":
        return (
            payload.get("trigger_type") == "override"
            and bool(payload.get("replaced_pairing_id"))
        )
    return False


def _row_read(row: AuditLog) -> AuditLogRead:
    read = AuditLogRead.model_validate(row)
    read.reversible = _is_reversible(row)
    return read


def _query(
    db: Session,
    viewer: _Viewer,
    *,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    pairing_logic: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = DEFAULT_AUDIT_LIMIT,
    offset: int = 0,
) -> tuple[list[AuditLogRead], int]:
    stmt = select(AuditLog)
    pred = _visibility_predicate(viewer)
    if pred is not None:
        stmt = stmt.where(pred)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if pairing_logic:
        stmt = stmt.where(_logic_is(pairing_logic))
    if date_from:
        stmt = stmt.where(AuditLog.occurred_at >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.occurred_at <= date_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [_row_read(r) for r in rows], total


@router.get("", response_model=AuditQueryRead)
def query_audit(
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    actor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    pairing_logic: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Query the audit log (decision 7): filters, limit<=500, offset,
    newest first, visibility applied server-side."""
    viewer = _viewer_for(db, current_user)
    rows, total = _query(
        db,
        viewer,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        pairing_logic=pairing_logic,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return {"rows": rows, "total": total}


@router.get("/entity/{entity_type}/{entity_id}", response_model=AuditQueryRead)
def entity_timeline(
    entity_type: str,
    entity_id: str,
    limit: int = Query(default=DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """The event timeline for one entity (spec "Query API")."""
    viewer = _viewer_for(db, current_user)
    rows, total = _query(
        db,
        viewer,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    return {"rows": rows, "total": total}


@router.get("/me", response_model=AuditMeRead)
def audit_me(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """The viewer's own visibility settings (spec "GET /me/")."""
    viewer = _viewer_for(db, current_user)
    return {
        "email": viewer.email,
        "role": viewer.role,
        "logic_groups": list(viewer.groups),
    }


def _newest_event_for(db: Session, *, reverse_of: UUID) -> Optional[AuditLog]:
    """The (unique) event that carries ``reverse_of`` pointing at one
    reversed event — the reversal's own audit trail."""
    return db.scalar(
        select(AuditLog)
        .where(AuditLog.reverse_of == reverse_of)
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
    )


def _reverse_pairing_created(
    db: Session, event: AuditLog, payload: dict, current_user: dict
) -> AuditLogRead:
    """Restore the pairing an override replaced (decision 6): re-run
    the normal pairing path pinned at the old normal. The new
    ``pairing.created`` event carries ``reverse_of`` itself."""
    if payload.get("trigger_type") != "override":
        raise HTTPException(
            status_code=400,
            detail=(
                "Only override pairings are reversible (this pairing was "
                f"triggered as {payload.get('trigger_type')!r})"
            ),
        )
    replaced_id = payload.get("replaced_pairing_id")
    if not replaced_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "This override created the first pairing for its tumor — "
                "there is no previous pairing to restore"
            ),
        )
    override = db.get(PairingTable, UUID(event.entity_id))
    if override is None:
        raise HTTPException(
            status_code=404, detail="The override pairing row no longer exists"
        )
    replaced = db.get(PairingTable, UUID(replaced_id))
    if replaced is None:
        raise HTTPException(
            status_code=404,
            detail="The previously-replaced pairing row no longer exists",
        )
    # The override must still be the current (latest) pairing — if it
    # has since been replaced, restoring the old pairing would clobber
    # a newer one.
    current = db.scalars(
        select(PairingTable).where(
            PairingTable.tumor_id == override.tumor_id,
            PairingTable.pairing_logic_id == override.pairing_logic_id,
            PairingTable.latest.is_(True),
        )
    ).first()
    if current is None or current.id != override.id:
        raise HTTPException(
            status_code=409,
            detail=(
                "This pairing has since been replaced — reverse the "
                "current pairing instead"
            ),
        )
    tumor = db.get(Sample, override.tumor_id)
    if tumor is None:
        raise HTTPException(
            status_code=404, detail="The tumor sample no longer exists"
        )
    logic = db.get(PairingLogic, override.pairing_logic_id)
    if logic is None:
        raise HTTPException(
            status_code=404, detail="The pairing logic no longer exists"
        )
    pairing = trigger_pairing(
        db,
        tumor,
        logic,
        "override",
        normal_id=replaced.normal_id,
        actor=current_user,
        reverse_of=event.id,
    )
    if pairing is None:
        raise HTTPException(
            status_code=409, detail="Could not restore the previous pairing"
        )
    new_event = _newest_event_for(db, reverse_of=event.id)
    if new_event is None:
        raise HTTPException(
            status_code=500,
            detail="Reversal ran but its audit event was not recorded",
        )
    return _row_read(new_event)


def _reverse_sample_redacted(
    db: Session, event: AuditLog, current_user: dict
) -> AuditLogRead:
    """Un-redact (decision 6): run the normal unredact path; its
    ``sample.unredacted`` event carries ``reverse_of`` itself."""
    sample = db.get(Sample, UUID(event.entity_id))
    if sample is None:
        raise HTTPException(status_code=404, detail="The sample no longer exists")
    if not sample.redacted:
        raise HTTPException(
            status_code=409,
            detail="The sample is not currently redacted — nothing to reverse",
        )
    latest_redact = db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.entity_type == "sample",
            AuditLog.entity_id == event.entity_id,
            AuditLog.action == "sample.redacted",
        )
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
    )
    if latest_redact != event.id:
        raise HTTPException(
            status_code=409,
            detail=(
                "This is not the sample's current redaction event — "
                "reverse the most recent one"
            ),
        )
    unredact_sample(sample_id=UUID(event.entity_id), db=db, current_user=current_user)
    new_event = _newest_event_for(db, reverse_of=event.id)
    if new_event is None:
        raise HTTPException(
            status_code=500,
            detail="Reversal ran but its audit event was not recorded",
        )
    return _row_read(new_event)


@router.post("/{event_id}/reverse", response_model=AuditLogRead)
def reverse_event(
    event_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> AuditLogRead:
    """Reverse one audit event (decision 6/8).

    The original row is never modified or deleted — the audit log is
    append-only (AL1 trigger). The compensating action runs the normal
    code path, so it is audited like any other action, and the new
    event's ``reverse_of`` links back to the one being reversed.
    Permission: the actor of the event, or an admin.
    """
    event = db.get(AuditLog, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="No audit event with this id")
    viewer = _viewer_for(db, current_user)
    if not audit_row_visible(event, viewer):
        raise HTTPException(
            status_code=403,
            detail="You do not have visibility of this audit event",
        )
    if event.actor != viewer.email and viewer.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Reversing another user's event requires the admin role",
        )
    payload = event.payload or {}
    if event.action == "pairing.created":
        return _reverse_pairing_created(db, event, payload, current_user)
    if event.action == "sample.redacted":
        return _reverse_sample_redacted(db, event, current_user)
    if event.action == "pair_assignment.imported":
        # Listed as reversible in decision 6, but its events and the
        # import endpoint they come from are ticket AL3 — until then
        # there is no payload contract to reverse against.
        raise HTTPException(
            status_code=400,
            detail=(
                "Per-pair reversal of imported pair assignments ships "
                "with the import endpoint (ticket AL3)"
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=(
            f"Action {event.action!r} is not reversible (reversible "
            "actions: pairing.created overrides, sample.redacted, "
            "pair_assignment.imported)"
        ),
    )
