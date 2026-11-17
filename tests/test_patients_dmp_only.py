"""C4 — Patients tab DMP-only group view.

Backend: ``GET /v1/patient/`` gains ``dmp_only: bool = False``. With the
flag set the listing groups by ``patient__dmp`` WHERE ``patient__cmo IS
NULL`` (the pure-DMP patients invisible to the CMO view) — same row shape
(``PatientRead``: group id + sample/tumor/normal counts + requests +
derived metadata) and the same X-Total-Count pre-pagination convention;
``q`` partial-matches the group key (patient__dmp in this view). While
``logic_gating_enabled`` is set the DMP-only view is gated on DMP
visibility: a principal whose ``allowed_logics`` does not include DMP gets
403 with the Layer A stable detail (``LOGIC_ACCESS_DENIED_DETAIL``); with
the flag off the view is open (dark-launch passthrough).

Static UI (test_pair_report_links.py convention): the Patients tab has a
CMO-groups / DMP-only-groups toggle, hidden by default and shown only
when the /auth/me ``allowed_logics`` include "DMP"; DMP-only rows link to
the Samples search prefilled with the dmp id (the Samples q matches
patient__dmp since dev @ 4504257).

Direct-call convention (repo standard): endpoint functions are called with
an in-memory SQLite session and limit/offset passed explicitly;
``current_user`` is a plain kwarg in C1 principal shape.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from syzygy.auth import LOGIC_ACCESS_DENIED_DETAIL
from syzygy.database import Base, settings
from syzygy.models import BaitSet, PairingLogic, Sample
from syzygy.routers.patients import list_patients
from syzygy.schemas import TumorOrNormal

BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)
TOTAL_HEADER = "X-Total-Count"


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
    """Seed a two-logic world with CMO, mixed and pure-DMP patients.

    Logic families:
      ARGOS <- bait set "IMPACT468"
      DMP   <- bait set "DMP-BAIT"  (+ bait-NULL sample_type 'DMP')

    Samples:
      S-A1  IMPACT468            (ARGOS)  cmo P-1  dmp None   TUMOR
      S-M1  bait NULL, DMP       (DMP)    cmo P-2  dmp D-9    TUMOR
              -- mixed patient: has BOTH ids, so it belongs to the CMO
              -- view only, never to the DMP-only view.
      S-D1  bait NULL, DMP       (DMP)    cmo None dmp D-1    TUMOR
      S-D2  DMP-BAIT             (DMP)    cmo None dmp D-1    NORMAL
      S-D3  bait NULL, DMP       (DMP)    cmo None dmp D-2    TUMOR
      S-N1  bait NULL, tissue    (none)   cmo None dmp None   TUMOR
              -- orphan: no patient id at all, no group in either view.
    """
    argos = PairingLogic(name="ARGOS", strategy_name="default", active=True)
    dmp = PairingLogic(name="DMP", strategy_name="dmp", active=True)
    db.add_all([argos, dmp])
    db.commit()
    db.add_all(
        [
            BaitSet(bait_set="IMPACT468", pairing_logic_id=argos.id),
            BaitSet(bait_set="DMP-BAIT", pairing_logic_id=dmp.id),
        ]
    )
    db.commit()

    specs = {
        # (bait, sample_type, cmo, dmp, role, request)
        "S-A1": ("IMPACT468", "tissue", "P-1", None, TumorOrNormal.TUMOR, "R-1"),
        "S-M1": (None, "DMP", "P-2", "D-9", TumorOrNormal.TUMOR, "R-2"),
        "S-D1": (None, "DMP", None, "D-1", TumorOrNormal.TUMOR, "R-3"),
        "S-D2": ("DMP-BAIT", "tissue", None, "D-1", TumorOrNormal.NORMAL, "R-3"),
        "S-D3": (None, "DMP", None, "D-2", TumorOrNormal.TUMOR, "R-4"),
        "S-N1": (None, "tissue", None, None, TumorOrNormal.TUMOR, "R-5"),
    }
    for i, (pid, (bait, stype, cmo, dmp_id, ton, request)) in enumerate(specs.items()):
        db.add(
            Sample(
                primary_id=pid,
                cmo_sample_name=f"{pid}_name",
                patient__cmo=cmo,
                patient__dmp=dmp_id,
                request_id=request,
                sample_type=stype,
                gene_panel="IMPACT468",
                bait_set=bait,
                tumor_or_normal=ton.value,
                created_date=BASE_TIME + timedelta(minutes=i),
            )
        )
    db.commit()
    return {"logics": {"ARGOS": argos, "DMP": dmp}}


def _dmp_ids(rows):
    return [p.patient__dmp for p in rows]


# ---------------------------------------------------------------------------
# Grouping: shape, totals, q filter
# ---------------------------------------------------------------------------


def test_dmp_only_groups_shape(world, db):
    """Groups by patient__dmp where patient__cmo IS NULL — same row shape
    as the CMO view, keyed on patient__dmp (patient__cmo null)."""
    rows = list_patients(limit=None, offset=None, q=None, db=db, dmp_only=True)
    assert _dmp_ids(rows) == ["D-1", "D-2"]
    first = rows[0]
    assert first.patient__cmo is None
    assert first.patient__dmp == "D-1"
    # D-1 holds two samples (bait-NULL DMP tumor + DMP-baited normal) —
    # counts cover ALL of the group's samples.
    assert first.sample_count == 2
    assert first.tumor_count == 1
    assert first.normal_count == 1
    assert first.requests == ["R-3"]
    assert rows[1].sample_count == 1
    assert rows[1].tumor_count == 1
    assert rows[1].normal_count == 0


def test_dmp_only_totals_pre_pagination(world, db):
    """X-Total-Count reflects the gated/filtered group set, independent of
    limit/offset (pre-pagination convention, same as the CMO view)."""
    response = Response()
    page1 = list_patients(
        limit=1, offset=0, q=None, db=db, dmp_only=True, response=response
    )
    assert _dmp_ids(page1) == ["D-1"]
    assert response.headers[TOTAL_HEADER] == "2"
    response = Response()
    page2 = list_patients(
        limit=1, offset=1, q=None, db=db, dmp_only=True, response=response
    )
    assert _dmp_ids(page2) == ["D-2"]
    assert response.headers[TOTAL_HEADER] == "2"
    # No groups at all when the filter matches nothing.
    response = Response()
    assert list_patients(
        limit=None, offset=None, q="no-such-dmp", db=db, dmp_only=True, response=response
    ) == []
    assert response.headers[TOTAL_HEADER] == "0"


def test_dmp_only_q_filter(world, db):
    """q is a case-insensitive partial match on the GROUP KEY — in the
    DMP-only view that is patient__dmp, not patient__cmo."""
    assert _dmp_ids(
        list_patients(limit=None, offset=None, q="D-1", db=db, dmp_only=True)
    ) == ["D-1"]
    # Case-insensitive.
    assert _dmp_ids(
        list_patients(limit=None, offset=None, q="d-2", db=db, dmp_only=True)
    ) == ["D-2"]
    # A cmo id must NOT match: q searches the dmp id in this view.
    assert (
        list_patients(limit=None, offset=None, q="P-1", db=db, dmp_only=True) == []
    )


def test_dmp_only_excludes_mixed_cmo_patients_and_orphans(world, db):
    """patient__cmo IS NULL is the defining rule: the mixed patient P-2
    (dmp id D-9) belongs to the CMO view only; the id-less orphan sample
    forms no group in either view."""
    dmp_rows = list_patients(limit=None, offset=None, q=None, db=db, dmp_only=True)
    assert "D-9" not in _dmp_ids(dmp_rows)
    assert None not in _dmp_ids(dmp_rows)  # no null group from the orphan

    cmo_rows = list_patients(limit=None, offset=None, q=None, db=db)
    assert [p.patient__cmo for p in cmo_rows] == ["P-1", "P-2"]
    assert all(p.patient__dmp is None for p in cmo_rows)  # CMO view shape
    # The pure-DMP patients stay invisible to the CMO view (today's
    # behavior, unchanged).
    assert "D-1" not in {p.patient__cmo for p in cmo_rows}


# ---------------------------------------------------------------------------
# Gating: DMP visibility while logic_gating_enabled
# ---------------------------------------------------------------------------


def test_dmp_only_visible_to_dmp_principal_when_gating(world, db, monkeypatch):
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    rows = list_patients(
        limit=None, offset=None, q=None, db=db, dmp_only=True, current_user=DMP_ONLY
    )
    assert _dmp_ids(rows) == ["D-1", "D-2"]
    # The C3 HAVING gate ANDs in as well: both groups' samples are DMP
    # family, so a DMP principal sees them, and the total is pre-pagination.
    response = Response()
    list_patients(
        limit=1,
        offset=0,
        q=None,
        db=db,
        dmp_only=True,
        current_user=DMP_ONLY,
        response=response,
    )
    assert response.headers[TOTAL_HEADER] == "2"


def test_dmp_only_403_when_dmp_not_allowed(world, db, monkeypatch):
    """Gated on DMP visibility: no DMP in allowed_logics -> 403 with the
    Layer A stable detail, for an ARGOS-only principal AND the fail-closed
    empty-set principal."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    for principal in (ARGOS_ONLY, NOBODY):
        with pytest.raises(HTTPException) as excinfo:
            list_patients(
                limit=None,
                offset=None,
                q=None,
                db=db,
                dmp_only=True,
                current_user=principal,
            )
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == LOGIC_ACCESS_DENIED_DETAIL


