"""Keycloak JWT verification.

Verifies tokens issued by the configured Keycloak realm against its JWKS
endpoint. Raises TokenError for anything that is not a valid, current
token for this client.
"""

import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_db, settings
from models import AllowedUser

# Cached JWKS, keyed by realm so each realm is fetched at most once.
_jwks_cache: Dict[str, Dict[str, Any]] = {}


class TokenError(Exception):
    """Raised when a token fails verification."""


def _get_jwks() -> dict:
    """Fetch the realm's JWKS and cache it keyed by realm."""
    realm = settings.keycloak_realm
    if realm not in _jwks_cache:
        url = f"{settings.keycloak_url}/realms/{realm}/protocol/openid-connect/certs"
        response = httpx.get(url)
        response.raise_for_status()
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
    return _jwks_cache[realm]


def verify_token(token: str) -> dict:
    """Decode a Keycloak JWT and return its identity claims.

    Returns {"sub", "email", "preferred_username"} or raises TokenError.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenError(f"malformed token: {exc}") from exc

    if kid is None:
        raise TokenError("token is missing a 'kid' header")

    key = _get_jwks().get(kid)
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
    except jwt.PyJWTError as exc:
        raise TokenError(f"token verification failed: {exc}") from exc

    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "preferred_username": claims.get("preferred_username"),
    }


def _extract_token(request: Request) -> Optional[str]:
    """Read the bearer token from the Authorization header, falling back
    to the ``auth_token`` cookie."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip() or None
    return request.cookies.get("auth_token") or None


def _resolve_user(request: Request, db: Session) -> Tuple[str, Optional[dict]]:
    """Resolve the request's token to an allow-listed user.

    Returns (status, user) where status is:
    - "none"            no token presented at all
    - "unauthenticated" token present but not a valid Keycloak token
    - "forbidden"       valid token, but the email is not in allowed_users
                        or the row is not active
    - "ok"              valid token for an active allowed user; user is
                        {"sub", "email", "preferred_username",
                        "display_name" (from the AllowedUser row)}
    """
    token = _extract_token(request)
    if token is None:
        return ("none", None)
    try:
        claims = verify_token(token)
    except TokenError:
        return ("unauthenticated", None)

    email = (claims.get("email") or "").lower()
    user = db.scalar(
        select(AllowedUser).where(func.lower(AllowedUser.email) == email)
    )
    if user is None or not user.active:
        return ("forbidden", None)
    return (
        "ok",
        {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "preferred_username": claims.get("preferred_username"),
            "display_name": user.display_name,
        },
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> dict:
    """FastAPI dependency: require a valid Keycloak token whose email is
    an active row in allowed_users. Raises 401/403 otherwise."""
    status, user = _resolve_user(request, db)
    if status == "ok" and user is not None:
        return user
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="User not allowed")
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
    """Clear the session cookie and send the browser back to /login."""
    response = RedirectResponse("/login")
    response.delete_cookie("auth_token", path="/", samesite="lax")
    return response


@router.get("/redirect")
def redirect(request: Request) -> RedirectResponse:
    """Kick the browser to the Keycloak authorize endpoint."""
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
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in")

    redirect = RedirectResponse("/ui")
    redirect.set_cookie(
        "auth_token",
        access_token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=expires_in if expires_in is not None else 43200,
    )
    return redirect
