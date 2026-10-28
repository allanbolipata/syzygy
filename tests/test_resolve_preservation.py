"""Unit tests for resolve_preservation (ARGOS/IMPACT preservation gate).

The gate rule: resolve_preservation returns
  * ``"ffpe"`` / ``"frozen"`` — the resolved category;
  * ``""`` — unresolvable (a within-side conflict, or a junk origin
    when preservation is empty); an unresolvable side blocks the pair;
  * ``None`` — not considered (both fields empty); the gate does not
    apply to that side and it never blocks.
"""

from pairing_strategies import resolve_preservation


# ---------------------------------------------------------------------------
# Preservation field decides (present: "FFPE" -> ffpe, anything else frozen)
# ---------------------------------------------------------------------------

def test_preservation_ffpe():
    assert resolve_preservation(["FFPE"], None) == "ffpe"
    assert resolve_preservation(["FFPE"], []) == "ffpe"
    assert resolve_preservation("ffpe", None) == "ffpe"
    assert resolve_preservation("  FfPe ", None) == "ffpe"


def test_preservation_anything_else_is_frozen():
    assert resolve_preservation(["Fresh or Frozen"], None) == "frozen"
    assert resolve_preservation(["Frozen"], None) == "frozen"
    assert resolve_preservation(["FROZEN"], None) == "frozen"
    assert resolve_preservation(["EDTA-Streck"], None) == "frozen"
    assert resolve_preservation("liquid", None) == "frozen"


def test_preservation_list_any_entry_qualifies():
    assert resolve_preservation(["EDTA-Streck", "FFPE"], None) == "ffpe"
    assert resolve_preservation(["FFPE", "Fresh or Frozen"], None) == "ffpe"


# ---------------------------------------------------------------------------
# Sample origin decides only when preservation is empty
# ---------------------------------------------------------------------------

def test_origin_ffpe():
    assert resolve_preservation(None, ["FFPE"]) == "ffpe"
    assert resolve_preservation(None, "ffpe") == "ffpe"
    assert resolve_preservation([], ["ffpe"]) == "ffpe"


def test_origin_frozen():
    assert resolve_preservation(None, ["Fresh or Frozen"]) == "frozen"
    assert resolve_preservation(None, ["FROZEN"]) == "frozen"
    assert resolve_preservation(None, ["frozen"]) == "frozen"


def test_origin_unknown_is_unresolvable():
    assert resolve_preservation(None, ["Plasma"]) == ""
    assert resolve_preservation(None, ["EDTA-Streck"]) == ""
    assert resolve_preservation(None, ["Other"]) == ""
    # An origin mixing both categories is unresolvable.
    assert resolve_preservation(None, ["fresh or frozen", "ffpe"]) == ""


# ---------------------------------------------------------------------------
# Both fields present: preservation decides; origin is free unless it
# names the OPPOSITE category (within-side conflict -> unresolvable)
# ---------------------------------------------------------------------------

def test_both_present_agree():
    assert resolve_preservation(["FFPE"], ["FFPE"]) == "ffpe"
    assert resolve_preservation(["Frozen"], ["Fresh or Frozen"]) == "frozen"
    assert resolve_preservation(["Fresh or Frozen"], ["FROZEN"]) == "frozen"


def test_preservation_ffpe_ignores_junk_origin():
    # A junk origin no longer blocks when preservation is present.
    assert resolve_preservation(["FFPE"], ["Tissue"]) == "ffpe"
    assert resolve_preservation(["FFPE"], ["Plasma"]) == "ffpe"


def test_preservation_frozen_ignores_junk_origin():
    assert resolve_preservation(["Frozen"], ["Buffy Coat"]) == "frozen"
    assert resolve_preservation(["Frozen"], ["Tissue"]) == "frozen"


def test_opposite_category_conflict_is_unresolvable():
    # ffpe-side + origin naming frozen, or frozen-side + origin naming
    # ffpe: within-side conflict -> unresolvable -> gate blocks.
    assert resolve_preservation(["FFPE"], ["Frozen"]) == ""
    assert resolve_preservation(["FFPE"], ["Fresh or Frozen"]) == ""
    assert resolve_preservation(["Frozen"], ["FFPE"]) == ""
    assert resolve_preservation(["Fresh or Frozen"], ["ffpe"]) == ""
    # A mixed origin containing the opposite category also conflicts.
    assert resolve_preservation(["FFPE"], ["FFPE", "Frozen"]) == ""


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------

def test_both_empty_is_not_considered():
    # Not considered is NOT unresolvable: the gate does not apply.
    assert resolve_preservation(None, None) is None
    assert resolve_preservation([], []) is None
    assert resolve_preservation(None, [""]) is None
    assert resolve_preservation([""], []) is None
