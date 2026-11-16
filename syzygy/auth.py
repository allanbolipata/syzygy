"""Keycloak JWT verification.

Verifies tokens issued by the configured Keycloak realm against its JWKS
endpoint. Raises TokenError for anything that is not a valid, current
token for this client.
"""

import hmac
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from syzygy.database import get_db, settings
from syzygy.models import AllowedUser, PairingLogic

logger = logging.getLogger(__name__)

# Cached JWKS, keyed by realm so each realm is fetched at most once.
_jwks_cache: Dict[str, Dict[str, Any]] = {}
# realm -> time.monotonic() of the most recent JWKS (re)fetch, used to cap
# how often an unknown kid can force a refetch of the certs endpoint.
_jwks_fetched_at: Dict[str, float] = {}
# Minimum seconds between JWKS (re)fetches for a realm.
_JWKS_MIN_REFETCH_INTERVAL = 60.0


class TokenError(Exception):
    """Raised when a token fails verification."""


class TokenExpiredError(TokenError):
    """The token is structurally valid (signature OK) but past its ``exp``.

    Distinguished from the other ``TokenError``s so the auth dependency can
    attempt a silent refresh-token rotation for EXPIRY failures only —
    bad signatures, unknown kids, JWKS fetch failures and malformed tokens
    must fall straight through to the unauthenticated behavior.
    """


def dev_auth_enabled() -> bool:
    """True when the dev-only local login is configured
    (``dev_auth_token`` set). Never true in production."""
    return bool(settings.dev_auth_token)


def _dev_user(db: Session) -> dict:
    """Identity for the dev-only local admin. No Keycloak token and no
    allowed_users row are involved; this path only exists when
    ``dev_auth_token`` is set. The synthetic local identity is an admin:
    full logic set + admin, gating enabled or not."""
    return {
        "sub": "dev-admin",
        "email": settings.dev_auth_email,
        "preferred_username": "dev-admin",
        "display_name": "Dev Admin (local)",
        "allowed_logics": _known_logic_names(db),
        "is_logic_admin": True,
    }


def _fetch_jwks(realm: str) -> dict:
    """Fetch a realm's JWKS from Keycloak, cache it, and stamp the fetch
    time. Raises TokenError if the endpoint is unreachable or returns an
    HTTP error, so a Keycloak blip degrades to 401 (unauthenticated,
    logged by the caller) rather than an unhandled 500.
    """
    url = f"{settings.keycloak_url}/realms/{realm}/protocol/openid-connect/certs"
    try:
        response = httpx.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TokenError(
            f"JWKS fetch failed for realm {realm!r}: {type(exc).__name__}"
        ) from exc
    # Real Keycloak realms expose more than one key (e.g. an
    # RSA-OAEP 'use: enc' key alongside the RS256 'use: sig' key).
    # PyJWK cannot build a signing key from an enc-only entry, so
    # keep only keys that are usable for signature verification.
    jwks = {
        key["kid"]: jwt.PyJWK(key).key
        for key in response.json().get("keys", [])
        if key.get("use", "sig") == "sig"
    }
    _jwks_cache[realm] = jwks
    _jwks_fetched_at[realm] = time.monotonic()
    return jwks


def _get_jwks() -> dict:
    """Return the realm's cached JWKS, fetching it on first use."""
    realm = settings.keycloak_realm
    if realm not in _jwks_cache:
        _fetch_jwks(realm)
    return _jwks_cache[realm]


def _refetch_jwks(realm: str) -> bool:
    """Rate-capped JWKS refetch, used after a kid cache-miss.

    Keycloak may have rotated its signing keys since the cache was
    populated; a fresh fetch is what lets post-rotation tokens verify
    without an app restart. Returns True if a new fetch replaced the
    cache. A too-soon refetch (within ``_JWKS_MIN_REFETCH_INTERVAL`` of
    the last fetch) or a failed fetch returns False and leaves the
    existing cached keys in place.
    """
    last = _jwks_fetched_at.get(realm)
    if last is not None and (time.monotonic() - last) < _JWKS_MIN_REFETCH_INTERVAL:
        return False
    try:
        _fetch_jwks(realm)
    except TokenError:
        return False
    return True


