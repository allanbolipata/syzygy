"""P3 — consent gate: non-consent DMP samples need the NONCONSENT entitlement.

While ``logic_gating_enabled`` is set, DMP-family samples with
``consent IS FALSE`` are withheld from a principal that lacks the
non-consent entitlement (the ``can_see_nonconsent`` flag, derived from the
configured ``syzygy_nonconsent`` claim). ``consent IS TRUE`` and
``consent IS NULL`` are unaffected, and non-DMP samples (consent NULL) are
always unaffected — a non-DMP sample's consent can never be FALSE, so the
single ``consent IS NOT FALSE`` predicate hides exactly the non-consent DMP
samples and nothing else.

Dark launch (``logic_gating_enabled`` off) never applies the gate: listings
behave exactly as today. Fail-closed: a principal whose dict lacks the
``can_see_nonconsent`` flag (a token from before the claim existed) gets the
gate.

The gate is a Layer B row filter on the four listings (samples / patients /
requests / pairings) and on the Layer A logic-scoped DMP sample read
(``GET /pairing/unmatched/``). ``GET /auth/me`` exposes the flag so the UI
can react.

Direct-call convention (repo standard): endpoint functions are called with an
in-memory SQLite session; ``current_user`` is a plain kwarg carrying the
``can_see_nonconsent`` flag. HTTP-level TestClient tests confirm the flag
reaches the query through the real ``Depends(get_current_user)`` graph and
that ``/auth/me`` surfaces it.

NOTE: the DMP Pair Report (``/pairing/report/``, ``/report-stats/``,
``/export/``) is a separate, aggregate+streaming surface tracked as a
follow-up card; this ticket gates the sample-level listing reads.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import _nonconsent_entitlement, get_current_user
from syzygy.database import Base, get_db, settings
from syzygy.main import app
from syzygy.models import BaitSet, PairingLogic, Sample
from syzygy.routers.pairings import list_unmatched_dmp_samples
from syzygy.routers.patients import list_patients
from syzygy.routers.samples import list_samples

BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)
TOTAL_HEADER = "X-Total-Count"


def _principal(logics, can_see_nonconsent):
    """A synthetic get_current_user result carrying the P3 consent flag."""
    return {
        "sub": "consent-user",
        "email": "consent@example.com",
        "preferred_username": "consent",
        "display_name": "Consent User",
        "allowed_logics": set(logics),
        "is_logic_admin": False,
        "can_see_nonconsent": can_see_nonconsent,
    }


# A DMP member WITHOUT the non-consent entitlement (fail-closed).
DMP_NO_CLAIM = _principal(["DMP"], can_see_nonconsent=False)
# A DMP member WITH the non-consent entitlement.
DMP_WITH_CLAIM = _principal(["DMP"], can_see_nonconsent=True)
# An ARGOS-only member without the claim (non-DMP, must be unaffected).
ARGOS_NO_CLAIM = _principal(["ARGOS"], can_see_nonconsent=False)


# ---------------------------------------------------------------------------
# Claim-level entitlement derivation (_nonconsent_entitlement)
#
# The KC 26.8.1 built-in Group Membership mapper emits ALL of the user's
# group names into the claim for ANY user who has at least one group — it
# does not filter on its configured group (verified live: the
# GroupMembershipMapper class never references claim.value). So a
# NONCONSENT non-member's token STILL carries the claim (with their other
# group names); the entitlement must be list MEMBERSHIP of the group name,
# not claim truthiness (a truthy check would grant the gate to everyone).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim_value", "expected"),
    [
        pytest.param(["ARGOS"], False, id="non-member-group-list"),
        pytest.param(["DMP", "NONCONSENT"], True, id="member-multi-group"),
        pytest.param(["NONCONSENT"], True, id="member-only"),
        pytest.param([], False, id="empty-list"),
        pytest.param(True, True, id="scalar-truthy-option-b"),
        pytest.param(0, False, id="scalar-falsy"),
    ],
)
def test_nonconsent_entitlement_claim_shapes(
        monkeypatch, claim_value, expected):
    """Entitlement = the NONCONSENT group name is in the list claim (or a
    scalar org claim is truthy); a non-member's group list denies even
    though the claim is present and truthy."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    claims = {settings.nonconsent_claim: claim_value}
    assert _nonconsent_entitlement(claims) is expected


