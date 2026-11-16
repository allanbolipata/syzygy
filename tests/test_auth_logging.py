"""Tests that ``auth._resolve_user`` logs the ``TokenError`` REASON at
WARNING (auth failures must be diagnosable from ``docker logs``) while
never including the token itself in any log line.

``verify_token`` is monkeypatched to raise each TokenError class; no real
Keycloak or database is involved (the token path returns before the DB is
consulted when verification fails).
"""

import logging

import pytest

import syzygy.auth as auth

# A fake bearer token (three dot-separated segments, JWT-shaped). It is
# never a real token and must NEVER appear in any log line.
TOKEN = "eyJfa2lk.x-fake-payload.signature"


class _Req:
    """Just enough of a starlette Request for ``auth._extract_token``."""

    headers = {"Authorization": f"Bearer {TOKEN}"}
    cookies = {}


@pytest.mark.parametrize(
    "reason",
    [
        "token verification failed: Signature has expired",  # expired
        "token verification failed: Signature verification failed",  # signature
        "JWKS fetch failed for realm 'r': ConnectError",  # JWKS fetch
        "no JWKS key found for kid 'rotated-key'",  # JWKS kid-miss
        "malformed token: Invalid header string",  # malformed
    ],
    ids=["expired", "signature", "jwks-fetch", "jwks-kid-miss", "malformed"],
)
def test_resolve_user_logs_reason_not_token(caplog, monkeypatch, reason):
    def fake_verify(token):
        raise auth.TokenError(reason)

    monkeypatch.setattr(auth, "verify_token", fake_verify)

    with caplog.at_level(logging.WARNING):
        status, user, claims = auth._resolve_user(_Req(), db=None)

    # Still unauthenticated, and no claims leaked to the caller.
    assert status == "unauthenticated"
    assert user is None and claims is None

    warnings = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ]
    # A WARNING carrying the failure reason was emitted...
    assert any(reason in w for w in warnings), warnings
    # ...and the token itself appears in NO log line.
    assert not any(TOKEN in w for w in warnings), warnings


def test_scrub_redacts_token_embedded_in_reason(caplog, monkeypatch):
    # Belt-and-suspenders: even if a TokenError message somehow carried the
    # token, it must be scrubbed out before it reaches the log.
    def fake_verify(token):
        raise auth.TokenError(f"bad token: {token}")

    monkeypatch.setattr(auth, "verify_token", fake_verify)
    with caplog.at_level(logging.WARNING):
        status, user, claims = auth._resolve_user(_Req(), db=None)

    assert status == "unauthenticated"
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("<redacted-token>" in w for w in warnings), warnings
    assert not any(TOKEN in w for w in warnings), warnings