def verify_token(token: str) -> dict:
    """Decode a Keycloak JWT and return its identity claims.

    Returns {"sub", "email", "preferred_username"}, plus the configured
    logic-entitlement claims (``settings.logic_claim_name`` /
    ``settings.logic_admin_claim``) when the token carries them — tokens
    without those claims keep the three-key shape. Raises TokenError.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenError(f"malformed token: {exc}") from exc

    if kid is None:
        raise TokenError("token is missing a 'kid' header")

    key = _get_jwks().get(kid)
    if key is None:
        # kid not in the cached JWKS: Keycloak may have rotated signing
        # keys since the cache was populated. Refetch once (rate-capped)
        # and retry before giving up — otherwise every token minted after
        # a rotation 401s until the process restarts.
        if _refetch_jwks(settings.keycloak_realm):
            key = _jwks_cache[settings.keycloak_realm].get(kid)
    if key is None:
        raise TokenError(f"no JWKS key found for kid {kid!r}")

    issuer = f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
    # No audience check: tokens may be issued to any client in the realm
    # (other org services sharing Keycloak). Trust boundary = issuer +
    # signature (both pinned to this realm) + the allowed_users table.
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        # Expired is a distinct, RECOVERABLE failure: a validly-signed
        # token past its exp can be renewed via the refresh grant, so the
        # auth dependency needs to tell it apart from a hard failure.
        raise TokenExpiredError(f"token expired: {exc}") from exc
    except jwt.PyJWTError as exc:
        raise TokenError(f"token verification failed: {exc}") from exc

    user = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "preferred_username": claims.get("preferred_username"),
    }
    # Pass the entitlement claims through, BY THE CONFIGURED NAMES, so
    # _user_from_claims can derive allowed_logics / is_logic_admin.
    # Tokens without them are left untouched (the three-key shape).
    for claim_name in (settings.logic_claim_name, settings.logic_admin_claim):
        if claim_name in claims:
            user[claim_name] = claims[claim_name]
    return user


def _extract_token(request: Request) -> Optional[str]:
    """Read the bearer token from the Authorization header, falling back
    to the ``auth_token`` cookie."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip() or None
    return request.cookies.get("auth_token") or None


# --- Refresh-token flow -------------------------------------------------

# Session cookie names. The refresh cookie is scoped to Path=/ (NOT the
# /auth prefix suggested in the ticket): the auth dependency runs on the
# /v1/* endpoints, where an /auth-scoped cookie would never be sent, so
# the silent refresh could never see the refresh token. It stays HttpOnly
# like auth_token; only syzygy.auth reads it.
REFRESH_COOKIE_NAME = "refresh_token"
# Lifetime for the refresh cookie when the token response omits
# refresh_expires_in (realm "Refresh Token Max" unset).
_REFRESH_COOKIE_DEFAULT_MAX_AGE = 12 * 60 * 60  # 12 h
# Lifetime for the access-token cookie when the token response omits
# expires_in (previously the literal 43200 in /callback and /auth/redirect).
_ACCESS_COOKIE_DEFAULT_MAX_AGE = 12 * 60 * 60  # 12 h
# Serializes refresh-token POSTs within this process. Concurrent requests
# from the same browser all carry the SAME expired cookies at the moment
# the access token lapses; without the lock they would race the token
# endpoint and, on a realm with refresh-token rotation enabled, a second
# use of the old refresh token would invalidate the session the first
# request just renewed.
_refresh_lock = threading.Lock()