def test_dmp_only_flag_off_passthrough(world, db, monkeypatch):
    """Dark-launch: with the flag off the DMP-only view is open to
    everyone, even a principal with an empty allowed set."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False)
    rows = list_patients(
        limit=None, offset=None, q=None, db=db, dmp_only=True, current_user=NOBODY
    )
    assert _dmp_ids(rows) == ["D-1", "D-2"]


def test_cmo_view_unaffected_by_dmp_only_param(world, db, monkeypatch):
    """dmp_only=false keeps today's CMO grouping even under gating (the
    C3 HAVING gate applies as before): a DMP principal sees only the
    mixed patient P-2, which carries a DMP-family sample."""
    monkeypatch.setattr(settings, "logic_gating_enabled", True)
    rows = list_patients(
        limit=None, offset=None, q=None, db=db, current_user=DMP_ONLY
    )
    assert [p.patient__cmo for p in rows] == ["P-2"]


# ---------------------------------------------------------------------------
# Static UI: the Patients-tab toggle (test_pair_report_links.py convention)
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _slice(start_marker: str, end_marker: str, text: str = APP_JS) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def test_patients_tab_toggle_exists_in_html():
    """The Patients tab has a CMO-groups / DMP-only-groups toggle, hidden
    by default (the /auth/me response decides whether it shows)."""
    wrap = _slice('<div id="p-mode-wrap"', "</div>", INDEX_HTML)
    assert 'class="p-mode hidden"' in wrap  # hidden until allowed_logics says DMP
    cmo_btn = _slice('<button id="p-mode-cmo"', "</button>", INDEX_HTML)
    assert "CMO groups" in cmo_btn
    dmp_btn = _slice('<button id="p-mode-dmp"', "</button>", INDEX_HTML)
    assert "DMP-only groups" in dmp_btn
    # The group-key label (search field + first column) is id-tagged so it
    # can follow the active mode.
    assert 'id="p-q-label"' in INDEX_HTML
    assert 'id="p-id-head"' in INDEX_HTML


def test_toggle_gates_on_auth_me_allowed_logics():
    """The toggle is shown only when the /auth/me ``allowed_logics``
    include "DMP" — no knowledge of the gating flag client-side (full
    set while gating is disabled, so the UI needs none)."""
    can = _slice("function canSeeDmpLogic()", "function renderDmpOnlyToggle()")
    assert "currentUser.allowed_logics" in can
    assert '.includes("DMP")' in can
    render = _slice(
        "function renderDmpOnlyToggle()", "function setPatientMode("
    )
    assert 'wrap.classList.toggle("hidden", !canSeeDmpLogic());' in render
    # Losing DMP visibility forces the mode back to CMO groups.
    assert 'patientsState.mode = "cmo";' in render
    # /auth/me is the only source of the visibility decision: the render
    # runs once the me response lands.
    me = _slice("async function loadCurrentUser()", "function signOut()")
    assert 'currentUser = await api("/auth/me");' in me
    assert me.index("currentUser = await api(\"/auth/me\");") < me.index(
        "renderDmpOnlyToggle();"
    )


def test_dmp_mode_requests_dmp_only_param():
    """The DMP-only group view is requested as dmp_only=true on
    /patient/ (the backend's new grouping)."""
    params = _slice("function patientsParams(offset)", "async function loadPatients")
    assert 'if (patientsState.mode === "dmp") p.set("dmp_only", "true");' in params
    # Switching mode resets the Patients query state and reloads from 0.
    switch = _slice("function setPatientMode(mode)", '$("#p-mode-cmo")')
    assert "clearPatientQueries()" in switch
    assert "loadPatients(0);" in switch
    assert '#p-mode-dmp' in APP_JS and "setPatientMode(\"dmp\")" in APP_JS


def test_dmp_only_rows_link_to_samples_search():
    """DMP-only rows link to the Samples search prefilled with the dmp id
    (the Samples q matches patient__dmp); CMO rows keep opening the
    patient detail."""
    render = _slice("function renderPatients()", "/* Sample id ->")
    assert "patientGroupKey(p)" in render
    dmp_branch = _slice(
        'if (patientsState.mode === "dmp") {', "openPatient(row);", render
    )
    # The shared reset-then-prefill helper: clears stale Samples queries,
    # prefills #f-q with the term, switches to the Samples view, reloads
    # and focuses the exact row when present.
    assert "navigateToSampleInSamples(tr.dataset.pid);" in dmp_branch
    assert "openPatient(row);" in render
    # The row id rendered/clicked is the view's group key (dmp id in
    # DMP-only mode).
    assert 'data-pid="${esc(id)}"' in render