def test_nonconsent_entitlement_absent_claim_fail_closed(monkeypatch):
    """Gating on + absent claim (no groups at all / pre-claim token) ->
    the gate applies (no entitlement)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    assert _nonconsent_entitlement({}) is False


def test_nonconsent_entitlement_gating_off_is_open(monkeypatch):
    """Dark launch (gating off): the flag is True regardless of claims —
    the consent gate never applies."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    assert _nonconsent_entitlement({}) is True


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
def consent_world(db):
    """Seed a two-logic world with explicit consent values.

    Logics / bait sets:
      DMP   <- bait "DMP-BAIT"  (plus the null-bait sample_type 'DMP' rule)
      ARGOS <- bait "ARGOS-BAIT"

    DMP samples (all unpaired — no pairings exist in this fixture, so all
    three are returned by the unmatched DMP read):
      S-NC    consent False -> hidden without the claim
      S-C     consent True  -> always visible
      S-NULL  consent None  -> always visible
    Non-DMP sample:
      S-ARG   ARGOS, consent None -> unaffected (never a non-consent DMP)
    """
    dmp = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    argos = PairingLogic(name="ARGOS", strategy_name="default", active=True)
    db.add_all([dmp, argos])
    db.commit()
    db.add_all(
        [
            BaitSet(bait_set="DMP-BAIT", pairing_logic_id=dmp.id),
            BaitSet(bait_set="ARGOS-BAIT", pairing_logic_id=argos.id),
        ]
    )
    db.commit()

    # pid: (bait, sample_type, patient, request, consent)
    specs = {
        "S-NC": (None, "DMP", "P-NC", "R-NC", False),
        "S-C": (None, "DMP", "P-C", "R-C", True),
        "S-NULL": (None, "DMP", "P-NULL", "R-NULL", None),
        "S-ARG": ("ARGOS-BAIT", "tissue", "P-ARG", "R-ARG", None),
    }
    samples = {}
    for i, (pid, (bait, stype, patient, request, consent)) in enumerate(specs.items()):
        sample = Sample(
            primary_id=pid,
            cmo_sample_name=f"{pid}_name",
            patient__cmo=patient,
            request_id=request,
            sample_type=stype,
            gene_panel="IMPACT468",
            bait_set=bait,
            tumor_or_normal="TUMOR",
            consent=consent,
            created_date=BASE_TIME + timedelta(minutes=i),
        )
        db.add(sample)
        samples[pid] = sample
    db.commit()
    return {"logics": {"DMP": dmp, "ARGOS": argos}, "samples": samples}


def _ids(rows):
    return {r.primary_id for r in rows}


# ---------------------------------------------------------------------------
# Samples (Layer B row filter) — the four spec cases
# ---------------------------------------------------------------------------


def test_consent_false_hidden_for_dmp_member_without_claim(
    consent_world, db, monkeypatch
):
    """Accept: consent=FALSE DMP samples are hidden for a DMP member
    without the non-consent entitlement; TRUE and NULL are unaffected."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=DMP_NO_CLAIM)
    )
    assert "S-NC" not in ids
    assert "S-C" in ids
    assert "S-NULL" in ids
    assert ids == {"S-C", "S-NULL"}


def test_consent_false_visible_with_claim(consent_world, db, monkeypatch):
    """Accept: the same consent=FALSE DMP sample is visible once the
    principal holds the non-consent entitlement."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=DMP_WITH_CLAIM)
    )
    assert ids == {"S-NC", "S-C", "S-NULL"}


def test_consent_true_unaffected(consent_world, db, monkeypatch):
    """Accept: consent=TRUE DMP samples are never hidden by the gate."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=DMP_NO_CLAIM)
    )
    assert "S-C" in ids


def test_non_dmp_samples_unaffected(consent_world, db, monkeypatch):
    """Accept: non-DMP samples (consent NULL) are unaffected — an ARGOS-only
    principal without the claim still sees its own sample."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=ARGOS_NO_CLAIM)
    )
    assert ids == {"S-ARG"}


# ---------------------------------------------------------------------------
# Gate mechanics: dark launch, fail-closed, composition, totals
# ---------------------------------------------------------------------------


def test_dark_launch_gate_off_passthrough(consent_world, db, monkeypatch):
    """Dark launch: logic_gating_enabled off -> the gate never applies, so a
    DMP member without the claim still sees the consent=FALSE sample."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=DMP_NO_CLAIM)
    )
    # Gate off: no logic restriction and no consent gate -> every sample is
    # visible, including the consent=FALSE DMP sample.
    assert "S-NC" in ids
    assert ids == {"S-NC", "S-C", "S-NULL", "S-ARG"}


def test_fail_closed_missing_flag(consent_world, db, monkeypatch):
    """Fail-closed: a principal whose dict LACKS ``can_see_nonconsent`` (a
    token from before the claim existed) gets the gate (S-NC hidden)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    legacy = {
        "sub": "legacy",
        "email": "legacy@example.com",
        "preferred_username": "legacy",
        "display_name": "Legacy",
        "allowed_logics": {"DMP"},
        "is_logic_admin": False,
        # NOTE: deliberately no can_see_nonconsent key.
    }
    ids = _ids(
        list_samples(limit=None, offset=None, db=db, current_user=legacy)
    )
    assert "S-NC" not in ids
    assert ids == {"S-C", "S-NULL"}