def _set_session_cookies(
    response: Response, token_data: dict, refresh_token: str = ""
) -> None:
    """(Re)set both session cookies from a Keycloak token response.

    ``refresh_token`` is the previously-presented refresh token (empty for
    a fresh login). The response's own ``refresh_token`` wins when present
    (Keycloak may rotate it on refresh grants); otherwise the previous one
    is kept. No refresh cookie is written at all when no refresh token
    exists — a client without refresh-grant support degrades gracefully to
    the old access-token-only session.
    """
    expires_in = token_data.get("expires_in")
    response.set_cookie(
        "auth_token",
        token_data["access_token"],
        httponly=True,
        samesite="lax",
        path="/",
        max_age=expires_in
        if expires_in is not None
        else _ACCESS_COOKIE_DEFAULT_MAX_AGE,
    )
    new_refresh = token_data.get("refresh_token") or refresh_token
    if new_refresh:
        refresh_expires_in = token_data.get("refresh_expires_in")
        response.set_cookie(
            REFRESH_COOKIE_NAME,
            new_refresh,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=(
                refresh_expires_in
                if refresh_expires_in is not None
                else _REFRESH_COOKIE_DEFAULT_MAX_AGE
            ),
        )


def _known_logic_names(db: Session) -> Set[str]:
    """All pairing-logic names in the database — the full entitlement
    set (every logic a principal can be granted)."""
    return set(db.scalars(select(PairingLogic.name)).all())


def _logic_entitlements(claims: dict, db: Session) -> Tuple[Set[str], bool]:
    """(allowed_logics, is_logic_admin) for a verified Keycloak token.

    Claim names come from settings (``logic_claim_name`` /
    ``logic_admin_claim``) so adapting to the org's Keycloak scheme is a
    .env change, not a code change:
    - gating disabled: the FULL set + admin — behavior is exactly
      today's and /auth/me needs no flag knowledge;
    - gating enabled (fail-closed): allowed logics are the configured
      list claim intersected with the known logic names — an absent
      claim yields an EMPTY set, and names the app does not know are
      ignored with a warning. The admin flag is the configured boolean
      claim.
    """
    known = _known_logic_names(db)
    if not settings.logic_gating_enabled:
        return known, True
    raw = claims.get(settings.logic_claim_name)
    if raw is None:
        raw = []
    elif not isinstance(raw, list):
        # A non-list (e.g. a bare string) degrades to a single-element
        # list rather than failing open.
        raw = [raw]
    allowed: Set[str] = set()
    for name in raw:
        if name in known:
            allowed.add(name)
        else:
            logger.warning(
                "auth: ignoring unknown pairing logic %r from %r claim",
                name,
                settings.logic_claim_name,
            )
    is_admin = bool(claims.get(settings.logic_admin_claim))
    return allowed, is_admin


def _user_from_claims(
    claims: dict, db: Session
) -> Tuple[str, Optional[dict], Optional[dict]]:
    """Map verified claims onto the allowed_users table, returning the
    (status, user, claims) triple: "forbidden" when the email has no
    active row, "ok" (with the row's display_name) when it does. Shared by
    the token path and the silent-refresh path so both apply identical
    allowed_users gating."""
    email = (claims.get("email") or "").lower()
    user = db.scalar(
        select(AllowedUser).where(func.lower(AllowedUser.email) == email)
    )
    if user is None or not user.active:
        return ("forbidden", None, claims)
    principal = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "preferred_username": claims.get("preferred_username"),
        "display_name": user.display_name,
    }
    principal["allowed_logics"], principal["is_logic_admin"] = _logic_entitlements(
        claims, db
    )
    return ("ok", principal, claims)


