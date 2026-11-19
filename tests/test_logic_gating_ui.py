"""Static-UI tests for the ticket-C5 logic gating (same convention as
tests/test_patients_dmp_only.py and tests/test_view_selector.py: load the
static files once, slice between stable markers, assert anchors).

C5 builds:
- the topbar View selector lists only the ACTIVE logics in the principal's
  /auth/me ``allowed_logics``;
- the three pairing tabs are hidden when no logic is accessible;
- admin-only trigger controls (Override buttons, the "Allow overrides"
  checkbox) render only for logic admins.

UX only — the server enforces the real gates (403 "Logic not accessible" /
"Logic admin required"). The client never reads the gating flag: /auth/me
carries the full set while LOGIC_GATING_ENABLED=false, so it needs none.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
DOCS = (REPO_ROOT / "docs" / "logic_gating_setup.md").read_text(encoding="utf-8")
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _slice(start_marker: str, end_marker: str, text: str = APP_JS) -> str:
    """Return text between (and including) two unique markers."""
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


# ---------------------------------------------------------------------------
# Entitlement helpers: /auth/me is the single source of truth (UX only)
# ---------------------------------------------------------------------------

def test_entitlement_helpers_read_auth_me_only():
    """allowed_logics / is_logic_admin come from the /auth/me principal —
    and the client never reads the gating flag itself (the /auth/me
    response carries the full set while it is off)."""
    set_fn = _slice("function allowedLogicSet()", "function allowedActiveLogics()")
    assert "currentUser.allowed_logics" in set_fn
    assert "new Set(currentUser.allowed_logics)" in set_fn

    admin_fn = _slice(
        "function isLogicAdmin()", "/* Options = ACTIVE logics the principal is ALLOWED"
    )
    assert "currentUser.is_logic_admin" in admin_fn

    # The flag is server-side only; the client must not branch on it.
    assert "logic_gating_enabled" not in APP_JS


def test_allowed_active_logics_is_active_intersected_with_allowed():
    """The selector's option set = ACTIVE logics filtered by the principal's
    allowed set (null entitlements render unfiltered)."""
    fn = _slice(
        "function allowedActiveLogics()", "function isLogicAdmin()"
    )
    assert "allowedLogicSet()" in fn
    assert "activeLogics()" in fn
    assert "allowed.has(l.name)" in fn


# ---------------------------------------------------------------------------
# View selector: options filtered by /auth/me allowed_logics
# ---------------------------------------------------------------------------

def test_view_selector_options_filtered_by_allowed_logics():
    """renderViewSelect renders allowedActiveLogics() (not raw activeLogics)
    and its empty placeholder covers both 'no active logics' and 'none
    accessible'."""
    select = _slice("function renderViewSelect()", "function renderViewTags()")
    assert "allowedActiveLogics()" in select
    assert "No accessible pairing logics" in select
    # The unfiltered active list must not be the option source anymore.
    assert "activeLogics()" not in select.replace("allowedActiveLogics()", "")


def test_saved_and_default_view_validated_against_entitlements():
    """A persisted View (localStorage) and the server default are kept only
    if they name an active logic the principal is ALLOWED."""
    resolve = _slice(
        "async function resolveInitialViewLogic()", "function setViewLogic("
    )
    assert resolve.count("allowedActiveLogics()") >= 2
    # Neither validation may fall back to the unfiltered active list.
    assert "activeLogics()" not in resolve.replace("allowedActiveLogics()", "")


# ---------------------------------------------------------------------------
# Pairing tabs hidden when no logic is accessible
# ---------------------------------------------------------------------------

def test_pairing_tabs_hidden_without_accessible_logic():
    """renderGatedViews hides the three pairing tabs when the /auth/me set
    is known and filters the active list to nothing; a deep link that lands
    on a hidden pairing view settles on Samples."""
    fn = _slice("function renderGatedViews()", "function renderAdminControls()")
    assert "PAIRING_VIEWS.includes(b.dataset.tab)" in fn
    assert 'b.classList.toggle("hidden", gated)' in fn
    # Entitlements unknown (no /auth/me yet) must NOT gate.
    assert "allowedLogicSet() !== null" in fn
    # Deep-link fallback.
    assert 'showView("samples")' in fn