def test_consent_gate_composes_with_logic_scoped_read(consent_world, db, monkeypatch):
    """Layer A logic-scoped read: scoping the samples listing to the DMP
    logic still applies the consent gate (S-NC hidden without the claim)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    without = _ids(
        list_samples(
            limit=None,
            offset=None,
            pairing_logic="DMP",
            db=db,
            current_user=DMP_NO_CLAIM,
        )
    )
    assert without == {"S-C", "S-NULL"}
    with_claim = _ids(
        list_samples(
            limit=None,
            offset=None,
            pairing_logic="DMP",
            db=db,
            current_user=DMP_WITH_CLAIM,
        )
    )
    assert with_claim == {"S-NC", "S-C", "S-NULL"}


def test_total_reflects_consent_gated_set(consent_world, db, monkeypatch):
    """X-Total-Count reflects the consent-gated set, pre-pagination."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    response = Response()
    list_samples(
        limit=None,
        offset=None,
        db=db,
        current_user=DMP_NO_CLAIM,
        response=response,
    )
    # S-C + S-NULL survive; S-NC is hidden.
    assert response.headers[TOTAL_HEADER] == "2"


# ---------------------------------------------------------------------------
# Layer A logic-scoped DMP read: GET /pairing/unmatched/
# ---------------------------------------------------------------------------


def test_unmatched_dmp_consent_gate(consent_world, db, monkeypatch):
    """The unmatched DMP sample read hides the consent=FALSE sample without
    the claim and shows it with the claim (all three samples are unpaired)."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    without = _ids(
        list_unmatched_dmp_samples(
            pairing_logic_name="DMP",
            limit=None,
            offset=None,
            db=db,
            current_user=DMP_NO_CLAIM,
        )
    )
    assert without == {"S-C", "S-NULL"}
    with_claim = _ids(
        list_unmatched_dmp_samples(
            pairing_logic_name="DMP",
            limit=None,
            offset=None,
            db=db,
            current_user=DMP_WITH_CLAIM,
        )
    )
    assert with_claim == {"S-NC", "S-C", "S-NULL"}


# ---------------------------------------------------------------------------
# Group-level (patients): a patient with only a non-consent DMP sample
# has no surviving rows and is hidden.
# ---------------------------------------------------------------------------


def test_patients_consent_gate_excludes_nonconsent_dmp(consent_world, db, monkeypatch):
    """Group-level: the consent gate is a WHERE before the GROUP BY, so a
    patient whose only DMP sample is non-consent disappears without the
    claim and reappears with it."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    without = [
        p.patient__cmo
        for p in list_patients(
            limit=None, offset=None, q=None, db=db, current_user=DMP_NO_CLAIM
        )
    ]
    assert "P-NC" not in without
    assert set(without) == {"P-C", "P-NULL"}
    with_claim = [
        p.patient__cmo
        for p in list_patients(
            limit=None, offset=None, q=None, db=db, current_user=DMP_WITH_CLAIM
        )
    ]
    assert set(with_claim) == {"P-NC", "P-C", "P-NULL"}


# ---------------------------------------------------------------------------
# /auth/me exposes the flag
# ---------------------------------------------------------------------------


def test_auth_me_exposes_consent_flag(monkeypatch):
    """Accept (UI): GET /auth/me surfaces the can_see_nonconsent flag."""
    state = {"user": DMP_WITH_CLAIM}

    def override_user():
        return state["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        client = TestClient(app)
        r = client.get("/auth/me")
        assert r.status_code == 200
        assert r.json()["can_see_nonconsent"] is True

        state["user"] = DMP_NO_CLAIM
        r = client.get("/auth/me")
        assert r.status_code == 200
        assert r.json()["can_see_nonconsent"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# HTTP-level: the flag reaches the query via the real dependency graph
# ---------------------------------------------------------------------------


def test_http_consent_gate_reaches_query(consent_world, db, monkeypatch):
    """End-to-end through the real FastAPI dependency graph: the overridden
    get_current_user principal's consent flag drives the samples-listing
    gate."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    state = {"user": DMP_NO_CLAIM}

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
        assert {s["primary_id"] for s in r.json()} == {"S-C", "S-NULL"}
        assert r.headers[TOTAL_HEADER] == "2"

        state["user"] = DMP_WITH_CLAIM
        r = client.get("/v1/sample/")
        assert r.status_code == 200
        assert {s["primary_id"] for s in r.json()} == {"S-NC", "S-C", "S-NULL"}
        assert r.headers[TOTAL_HEADER] == "3"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