def _attempt_silent_refresh(
    request: Request, response: Optional[Response], db: Session
) -> Optional[Tuple[str, Optional[dict], Optional[dict]]]:
    """Renew an expired session via a Keycloak refresh grant.

    Only called when ``_resolve_user`` reported "unauthenticated". Returns
    a fresh (status, user, claims) triple when the refresh succeeded —
    the caller then proceeds exactly as if the presented token had been
    valid — or None to fall through to the existing unauthenticated
    behavior (401).

    A refresh is attempted ONLY for expiry failures: a token that fails
    signature verification, an unknown kid, or is malformed must NOT hit
    the token endpoint. Missing/empty refresh cookie, missing client
    secret, a rejected grant, and a refreshed token that fails
    verification all return None. On success both cookies are rotated on
    the response (only when FastAPI injected one).
    """
    token = _extract_token(request)
    if token is None:
        return None
    try:
        verify_token(token)
    except TokenExpiredError:
        pass  # expired — the only failure that qualifies for a refresh
    except TokenError:
        return None  # bad signature / kid-miss / malformed: no refresh
    else:
        return None  # token still verifies; nothing to refresh

    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token or not settings.keycloak_client_secret:
        return None

    try:
        with _refresh_lock:
            refresh_response = httpx.post(
                f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
                "/protocol/openid-connect/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.keycloak_client_id,
                    "client_secret": settings.keycloak_client_secret,
                },
            )
            refresh_response.raise_for_status()
            token_data = refresh_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Never log the tokens: the exception type and a scrubbed message
        # only (the token goes in the POST body, not the URL, but scrub
        # defensively in case an error message carries one).
        reason = str(exc)
        for secret in (token, refresh_token):
            if secret:
                reason = reason.replace(secret, "<redacted-token>")
        logger.warning(
            "auth: silent refresh failed: %s: %s", type(exc).__name__, reason
        )
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        logger.warning("auth: silent refresh response missing access_token")
        return None

    # The freshly-issued token must pass the same verification as any
    # presented token before the session is continued as authenticated.
    try:
        claims = verify_token(access_token)
    except TokenError:
        logger.warning("auth: refreshed access token failed verification")
        return None

    if response is not None:
        _set_session_cookies(response, token_data, refresh_token)
    return _user_from_claims(claims, db)


def _resolve_user(request: Request, db: Session) -> Tuple[str, Optional[dict], Optional[dict]]:
    """Resolve the request's token to an allow-listed user.

    Returns (status, user, claims) where status is:
    - "none"            no token presented at all (claims None)
    - "unauthenticated" token present but not a valid Keycloak token
                        (claims None)
    - "forbidden"       valid token, but the email is not in allowed_users
                        or the row is not active (claims included so the
                        caller can explain why)
    - "ok"              valid token for an active allowed user; user is
                        {"sub", "email", "preferred_username",
                         "display_name" (from the AllowedUser row),
                         "allowed_logics" (set[str]),
                         "is_logic_admin" (bool)}
    """
    token = _extract_token(request)
    if token is None:
        return ("none", None, None)
    # Dev-only local login: a configured static token authenticates as
    # the local dev admin without contacting Keycloak or consulting
    # allowed_users. Constant-time compare; a non-matching token falls
    # through to normal Keycloak verification (and fails there).
    if (
        dev_auth_enabled()
        and hmac.compare_digest(
            token.encode("utf-8"), settings.dev_auth_token.encode("utf-8")
        )
    ):
        return ("ok", _dev_user(db), None)
    try:
        claims = verify_token(token)
    except TokenError as exc:
        # Log the failure REASON (expired / signature / JWKS fetch /
        # malformed) so auth failures are diagnosable from `docker logs`.
        # Never log the token itself — scrub it defensively in case an
        # exception message ever carries a fragment of it.
        reason = str(exc)
        if token:
            reason = reason.replace(token, "<redacted-token>")
        logger.warning("auth: token rejected: %s", reason)
        return ("unauthenticated", None, None)

    email = (claims.get("email") or "").lower()
    return _user_from_claims(claims, db)


