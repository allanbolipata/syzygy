"""Tests for the clickable Pair Report identifier links (card t_d8720219).

Static UI wiring only (test_login_page.py / test_view_selector.py
convention): every Pair Report column set (PAIR, DMP, HARMONY) renders its
sample / patient / request identifier columns as nav-link cells. Sample
clicks land on the Samples tab with #f-q prefilled and the exact row
focused; patient / request clicks reuse the existing cross-view helpers
(prefill + open the detail panel); DMP patient ids are conditional links —
they navigate only when a patient record matches, else no-op with an info
banner and an explanatory tooltip. Navigation preserves target-tab state:
it touches only the search box needed to surface the target and never the
topbar View selector.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _slice(start_marker: str, end_marker: str) -> str:
    i = APP_JS.index(start_marker)
    j = APP_JS.index(end_marker, i)
    return APP_JS[i:j]


# ---------- column models declare links ----------

def test_pair_columns_declare_identifier_links():
    for key, helper in [
        ("tumor_primary_id", "sampleLink"),
        ("normal_primary_id", "sampleLink"),
        ("tumor_cmo_sample_name", "sampleLink"),
        ("normal_cmo_sample_name", "sampleLink"),
        ("tumor_patient__cmo", "patientLink"),
        ("normal_patient__cmo", "patientLink"),
        ("tumor_patient__dmp", "dmpPatientLink"),
        ("normal_patient__dmp", "dmpPatientLink"),
        ("tumor_request_id", "requestLink"),
        ("normal_request_id", "requestLink"),
    ]:
        assert re.search(
            rf'key: "{key}",[^\n]*link:[^\n]*{helper}', APP_JS
        ), f"PAIR column {key} missing {helper} link"


def test_dmp_columns_declare_identifier_links():
    for key in ("tumor_sample", "normal_sample"):
        assert re.search(
            rf'key: "{key}", header: "{key}", group:[^\n]*link: \(r\) => sampleLink', APP_JS
        ), f"DMP column {key} missing sampleLink"
    # The DMP patient column links conditionally: NA never links, the row's
    # own patient__cmo is passed as the direct-resolution proof.
    dmp_patient_line = re.search(
        r'key: "patient", header: "patient",[^\n]*', APP_JS
    ).group(0)
    assert "dmpPatientLink" in dmp_patient_line
    assert "DMP_NA ? null" in dmp_patient_line
    assert "r.tumor && r.tumor.patient__cmo" in dmp_patient_line


def test_dmp_legend_documents_patient_link():
    legend = _slice("const DMP_LEGEND = {", "const DMP_DEFAULT_KEYS")
    assert "links to the patient detail when a patient record matches" in legend


# ---------- cell rendering ----------

def test_pair_cell_html_renders_nav_links_with_tooltips():
    body = _slice("function pairCellHtml", "/* HARMONY: normal-oriented")
    for attr in ("data-goto-sample", "data-nav-patient", "data-nav-request", "data-nav-dmp-patient"):
        assert attr in body, f"pairCellHtml missing {attr}"
    assert 'class="nav-link"' in body
    assert 'title="${esc(PAIR_LINK_TITLES[link.kind])}"' in body
    # The dmp-patient kind carries the row's own patient__cmo for the
    # direct (query-free) resolution when present.
    assert "data-nav-cmo=" in body
    # Legacy sample semantics (openMatchesFor routing) is gone from the cell.
    assert "data-sample-id" not in body


def test_link_kind_helper_exists():
    assert re.search(r"const dmpPatientLink = \(dmpId, cmoId\)", APP_JS)
    titles = _slice("const PAIR_LINK_TITLES = {", "function pairCellHtml")
    assert "dmp-patient" in titles
    assert "Samples tab" in titles


# ---------- #pairs-table click routing ----------

PAIRS_HANDLER = _slice('$("#pairs-table").addEventListener', "\nrenderPrGroups();")


def test_pair_table_routes_samples_to_the_samples_tab():
    assert "navigateToSampleInSamples" in PAIRS_HANDLER
    # Pair Report sample links deliberately land on the Samples listing, not
    # on Matches & Overrides (the old behaviour).
    assert "openMatchesFor" not in PAIRS_HANDLER
    # PAIR/DMP values resolve to the loaded record; HARMONY values fall back
    # to being used as the search term.
    assert "pairsState.rows.find" in PAIRS_HANDLER
    assert "sample || v" in PAIRS_HANDLER


def test_pair_table_routes_patient_request_and_dmp_patient():
    assert 'navigateToPatient(el.dataset.navPatient)' in PAIRS_HANDLER
    assert 'navigateToRequest(el.dataset.navRequest)' in PAIRS_HANDLER
    assert 'navigateToDmpPatient(el.dataset.navDmpPatient, el.dataset.navCmo || null)' in PAIRS_HANDLER


# ---------- navigation helpers ----------

SAMPLE_NAV = _slice("function navigateToSampleInSamples", "/* Highlight + scroll")
FOCUS_ROW = _slice("function focusSampleRow", "/* DMP patient ids are conditional")
DMP_NAV = _slice("async function navigateToDmpPatient", "/* ---------- samples view ---------- */")


def test_samples_navigation_prefills_search_and_reloads():
    assert '$("#f-q").value = term;' in SAMPLE_NAV
    assert 'showView("samples")' in SAMPLE_NAV
    assert "loadSamples(0).then(() => focusSampleRow(term))" in SAMPLE_NAV


def test_role_select_relaxed_only_on_conflict():
    # A role filter that hides the link target is relaxed to "any" (Pair
    # Report links target both tumors and normals); it is never tightened,
    # and a non-conflicting selection is preserved.
    assert 'const sel = $("#f-tumor_or_normal");' in SAMPLE_NAV
    assert 'if (role && sel.value && sel.value !== String(role).toUpperCase()) sel.value = "";' in SAMPLE_NAV
    assert 'el.dataset.role || null' in PAIRS_HANDLER


def test_row_focus_uses_flash_and_scroll():
    assert "row-flash" in FOCUS_ROW
    assert 'scrollIntoView' in FOCUS_ROW
    # Exact-match focus only: a partial-candidate landing stays silent.
    assert ".toLowerCase() === want" in FOCUS_ROW


def test_dmp_patient_navigation_is_conditional():
    # Fast path: the row's own patient__cmo.
    assert "if (directCmo) return navigateToPatient(directCmo);" in DMP_NAV
    # Fallback: resolve via the samples listing on exact patient__dmp.
    assert "/sample/?q=" in DMP_NAV
    assert "patient__dmp" in DMP_NAV
    # Unresolved = no-op (no navigation happened before this line) + banner.
    assert 'banner("info"' in DMP_NAV
    assert "staying in the Pair Report" in DMP_NAV


def test_navigation_preserves_view_selector_state():
    bodies = {
        "navigateToPatient": _slice("function navigateToPatient", "function navigateToRequest"),
        "navigateToRequest": _slice("function navigateToRequest", "/* Sample links in the Pair Report"),
        "navigateToSampleInSamples": SAMPLE_NAV,
        "navigateToDmpPatient": DMP_NAV,
    }
    for name, body in bodies.items():
        assert "setViewLogic" not in body, f"{name} must not touch the View selector"
        assert "#view-logic" not in body, f"{name} must not touch the View selector"


# ---------- HARMONY table ----------

HARMONY_CELL = _slice("function harmonyCellHtml", "/* URLSearchParams for GET /pairing/harmony/")
HARMONY_TABLE = _slice("function renderHarmonyTable", "function renderPairsTable")


def test_harmony_identifier_columns_link():
    assert 'data-goto-sample="' in HARMONY_CELL
    assert 'key === "normal_sample" || key === "tumor_sample"' in HARMONY_CELL
    assert 'data-nav-dmp-patient="' in HARMONY_CELL
    assert 'key === "patient"' in HARMONY_CELL
    # The column key is the role hint for the Samples landing.
    assert 'data-role="${role}"' in HARMONY_CELL
    assert '? "TUMOR" : "NORMAL"' in HARMONY_CELL
    # bam-path columns keep their untouched em-dash placeholder rendering.
    assert "\\u2014" in HARMONY_CELL


def test_harmony_table_renders_through_cell_helper():
    assert "harmonyCellHtml(r, c.key)" in HARMONY_TABLE
    assert "esc(harmonyCellText(r, c.key))" not in HARMONY_TABLE


# ---------- CSS ----------

def test_nav_link_and_flash_styling_exist():
    assert ".nav-link" in STYLE_CSS
    assert "@keyframes row-flash" in STYLE_CSS
    assert "tbody tr.row-flash" in STYLE_CSS
