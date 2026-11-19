"""C3 — logic gating: Layer B listing data filter.

While ``logic_gating_enabled`` is set, the four listing endpoints
(samples / patients / requests / pairings) intersect their query with
the principal's ``allowed_logics`` — an OR across the allowed logics
built on the EXISTING ``bait_sets.logic_membership_condition`` (the DMP
null-bait rule comes free from the helper). The principal arrives via
``Depends(get_current_user)``; the flag is the C1 ``Settings`` switch,
monkeypatched on the ``settings`` singleton (same pattern as the C2
endpoint-gate tests).

Semantics under test (per LOGIC_GATING_TICKETS.md, ticket C3):
- Samples: row-level — a sample is listed iff its bait-set family (or
  the DMP null-bait rule) is among the principal's allowed logics.
  Samples of NO registered family (orphan bait set / bait-less
  non-DMP) are invisible to every gated principal (fail-closed),
  visible only with the flag off.
- Patients / requests: group-level, same HAVING shape as the
  ``pairing_logic`` filter — a group is listed when >=1 of its samples
  belongs to an allowed logic; the group's counts still cover ALL of
  its samples (2026-11-11 counting decision, reaffirmed 2026-11-12).
- Pairings: OR across the two JOIN SIDES — a pairing is listed when the
  tumor's OR the normal's sample belongs to an allowed logic. This
  includes a pairing created under an UNAUTHORIZED logic whose samples
  belong to an authorized family (chosen semantics, documented in
  list_pairings' docstring).
- X-Total-Count reflects the GATED set (pre-pagination convention).
- Fail-closed (design point 3): gating on + no/empty entitlements ->
  no rows; unknown logic names in the claim resolve to nothing.
- Dark-launch passthrough: flag off -> listings are exactly today's,
  even for a principal with an empty allowed set.

Direct-call convention (repo standard): endpoint functions are called
with an in-memory SQLite session and limit/offset passed explicitly;
``current_user`` is passed as a plain kwarg (its default is the
``Depends`` object, which ``gating_allowed_logics`` treats as
fail-closed — but with the flag off (the default) every direct call is
an unfiltered passthrough, so the rest of the suite is unaffected).
HTTP-level TestClient tests confirm the principal reaches the query
through the real ``Depends(get_current_user)`` graph.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import get_current_user
from syzygy.database import Base, get_db, settings
from syzygy.main import app
from syzygy.models import BaitSet, PairingLogic, PairingTable, Sample
from syzygy.routers.pairings import list_pairings
from syzygy.routers.patients import list_patients
from syzygy.routers.requests import list_requests
from syzygy.routers.samples import list_samples
from syzygy.schemas import TumorOrNormal

TOTAL_HEADER = "X-Total-Count"
BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _principal(logics):
    """A synthetic get_current_user result (C1 principal shape)."""
    return {
        "sub": "gating-user",
        "email": "gating@example.com",
        "preferred_username": "gating",
        "display_name": "Gating User",
        "allowed_logics": set(logics),
        "is_logic_admin": False,
    }


ARGOS_ONLY = _principal(["ARGOS"])
DMP_ONLY = _principal(["DMP"])
FULL = _principal(["ARGOS", "TEMPO", "DMP"])
NOBODY = _principal([])


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def world(db):
    """Seed the three-logic world and return the handles.

    Logic families:
      ARGOS  <- bait set "IMPACT468"
      TEMPO  <- bait set "ONCOGRAM"
      DMP    <- bait set "DMP-BAIT"  (+ bait-NULL sample_type 'DMP')

    Samples:
      S-ARG1  IMPACT468          (ARGOS)   patient P-1 / request R-1
      S-TEM1  ONCOGRAM           (TEMPO)   patient P-2 / request R-2
      S-DMP1  bait NULL, DMP     (DMP)     patient P-3 / request R-3
      S-DMP2  DMP-BAIT           (DMP)     patient P-3 / request R-3
      S-ORPH  ORPHAN-BAIT (no assignment)  patient P-4 / request R-4
      S-NOB   bait NULL, tissue  (none)    patient P-5 / request R-5
      N-ARG   IMPACT468          (ARGOS)   patient P-6 / request R-6
      N-DMP   bait NULL, DMP     (DMP)     patient P-7 / request R-7

    Pairings:
      PA: tumor S-ARG1 / normal N-ARG   under ARGOS
      PB: tumor S-DMP1 / normal N-DMP   under DMP
      PC: tumor S-DMP1 / normal N-ARG   under ARGOS  (cross: created
          under a logic the DMP principal is NOT allowed — but the
          tumor side's sample belongs to the DMP family)
    """
    argos = PairingLogic(name="ARGOS", strategy_name="default", active=True)
    tempo = PairingLogic(name="TEMPO", strategy_name="default", active=True)
    dmp = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    db.add_all([argos, tempo, dmp])
    db.commit()
    db.add_all(
        [
            BaitSet(bait_set="IMPACT468", pairing_logic_id=argos.id),
            BaitSet(bait_set="ONCOGRAM", pairing_logic_id=tempo.id),
            BaitSet(bait_set="DMP-BAIT", pairing_logic_id=dmp.id),
        ]
    )
    db.commit()

    specs = {
        "S-ARG1": ("IMPACT468", "P-1", "R-1", "tissue", TumorOrNormal.TUMOR),
        "S-TEM1": ("ONCOGRAM", "P-2", "R-2", "tissue", TumorOrNormal.TUMOR),
        "S-DMP1": (None, "P-3", "R-3", "DMP", TumorOrNormal.TUMOR),
        "S-DMP2": ("DMP-BAIT", "P-3", "R-3", "tissue", TumorOrNormal.NORMAL),
        "S-ORPH": ("ORPHAN-BAIT", "P-4", "R-4", "tissue", TumorOrNormal.TUMOR),
        "S-NOB": (None, "P-5", "R-5", "tissue", TumorOrNormal.TUMOR),
        "N-ARG": ("IMPACT468", "P-6", "R-6", "tissue", TumorOrNormal.NORMAL),
        "N-DMP": (None, "P-7", "R-7", "DMP", TumorOrNormal.NORMAL),
    }
    samples = {}
    for i, (pid, (bait, patient, request, stype, ton)) in enumerate(specs.items()):
        sample = Sample(
            primary_id=pid,
            cmo_sample_name=f"{pid}_name",
            patient__cmo=patient,
            request_id=request,
            sample_type=stype,
            gene_panel="IMPACT468",
            bait_set=bait,
            tumor_or_normal=ton.value,
            created_date=BASE_TIME + timedelta(minutes=i),
        )
        db.add(sample)
        samples[pid] = sample
    db.commit()

    pairings = {}
    for tag, (tumor, normal, logic) in {
        "PA": ("S-ARG1", "N-ARG", argos),
        "PB": ("S-DMP1", "N-DMP", dmp),
        "PC": ("S-DMP1", "N-ARG", argos),
    }.items():
        row = PairingTable(
            pairing_logic_id=logic.id,
            tumor_id=samples[tumor].id,
            normal_id=samples[normal].id,
            trigger_type="manual",
            created_date=BASE_TIME + timedelta(hours=1),
        )
        db.add(row)
        pairings[tag] = row
    db.commit()
    return {
        "logics": {"ARGOS": argos, "TEMPO": tempo, "DMP": dmp},
        "samples": samples,
        "pairings": pairings,
    }


def _ids(rows):
    return {r.primary_id for r in rows}


# ---------------------------------------------------------------------------
# Samples (row-level filter)
# ---------------------------------------------------------------------------


def test_samples_dmp_only_invisible_to_argos_only(world, db, monkeypatch):
    """Accept: a DMP-only sample is invisible to an ARGOS-only user."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    rows = list_samples(limit=None, offset=None, db=db, current_user=ARGOS_ONLY)
    ids = _ids(rows)
    assert "S-ARG1" in ids
    assert "N-ARG" in ids
    # The DMP family — bait-NULL DMP-type and DMP-baited — is hidden...
    assert "S-DMP1" not in ids
    assert "S-DMP2" not in ids
    assert "N-DMP" not in ids
    # ...as are other logics and samples of no family.
    assert "S-TEM1" not in ids
    assert "S-ORPH" not in ids
    assert "S-NOB" not in ids
    assert ids == {"S-ARG1", "N-ARG"}


def test_samples_dmp_only_visible_to_dmp_user(world, db, monkeypatch):
    """Accept: the same DMP-only sample is visible to a DMP user."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    rows = list_samples(limit=None, offset=None, db=db, current_user=DMP_ONLY)
    ids = _ids(rows)
    # The DMP null-bait rule (bait NULL + sample_type 'DMP') is covered
    # by the membership helper: S-DMP1 and N-DMP are bait-less DMP
    # samples; S-DMP2 carries a registered DMP bait set.
    assert ids == {"S-DMP1", "S-DMP2", "N-DMP"}


def test_samples_total_reflects_gated_set_pre_pagination(world, db, monkeypatch):
    """Accept: X-Total-Count reflects the GATED set, pre-pagination."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    response = Response()
    rows = list_samples(
        limit=1,
        offset=0,
        db=db,
        current_user=DMP_ONLY,
        response=response,
    )
    assert len(rows) == 1
    # The header counts the whole gated set (3 DMP samples), not the page.
    assert response.headers[TOTAL_HEADER] == "3"


def test_samples_disabled_flag_passthrough(world, db, monkeypatch):
    """Accept: flag off -> listings are exactly today's, even for a
    principal with an EMPTY allowed set (the dark-launch guarantee)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    response = Response()
    rows = list_samples(
        limit=None,
        offset=None,
        db=db,
        current_user=NOBODY,
        response=response,
    )
    assert _ids(rows) == {
        "S-ARG1",
        "S-TEM1",
        "S-DMP1",
        "S-DMP2",
        "S-ORPH",
        "S-NOB",
        "N-ARG",
        "N-DMP",
    }
    assert response.headers[TOTAL_HEADER] == "8"


def test_samples_fail_closed_empty_entitlements(world, db, monkeypatch):
    """Design point 3: gating on + no entitlements -> no rows, total 0.
    An empty allowed set (or a claim that names no logic) must NOT be
    treated as 'see everything'."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    response = Response()
    rows = list_samples(
        limit=None,
        offset=None,
        db=db,
        current_user=NOBODY,
        response=response,
    )
    assert rows == []
    assert response.headers[TOTAL_HEADER] == "0"


def test_samples_orphan_bait_invisible_under_any_gate(world, db, monkeypatch):
    """Samples with an UNREGISTERED bait set (or bait-less non-DMP)
    belong to no family: invisible to every gated principal, even one
    holding all three logics (the known wrinkle, flagged in
    logic_membership_condition)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=FULL)
    )
    assert "S-ORPH" not in ids
    assert "S-NOB" not in ids
    assert ids == {"S-ARG1", "S-TEM1", "S-DMP1", "S-DMP2", "N-ARG", "N-DMP"}


def test_samples_unknown_claim_names_fail_closed(world, db, monkeypatch):
    """A claim naming a logic that does not exist resolves to an empty
    condition (fail-closed), not a passthrough."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    rows = list_samples(
        limit=None,
        offset=None,
        db=db,
        current_user=_principal(["GHOST-LOGIC"]),
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Patients (group-level HAVING filter)
# ---------------------------------------------------------------------------


def test_patients_group_membership_gated(world, db, monkeypatch):
    """A patient is listed when >=1 of its samples belongs to an
    allowed logic; group counts stay ALL-samples."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)

    argos_rows = list_patients(
        limit=None, offset=None, q=None, db=db, current_user=ARGOS_ONLY
    )
    # P-1 (S-ARG1) and P-6 (the ARGOS normal N-ARG) — both have an
    # ARGOS-family sample.
    assert [p.patient__cmo for p in argos_rows] == ["P-1", "P-6"]
    # P-1's counts cover ALL of its samples.
    assert argos_rows[0].sample_count == 1

    dmp_rows = list_patients(
        limit=None, offset=None, q=None, db=db, current_user=DMP_ONLY
    )
    # P-3 holds two DMP samples (the bait-NULL one AND the baited one)
    # — both count even though the gate is family-level; P-7 holds the
    # bait-NULL DMP normal.
    assert [p.patient__cmo for p in dmp_rows] == ["P-3", "P-7"]
    assert dmp_rows[0].sample_count == 2

    nobody_rows = list_patients(
        limit=None, offset=None, q=None, db=db, current_user=NOBODY
    )
    assert nobody_rows == []


def test_patients_total_reflects_gated_set(world, db, monkeypatch):
    """X-Total-Count counts gated groups, pre-pagination."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    response = Response()
    rows = list_patients(
        limit=1,
        offset=0,
        q=None,
        db=db,
        current_user=DMP_ONLY,
        response=response,
    )
    assert [p.patient__cmo for p in rows] == ["P-3"]
    # Two gated DMP groups (P-3 and P-7), one per page.
    assert response.headers[TOTAL_HEADER] == "2"


def test_patients_disabled_flag_passthrough(world, db, monkeypatch):
    """Flag off: all 7 patients, even for an empty-set principal."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    response = Response()
    rows = list_patients(
        limit=None,
        offset=None,
        q=None,
        db=db,
        current_user=NOBODY,
        response=response,
    )
    assert [p.patient__cmo for p in rows] == [
        "P-1",
        "P-2",
        "P-3",
        "P-4",
        "P-5",
        "P-6",
        "P-7",
    ]
    assert response.headers[TOTAL_HEADER] == "7"


# ---------------------------------------------------------------------------
# Requests (group-level HAVING filter)
# ---------------------------------------------------------------------------


def test_requests_group_membership_gated(world, db, monkeypatch):
    """A request is listed when >=1 of its samples belongs to an
    allowed logic; group counts stay ALL-samples."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)

    argos_rows = list_requests(
        limit=None, offset=None, q=None, db=db, current_user=ARGOS_ONLY
    )
    # R-1 (S-ARG1) and R-6 (the ARGOS normal N-ARG).
    assert [r.request_id for r in argos_rows] == ["R-1", "R-6"]
    assert argos_rows[0].sample_count == 1

    dmp_rows = list_requests(
        limit=None, offset=None, q=None, db=db, current_user=DMP_ONLY
    )
    # R-3 holds the two DMP samples (P-3's) — both counted; R-7 holds
    # the bait-NULL DMP normal.
    assert [r.request_id for r in dmp_rows] == ["R-3", "R-7"]
    assert dmp_rows[0].sample_count == 2

    nobody_rows = list_requests(
        limit=None, offset=None, q=None, db=db, current_user=NOBODY
    )
    assert nobody_rows == []


def test_requests_total_reflects_gated_set(world, db, monkeypatch):
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    response = Response()
    rows = list_requests(
        limit=1,
        offset=0,
        q=None,
        db=db,
        current_user=ARGOS_ONLY,
        response=response,
    )
    assert [r.request_id for r in rows] == ["R-1"]
    # Two gated ARGOS groups (R-1 and R-6), one per page.
    assert response.headers[TOTAL_HEADER] == "2"


def test_requests_disabled_flag_passthrough(world, db, monkeypatch):
    """Flag off: all 7 requests, even for an empty-set principal."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    rows = list_requests(
        limit=None, offset=None, q=None, db=db, current_user=NOBODY
    )
    assert [r.request_id for r in rows] == [
        "R-1",
        "R-2",
        "R-3",
        "R-4",
        "R-5",
        "R-6",
        "R-7",
    ]


# ---------------------------------------------------------------------------
# Pairings (OR across the two join sides)
# ---------------------------------------------------------------------------


def test_pairings_visible_when_either_side_allowed(world, db, monkeypatch):
    """A pairing is listed when the TUMOR's or the NORMAL's sample
    belongs to an allowed logic — including PC, created under ARGOS
    (unauthorized for the DMP principal) whose TUMOR side is DMP."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    by_id = {p.id: tag for tag, p in world["pairings"].items()}

    argos_rows = list_pairings(
        latest=False, limit=None, offset=None, db=db, current_user=ARGOS_ONLY
    )
    # PA (both sides ARGOS) and PC (normal side ARGOS); PB is DMP-only.
    assert {by_id[p.id] for p in argos_rows} == {"PA", "PC"}

    dmp_rows = list_pairings(
        latest=False, limit=None, offset=None, db=db, current_user=DMP_ONLY
    )
    # PB (both sides DMP) and PC (tumor side DMP — visible even though
    # the pairing row itself was created under the unauthorized ARGOS
    # logic; chosen semantics, per list_pairings' docstring).
    assert {by_id[p.id] for p in dmp_rows} == {"PB", "PC"}

    nobody_rows = list_pairings(
        latest=False, limit=None, offset=None, db=db, current_user=NOBODY
    )
    assert nobody_rows == []


def test_pairings_disabled_flag_passthrough(world, db, monkeypatch):
    """Flag off: all three pairings, even for an empty-set principal."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    rows = list_pairings(
        latest=False, limit=None, offset=None, db=db, current_user=NOBODY
    )
    assert {p.id for p in rows} == {p.id for p in world["pairings"].values()}


def test_pairings_gate_composes_with_sample_attribute_filters(
    world, db, monkeypatch
):
    """The gate ANDs with the existing either-side sample filter (the
    aliases are shared, not duplicated): sample_id=N-ARG matches PA and
    PC on their normal side."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    by_id = {p.id: tag for tag, p in world["pairings"].items()}
    n_arg = world["samples"]["N-ARG"].id
    rows = list_pairings(
        latest=False,
        limit=None,
        offset=None,
        sample_id=n_arg,
        db=db,
        current_user=DMP_ONLY,
    )
    # PA fails the DMP gate (both sides ARGOS); PC passes it via its
    # DMP tumor side even though the attribute filter matched its
    # normal side.
    assert {by_id[p.id] for p in rows} == {"PC"}
    # The same attribute filter for an ARGOS-only principal: both
    # pairings carry N-ARG and both have an ARGOS side, so both pass.
    rows = list_pairings(
        latest=False,
        limit=None,
        offset=None,
        sample_id=n_arg,
        db=db,
        current_user=ARGOS_ONLY,
    )
    assert {by_id[p.id] for p in rows} == {"PA", "PC"}
    # And a principal whose logics cover neither side (TEMPO-only):
    # nothing.
    rows = list_pairings(
        latest=False,
        limit=None,
        offset=None,
        sample_id=n_arg,
        db=db,
        current_user=_principal(["TEMPO"]),
    )
    assert rows == []


# ---------------------------------------------------------------------------
# HTTP-level: the principal reaches the query via Depends(get_current_user)
# ---------------------------------------------------------------------------


def test_http_listings_use_principal_from_auth_dependency(world, db, monkeypatch):
    """End-to-end through the real FastAPI dependency graph: the
    overridden get_current_user principal drives the Layer B filter on
    all four listings at once."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    state = {"user": DMP_ONLY}

    def override_get_db():
        yield db

    def override_user():
        return state["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    try:
        client = TestClient(app)
        r = client.get("/v1/sample/")
        assert r.status_code == 200
        assert {s["primary_id"] for s in r.json()} == {"S-DMP1", "S-DMP2", "N-DMP"}
        assert r.headers[TOTAL_HEADER] == "3"

        r = client.get("/v1/patient/")
        assert r.status_code == 200
        assert [p["patient__cmo"] for p in r.json()] == ["P-3", "P-7"]

        r = client.get("/v1/request/")
        assert r.status_code == 200
        assert [q["request_id"] for q in r.json()] == ["R-3", "R-7"]

        r = client.get("/v1/pairing/")
        assert r.status_code == 200
        by_id = {p.id: tag for tag, p in world["pairings"].items()}
        assert {by_id[UUID(p["id"])] for p in r.json()} == {"PB", "PC"}

        # Swap the principal: ARGOS-only now sees its own set.
        state["user"] = ARGOS_ONLY
        r = client.get("/v1/sample/")
        assert {s["primary_id"] for s in r.json()} == {"S-ARG1", "N-ARG"}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