def get_current_user(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: require a valid Keycloak token whose email is
    an active row in allowed_users. Raises 401/403 otherwise; the 403
    detail explains *why* (e.g. the account has no email address).

    When the presented access token fails verification due to EXPIRY
    (not a bad signature or malformed token) and the browser carries a
    refresh cookie, the session is renewed silently via a Keycloak
    refresh grant and both cookies are rotated on the response before the
    request proceeds as authenticated. Non-expiry failures and refresh
    failures fall through to the existing 401.
    """
    status, user, claims = _resolve_user(request, db)
    if status == "unauthenticated":
        refreshed = _attempt_silent_refresh(request, response, db)
        if refreshed is not None:
            status, user, claims = refreshed
    if status == "ok" and user is not None:
        return user
    if status == "forbidden":
        assert claims is not None  # "forbidden" always carries claims
        email = (claims.get("email") or "").strip()
        username = claims.get("preferred_username") or "unknown"
        if email:
            detail = (
                f"User not allowed: {email} is not in the allow-list"
                " (or is inactive)"
            )
        else:
            detail = (
                f"User not allowed: '{username}' has no email address — "
                "the allow-list is per-email; ask an admin to provision "
                "your account"
            )
        raise HTTPException(status_code=403, detail=detail)
    raise HTTPException(status_code=401, detail="Authentication required")


router = APIRouter(prefix="/auth", tags=["auth"])

# OAuth state -> expiry (time.monotonic), 300 s lifetime.
_login_states: Dict[str, float] = {}
_STATE_LIFETIME = 300.0


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)) -> dict:
    return current_user


@router.get("/logout")
def logout() -> RedirectResponse:
    """Clear the session cookies and send the browser back to /login.

    Both the access-token cookie and the refresh cookie are cleared.
    (Revoking the refresh token on the Keycloak side would require
    admin-credential calls the app is not configured with; left as a
    follow-up — an unrevoked refresh token is useless without this
    server's client secret.)
    """
    response = RedirectResponse("/login")
    response.delete_cookie("auth_token", path="/", samesite="lax")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/", samesite="lax")
    return response


@router.get("/dev-login")
def dev_login() -> dict:
    """Public flag for the login page: whether the Keycloak-less local
    dev login is enabled. Returns only the flag — never the token.
    (Mounted at /auth/dev-login and /v1/auth/dev-login.)"""
    return {"dev_login": dev_auth_enabled()}


@router.get("/redirect")
def redirect(request: Request) -> RedirectResponse:
    """Kick the browser to the Keycloak authorize endpoint."""
    if dev_auth_enabled():
        # Dev-only: skip the Keycloak dance entirely and hand the
        # browser a local dev-admin session directly.
        response = RedirectResponse("/ui")
        response.set_cookie(
            "auth_token",
            settings.dev_auth_token,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=43200,
        )
        return response
    now = time.monotonic()
    for state in [s for s, exp in _login_states.items() if exp <= now]:
        del _login_states[state]
    state = uuid.uuid4().hex
    _login_states[state] = now + _STATE_LIFETIME

    query = urlencode(
        {
            "client_id": settings.keycloak_client_id,
            "redirect_uri": f"{request.base_url}auth/callback",
            "response_type": "code",
            "scope": "openid",
            "state": state,
        }
    )
    url = (
        f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        f"/protocol/openid-connect/auth?{query}"
    )
    return RedirectResponse(url)


@router.get("/callback")
def callback(request: Request, code: str, state: str) -> RedirectResponse:
    """Exchange the authorization code for a token, hand the browser an
    HttpOnly session cookie, and land it on the UI."""
    now = time.monotonic()
    for st in [s for s, exp in _login_states.items() if exp <= now]:
        del _login_states[st]
    if state not in _login_states:
        raise HTTPException(status_code=400, detail="Invalid login state")
    del _login_states[state]

    url = (
        f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        f"/protocol/openid-connect/token"
    )
    response = httpx.post(
        url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{request.base_url}auth/callback",
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret,
        },
    )
    if not response.is_success:
        raise HTTPException(
            status_code=502, detail="Keycloak token exchange failed"
        )

    token_data = response.json()
    redirect = RedirectResponse("/ui")
    # auth_token cookie as before, plus the refresh_token cookie when the
    # realm/client issued one (no refresh cookie = old behavior, session
    # simply ends at the access-token lifetime).
    _set_session_cookies(redirect, token_data)
    return redirect
