import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from database import settings


@pytest.fixture(autouse=True)
def _no_dev_auth_token(monkeypatch):
    """A local .env may set DEV_AUTH_TOKEN (dev-login convenience). If it
    leaks into the import-time ``settings`` singleton, /auth/redirect
    short-circuits to the dev-token path (302, no ``state``) and the auth
    tests fail. Clear it for every test; monkeypatch restores it after."""
    monkeypatch.setattr(settings, "dev_auth_token", "", raising=False)
