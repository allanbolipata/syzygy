"""Unit tests for resolve_preservation (ARGOS/IMPACT preservation gate)."""

from pairing_strategies import resolve_preservation


# ---------------------------------------------------------------------------
# Preservation field (checked first; "FFPE" -> ffpe, anything else frozen)
# ---------------------------------------------------------------------------

def test_preservation_ffpe():
    assert resolve_preservation(["FFPE"], None) == "ffpe"
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
# Sample origin fallback (checked only when preservation is empty)
# ---------------------------------------------------------------------------

def test_origin_ffpe():
    assert resolve_preservation(None, ["FFPE"]) == "ffpe"
    assert resolve_preservation(None, "ffpe") == "ffpe"


def test_origin_frozen():
    assert resolve_preservation(None, ["Fresh or Frozen"]) == "frozen"
    assert resolve_preservation(None, ["FROZEN"]) == "frozen"
    assert resolve_preservation(None, ["frozen"]) == "frozen"


def test_origin_unknown_is_unresolvable():
    assert resolve_preservation(None, ["Plasma"]) == ""
    assert resolve_preservation(None, ["EDTA-Streck"]) == ""
    assert resolve_preservation(None, ["fresh or frozen", "ffpe"]) == ""


# ---------------------------------------------------------------------------
# Both fields present -> categories must agree
# ---------------------------------------------------------------------------

def test_both_present_agree():
    assert resolve_preservation(["FFPE"], ["FFPE"]) == "ffpe"
    assert resolve_preservation(["Frozen"], ["Fresh or Frozen"]) == "frozen"
    assert resolve_preservation(["Fresh or Frozen"], ["FROZEN"]) == "frozen"


def test_both_present_disagree_is_unresolvable():
    assert resolve_preservation(["FFPE"], ["Fresh or Frozen"]) == ""
    assert resolve_preservation(["Fresh or Frozen"], ["FFPE"]) == ""
    assert resolve_preservation(["FFPE"], ["Plasma"]) == ""


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------

def test_both_empty_is_unresolvable():
    assert resolve_preservation(None, None) == ""
    assert resolve_preservation([], []) == ""
    assert resolve_preservation(None, [""]) == ""
