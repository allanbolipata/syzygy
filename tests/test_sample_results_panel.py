"""Tests for the sample results panels replacing the sample dropdowns
(card t_4f824f62: Matches & Overrides / Try Pairing).

Static UI wiring only (test_login_page.py / test_view_selector.py
convention): the #m-sample and #tq-sample dropdowns are GONE; each tab has
a p-detail-style clickable results panel with a capped (50) result list, a
"selected" summary bar, and the downstream buttons (#m-show / #tq-run)
still wired. app.js keeps no stale reads of the removed selects and drives
both panels through the shared renderSampleResults() picker.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


# ---------- removed dropdowns ----------

def test_sample_dropdowns_are_gone():
    for token in ('id="m-sample"', 'id="tq-sample"', "m-sample", "tq-sample"):
        assert token not in INDEX_HTML, f"sample dropdown {token!r} still in index.html"
    assert "sample (matched)" not in INDEX_HTML


def test_app_js_has_no_stale_select_reads():
    assert '$("#m-sample")' not in APP_JS
    assert '$("#tq-sample")' not in APP_JS
    assert '$("#m-logic")' not in APP_JS  # removed by the View-selector card
    assert '$("#tq-logic")' not in APP_JS


def test_css_drops_dead_select_rules():
    assert "#m-sample" not in STYLE_CSS
    assert "#m-logic" not in STYLE_CSS


# ---------- matches & overrides results panel ----------

M_ANCHORS = [
    'id="m-results"', 'id="m-results-table"', 'id="m-results-count"',
    'id="m-results-empty"', 'id="m-results-more"',
    'id="m-selected-bar"', 'id="m-selected-text"',
]

TQ_ANCHORS = [
    'id="tq-results"', 'id="tq-results-table"', 'id="tq-results-count"',
    'id="tq-results-empty"', 'id="tq-results-more"',
    'id="tq-selected-bar"', 'id="tq-selected-text"',
]


def test_matches_results_panel_anchors_exist():
    for anchor in M_ANCHORS:
        assert anchor in INDEX_HTML, f"missing {anchor}"


def test_try_results_panel_anchors_exist():
    for anchor in TQ_ANCHORS:
        assert anchor in INDEX_HTML, f"missing {anchor}"


def test_results_tables_have_spec_columns():
    # primary_id, cmo_sample_name, patient, request, type — both panels.
    col_re = re.compile(
        r"<thead>\s*<tr><th>primary_id</th><th>cmo_sample_name</th>"
        r"<th>patient</th><th>request</th><th>type</th></tr>\s*</thead>"
    )
    tables = re.findall(r'<table id="(?:m|tq)-results-table">.*?</table>', INDEX_HTML, re.S)
    assert len(tables) == 2
    for table in tables:
        assert col_re.search(table), f"unexpected columns in {table[:80]}..."


# ---------- downstream controls unchanged ----------

def test_downstream_buttons_survive():
    assert 'id="m-show"' in INDEX_HTML and "Show matches" in INDEX_HTML
    assert 'id="tq-run"' in INDEX_HTML and "Run dry run" in INDEX_HTML
    assert '$("#m-show").addEventListener' in APP_JS
    assert '$("#tq-run").addEventListener' in APP_JS
    # pairing/override plumbing untouched:
    assert "/possible-matches" in APP_JS
    assert 'api("/pairing/trigger"' in APP_JS


def test_row_click_selection_wired():
    # shared picker builds click rows and calls back into the tab selectors
    assert "function renderSampleResults(" in APP_JS
    assert 'tr class="click' in APP_JS
    assert 'querySelectorAll("tr[data-id]")' in APP_JS
    # matches tab: row click selects + runs; search preselects without running
    assert "function renderMatchesResults()" in APP_JS
    assert "async function selectMatchesSample(sample, run = true)" in APP_JS
    assert "selectMatchesSample(results[0], false)" in APP_JS
    # try tab: row click selects the dry-run target
    assert "function tryRenderResults()" in APP_JS
    assert re.search(r"\(sample\) => selectTrySample\(sample\)", APP_JS)
    # "selected" summary bars
    assert "function renderSelectedBar(" in APP_JS
    assert 'renderSelectedBar("m"' in APP_JS
    assert 'renderSelectedBar("tq"' in APP_JS


def test_results_capped_at_50_with_hint():
    assert re.search(r"const SAMPLE_PICK_LIMIT = 50;", APP_JS)
    # both lookups use the cap
    assert "limit=${SAMPLE_PICK_LIMIT}" in APP_JS
    assert APP_JS.count("SAMPLE_PICK_LIMIT") >= 4
    # no per-row pagination: the cap hint exists in both panels
    assert "Results capped at 50" in INDEX_HTML
    assert INDEX_HTML.count("Results capped at 50") == 2
    assert "const capped" in APP_JS
    # the old 25-row dropdown queries are gone
    assert "&limit=25" not in APP_JS


# ---------- selected-row visual language ----------

def test_selected_row_styling_exists():
    assert "tbody tr.selected" in STYLE_CSS
    assert '" selected"' in APP_JS  # row class applied to the picked sample
