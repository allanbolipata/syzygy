import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from syzygy.database import settings


@pytest.fixture(autouse=True)
def _no_dev_auth_token(monkeypatch):
    """A local .env may set DEV_AUTH_TOKEN (dev-login convenience). If it
    leaks into the import-time ``settings`` singleton, /auth/redirect
    short-circuits to the dev-token path (302, no ``state``) and the auth
    tests fail. Clear it for every test; monkeypatch restores it after."""
    monkeypatch.setattr(settings, "dev_auth_token", "", raising=False)


@pytest.fixture(autouse=True)
def _gating_dark_launch(monkeypatch):
    """A local .env may set LOGIC_GATING_ENABLED=true (gating rollout).
    If it leaks into the import-time ``settings`` singleton, trigger and
    pairing-logic writes 403 "Logic admin required" and every test that
    pairs through the endpoints fails. The dark-launch default is False;
    tests that exercise gating monkeypatch the flag themselves.
    monkeypatch restores the .env value after each test."""
    monkeypatch.setattr(settings, "logic_gating_enabled", False, raising=False)