def test_init_runs_the_c5_gates_last():
    """Both C5 renderers run in init, after the view is restored (the
    deep-link fallback needs the final view state)."""
    init_block = _slice("(async function init()", "})();")
    assert "renderGatedViews();" in init_block
    assert "renderAdminControls();" in init_block
    for anchor in ("renderGatedViews();", "renderAdminControls();"):
        assert init_block.index(anchor) > init_block.index("restoreFromHash")


def test_init_loads_entitlements_before_view_resolution():
    """/auth/me must land before the logic list, the saved/default View
    validation, and the gate renderers (they all key off currentUser)."""
    init_block = _slice("(async function init()", "})();")
    assert "await loadCurrentUser();" in init_block
    order = [
        "await loadCurrentUser();",
        "await loadLogics();",
        "resolveInitialViewLogic()",
    ]
    positions = [init_block.index(a) for a in order]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# Admin-only trigger controls hidden unless is_logic_admin
# ---------------------------------------------------------------------------

def test_override_button_rendered_only_for_logic_admins():
    """The Override button in Matches & Overrides (a Layer C write) renders
    only for logic admins."""
    i = APP_JS.index("const overrideBtn =")
    assert "isLogicAdmin()" in APP_JS[i : i + 240]


def test_render_admin_controls_toggles_overrides_wrap():
    """The 'Allow overrides' checkbox wrap is hidden for non-admins (and a
    checked state is reset so a re-render can't leak it)."""
    fn = _slice(
        "function renderAdminControls()", "/* First-visit resolution"
    )
    assert 'isLogicAdmin()' in fn
    assert '$("#m-overrides-wrap")' in fn
    assert 'wrap.classList.toggle("hidden", !admin)' in fn
    assert '$("#m-allow-overrides")' in fn
    assert "cb.checked = false" in fn


def test_overrides_control_wrap_exists_in_html():
    """The HTML carries the id-tagged wrap the renderer toggles."""
    wrap = _slice(
        '<label class="check" id="m-overrides-wrap">',
        "</label>",
        INDEX_HTML,
    )
    assert 'id="m-allow-overrides"' in wrap
    assert "Allow overrides" in wrap


# ---------------------------------------------------------------------------
# Docs: reference-implementation walkthrough + README pointer
# ---------------------------------------------------------------------------

def test_docs_present_reference_implementation_with_adaptation_knob():
    """Option A (Allan 2026-11-16): the org's Keycloak approach is not yet
    decided; the doc says so up front and names LOGIC_CLAIM_NAME /
    LOGIC_ADMIN_CLAIM as the adaptation knob."""
    # The "not yet decided" statement is in the opening blockquote.
    opening = DOCS.split("\n## ", 1)[0]
    assert "not yet decided" in opening.lower()
    assert "reference implementation" in opening
    assert "LOGIC_CLAIM_NAME" in opening
    assert "LOGIC_ADMIN_CLAIM" in opening
    # Default claim names are stated.
    assert "syzygy_logics" in DOCS
    assert "syzygy_admin" in DOCS


def test_docs_cover_dev_compose_keycloak_and_rollout_order():
    """Copy-pasteable walkthrough: dev compose Keycloak, the built-in
    mappers, the env flag, and the rollout order (populate groups -> flip
    flag)."""
    assert "docker-compose.dev.yml" in DOCS
    # Built-in mappers, flat exact-name groups, full-group-path OFF.
    assert "Group Membership" in DOCS
    assert "Full group path" in DOCS
    assert "OFF" in DOCS
    assert "User Attribute" in DOCS
    # Env flag + rollout order: populate the IdP BEFORE flipping the flag.
    assert "LOGIC_GATING_ENABLED" in DOCS
    populate = DOCS.index("Populate the IdP first")
    flip = DOCS.index("Flip the flag")
    assert populate < flip
    # Production guidance (org-managed Keycloak / existing claims).
    assert "Production guidance" in DOCS


def test_readme_auth_section_points_at_the_docs():
    """The README auth section links the new setup doc."""
    auth = README[README.index("## Authentication (Keycloak)") : README.index("## ", README.index("## Authentication (Keycloak)") + 3)]
    assert "docs/logic_gating_setup.md" in auth
