"""C2 — logic gating: endpoint dependencies (Layer A + C).

Per-endpoint 200/403 matrix for ``require_logic_access`` (the read gate)
and ``require_logic_admin`` (the write/admin gate), exercised through the
REAL FastAPI dependency graph: ``app.dependency_overrides`` of
``get_current_user`` swaps in a synthetic principal (with/without
``allowed_logics`` / ``is_logic_admin``) and the TestClient drives the
app — the gate reads the request's own ``pairing_logic_name`` param
exactly as in production.

Endpoints covered, enumerated from ``syzygy/routers/*.py`` (the spec
table is a starting point, not gospel):

  Layer A reads (``require_logic_access``):
    /v1/pairing/report/            (pairing_logic_name param)
    /v1/pairing/report-stats/      (pairing_logic_name param)
    /v1/pairing/export/            (pairing_logic_name param)
    /v1/pairing/evaluate/          (pairing_logic_name param)
    /v1/pairing/unmatched/         (pairing_logic_name param)
    /v1/pairing/unmatched-count/   (pairing_logic_name param)
    /v1/pairing/harmony/           (fixed HARMONY logic, no param)
    /v1/sample/{id}/possible-matches/  (optional pairing_logic_name)
  Layer C writes (``require_logic_admin``):
    /v1/pairing/trigger
    /v1/pairing/trigger-all
    /v1/pairing/trigger-dmp
    /v1/pairing/trigger-harmony
    /v1/pairing-logic/  (create — the only pairing-logic mutation)

The 403 ``detail`` strings are stable constants on ``syzygy.auth`` and
are asserted against those constants AND their literal values.

Both gates are no-ops while ``logic_gating_enabled`` is False (the
dark-launch default): the passthrough test asserts an empty-set,
non-admin principal still reads AND writes with the flag off.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import (
    LOGIC_ACCESS_DENIED_DETAIL,
    LOGIC_ADMIN_DENIED_DETAIL,
    get_current_user,
)
from syzygy.database import Base, get_db, settings
from syzygy.main import app
from syzygy.models import PairingLogic, PairingTable, Sample
from syzygy.routers import pairings
from syzygy.schemas import TumorOrNormal


def _principal(logics, admin):
    """A synthetic get_current_user result (C1 principal shape)."""
    return {
        "sub": "gating-user",
        "email": "gating@example.com",
        "preferred_username": "gating",
        "display_name": "Gating User",
        "allowed_logics": set(logics),
        "is_logic_admin": admin,
    }


# Principal set for the matrix:
ADMIN = _principal(["ARGOS", "DMP", "HARMONY"], True)
ARGOS_ONLY = _principal(["ARGOS"], False)
DMP_ONLY = _principal(["DMP"], False)
HARMONY_ONLY = _principal(["HARMONY"], False)
# Full visibility, NO admin: proves read != write (Layer C is not
# visibility-based).
FULL_VIS_NOADMIN = _principal(["ARGOS", "DMP", "HARMONY"], False)
# Fail-closed shape: gating on + no claim -> empty set, not admin.
NOBODY = _principal([], False)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # The three logic rows the matrix addresses. DMP and HARMONY point
    # at the dmp strategy (HARMONY reuses it per the router's
    # docstring); ARGOS at the default strategy (pairable in tests).
    session.add(PairingLogic(name="ARGOS", strategy_name="default", active=True))
    session.add(PairingLogic(name="DMP", strategy_name="dmp", active=True))
    session.add(PairingLogic(name="HARMONY", strategy_name="dmp", active=True))
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def gate(db, monkeypatch):
    """TestClient with get_db + get_current_user overridden, gating ON.

    ``gate.client`` — the TestClient (app lifespan not run).
    ``gate.as_(principal)`` — swap the principal for later requests.
    """
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    # Streaming endpoints (export, harmony csv) open their OWN session via
    # pairings._open_stream_session() (the request session may be closed
    # before the body finishes) — point it at the same in-memory engine,
    # same convention as test_pairings_report / test_harmony_endpoint.
    engine = db.get_bind()
    monkeypatch.setattr(
        pairings, "_open_stream_session", sessionmaker(bind=engine)
    )
    state = {"user": NOBODY}

    def override_get_db():
        yield db

    def override_user():
        return state["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    client = TestClient(app)
    yield {"client": client, "as_": lambda u: state.update(user=u)}
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _sample(db, primary_id, *, tumor_or_normal, bait_set="agilent"):
    sample = Sample(
        primary_id=primary_id,
        cmo_sample_name=f"{primary_id}_name",
        patient__cmo="P-GATE",
        request_id="R-GATE",
        sample_type="tissue",
        gene_panel="IMPACT468",
        bait_set=bait_set,
        tumor_or_normal=tumor_or_normal.value,
        created_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def test_403_detail_strings_are_stable():
    # Stable detail strings (asserted literally, not just via the
    # constants, so a rename in auth.py fails here).
    assert LOGIC_ACCESS_DENIED_DETAIL == "Logic not accessible"
    assert LOGIC_ADMIN_DENIED_DETAIL == "Logic admin required"


# ---------------------------------------------------------------------------
# Layer A — read gate matrix
# ---------------------------------------------------------------------------

# (id, method, path, denied params, allowed principal, allowed params)
READ_MATRIX = [
    (
        "report",
        "get",
        "/v1/pairing/report/",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
    (
        "report-stats",
        "get",
        "/v1/pairing/report-stats/",
        {"pairing_logic_name": "DMP"},
        DMP_ONLY,
        {"pairing_logic_name": "DMP"},
    ),
    (
        "export",
        "get",
        "/v1/pairing/export/",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
    (
        "evaluate",
        "get",
        "/v1/pairing/evaluate/",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
    (
        "unmatched",
        "get",
        "/v1/pairing/unmatched/",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
    (
        "unmatched-count",
        "get",
        "/v1/pairing/unmatched-count/",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
    # HARMONY is fixed (no pairing_logic_name param) — denied for any
    # principal without HARMONY.
    (
        "harmony",
        "get",
        "/v1/pairing/harmony/",
        {},
        HARMONY_ONLY,
        {},
    ),
    (
        "possible-matches",
        "get",
        "/v1/sample/{sample_id}/possible-matches",
        {"pairing_logic_name": "DMP"},
        ARGOS_ONLY,
        {"pairing_logic_name": "ARGOS"},
    ),
]


def _path_for(case, db):
    _case_id, _method, path_tmpl, *_rest = case
    if "{sample_id}" in path_tmpl:
        sample = _sample(db, "PM-1", tumor_or_normal=TumorOrNormal.TUMOR)
        return path_tmpl.format(sample_id=sample.id)
    return path_tmpl


@pytest.mark.parametrize("case", READ_MATRIX, ids=[c[0] for c in READ_MATRIX])
def test_read_denied_403(gate, db, case):
    """ARGOS_ONLY (no DMP/HARMONY) requesting a logic it may not see."""
    _case_id, method, path_tmpl, denied_params, *_rest = case
    gate["as_"](ARGOS_ONLY)
    response = gate["client"].request(
        method, _path_for(case, db), params=denied_params
    )
    assert response.status_code == 403
    assert response.json()["detail"] == LOGIC_ACCESS_DENIED_DETAIL


@pytest.mark.parametrize("case", READ_MATRIX, ids=[c[0] for c in READ_MATRIX])
def test_read_allowed_200(gate, db, case):
    """A principal that may see the requested logic reads it 200 —
    with NO admin bit (read access never implies write)."""
    _case_id, method, path_tmpl, _denied_params, allowed_user, allowed_params = case
    gate["as_"](allowed_user)
    response = gate["client"].request(
        method, _path_for(case, db), params=allowed_params
    )
    assert response.status_code == 200, response.text


def test_read_gate_absent_param_passes(gate, db):
    """Design: with no pairing_logic_name param there is no requested
    logic to check — the gate passes and the endpoint's own fallback
    resolution applies (here: no default logic configured -> 400, never
    403 from the gate)."""
    sample = _sample(db, "PM-2", tumor_or_normal=TumorOrNormal.TUMOR)
    gate["as_"](NOBODY)
    response = gate["client"].get(
        f"/v1/sample/{sample.id}/possible-matches"
    )
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# Layer C — write/admin gate matrix
# ---------------------------------------------------------------------------

# (id, method, path, payload factory(db))
WRITE_MATRIX = [
    ("trigger", "post", "/v1/pairing/trigger", None),
    ("trigger-all", "post", "/v1/pairing/trigger-all", None),
    ("trigger-dmp", "post", "/v1/pairing/trigger-dmp", None),
    ("trigger-harmony", "post", "/v1/pairing/trigger-harmony", None),
    (
        "pairing-logic-create",
        "post",
        "/v1/pairing-logic/",
        {"name": "GATE-TEST", "strategy_name": "default"},
    ),
]


def _write_payload(case, db):
    _id, _method, _path, payload = case
    if payload is None:
        # trigger: a fully valid payload (real tumor under ARGOS) so a
        # broken gate would actually create a pairing — the 403 must
        # come from the gate, not from body validation.
        if _id == "trigger":
            tumor = _sample(
                db, "TRIG-1", tumor_or_normal=TumorOrNormal.TUMOR
            )
            _sample(db, "TRIG-N1", tumor_or_normal=TumorOrNormal.NORMAL)
            return {
                "tumor_id": str(tumor.id),
                "trigger_type": "manual",
                "pairing_logic_name": "ARGOS",
            }
        return None
    return payload


def _no_pairing_created(db):
    assert db.query(PairingTable).count() == 0


def _no_logic_created(db, name):
    assert (
        db.query(PairingLogic).filter(PairingLogic.name == name).count() == 0
    )


@pytest.mark.parametrize("case", WRITE_MATRIX, ids=[c[0] for c in WRITE_MATRIX])
def test_write_denied_403(gate, db, case):
    """FULL_VIS_NOADMIN sees every logic but is NOT an admin: every
    trigger/config write is a 403 (read != write), and NOTHING is
    written."""
    _case_id, method, path, _payload = case
    gate["as_"](FULL_VIS_NOADMIN)
    response = gate["client"].request(
        method, path, json=_write_payload(case, db)
    )
    assert response.status_code == 403
    assert response.json()["detail"] == LOGIC_ADMIN_DENIED_DETAIL
    if case[0] == "trigger":
        _no_pairing_created(db)
    if case[0] == "pairing-logic-create":
        _no_logic_created(db, "GATE-TEST")


@pytest.mark.parametrize("case", WRITE_MATRIX, ids=[c[0] for c in WRITE_MATRIX])
def test_write_allowed(gate, db, case):
    """ADMIN passes the gate: trigger endpoints run (200, real effect
    where data exists) and the pairing-logic create lands (201)."""
    _case_id, method, path, _payload = case
    gate["as_"](ADMIN)
    response = gate["client"].request(
        method, path, json=_write_payload(case, db)
    )
    assert response.status_code in (200, 201), response.text
    if case[0] == "trigger":
        # The manual pairing under ARGOS was actually created.
        pairing = db.query(PairingTable).filter(
            PairingTable.latest.is_(True)
        ).first()
        assert pairing is not None
        assert response.json()["tumor_id"] == str(pairing.tumor_id)
    if case[0] == "pairing-logic-create":
        assert (
            db.query(PairingLogic).filter(
                PairingLogic.name == "GATE-TEST"
            ).count()
            == 1
        )
    # trigger-all (sync) with zero in-scope samples: 200; new_pairings
    # is the LIST of pairings created — empty for an empty db.
    if case[0] == "trigger-all":
        body = response.json()
        assert body["scanned"] == 0
        assert body["new_pairings"] == []
    # trigger-dmp / trigger-harmony are background jobs (post-dev-merge
    # contract): 200 returns a job receipt immediately; the gating
    # assertion is the 200 itself (admin passes Layer C).
    if case[0] in ("trigger-dmp", "trigger-harmony"):
        body = response.json()
        assert body["job_id"]
        assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# Disabled-flag passthrough (dark launch: flag off == exactly today)
# ---------------------------------------------------------------------------

def test_gating_disabled_passthrough(db, monkeypatch):
    """Flag OFF: both gates are no-ops — an empty-set, non-admin
    principal reads logic-scoped endpoints AND performs writes."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    state = {"user": NOBODY}

    def override_get_db():
        yield db

    def override_user():
        return state["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    try:
        client = TestClient(app)
        # Layer A read: DMP data with an EMPTY allowed set.
        response = client.get(
            "/v1/pairing/report/", params={"pairing_logic_name": "DMP"}
        )
        assert response.status_code == 200
        # Layer C write: a trigger run + a pairing-logic create.
        response = client.post("/v1/pairing/trigger-all")
        assert response.status_code == 200
        response = client.post(
            "/v1/pairing-logic/",
            json={"name": "GATE-OFF", "strategy_name": "default"},
        )
        assert response.status_code == 201
        assert (
            db.query(PairingLogic).filter(
                PairingLogic.name == "GATE-OFF"
            ).count()
            == 1
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
