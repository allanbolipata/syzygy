#!/usr/bin/env python3
"""Keycloak setup automation for syzygy pairing-logic gating (ticket P5).

Creates, idempotently, via the Keycloak ADMIN API only (no console
clicking, no scripting feature):

- the app's `syzygy` client itself, when missing (confidential,
  standard flow, the dev redirect URI / web origin — the README's
  manual one-time client step, folded into the script so a fresh dev
  box needs one command; on an org realm the client is expected to
  already exist and is only verified),
- the flat, exact-case realm groups (one per pairing logic),
- the gating mappers on the app client's DEDICATED client scope: one
  built-in Group Membership mapper per logic group (all feeding the
  ``syzygy_logics`` list claim) + the ``syzygy_admin`` user-attribute
  mapper,
- the managed ``syzygy_admin`` user-profile attribute plus the
  "Admin can edit" unmanaged-attributes policy (Keycloak >= 26 hides
  user attributes behind the User Profile feature),
- with ``--personas`` (DEV COMPOSE ONLY): the dev persona users with
  passwords and group memberships, and finally each persona's DECODED
  access-token claims printed to stdout (entitlements visible without
  DevTools).

Idempotent: safe to re-run; a second run reports "unchanged" for
everything. Never deletes anything the script did not create itself —
the one exception is REPAIR of mappers this script's first release
left on the dedicated scope (a mapper stored with protocol
``openid`` is silently skipped at token issuance on Keycloak 26.8.1,
and a PUT cannot change an existing mapper's protocol, so such
mappers are deleted and recreated).

Credentials: a realm-admin on the target realm is sufficient (never a
master-realm admin). The admin token is minted through the public
``admin-cli`` client — in the target realm first, then in the master
realm (the standard dedicated-realm shape: the admin account lives in
master and holds realm-management roles in the target realm).

User creation is the one part that must never touch a shared/federated
realm: ``--personas`` is REFUSED unless ``--allow-user-creation`` is
set explicitly.

The group/mapper/claim constants below are the SINGLE SOURCE OF TRUTH:
``--emit-handoff`` renders the identity-team one-pager (P5b) from them,
so docs/keycloak_gating_handoff.md is never hand-edited.

Usage (dev compose stack, realm master, admin/admin):
    .venv/bin/python scripts/keycloak_gating_setup.py \\
        --realm master --username admin --password admin \\
        --personas --allow-user-creation

Flags fall back to the environment (KEYCLOAK_URL, KEYCLOAK_REALM,
KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD, KEYCLOAK_CLIENT_ID,
KEYCLOAK_CLIENT_SECRET) and then to the repo .env, so a bare invocation
from the repo root works against the dev stack.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Single source of truth (P5a writes AND the P5b handoff one-pager are
# both derived from these constants).
# ---------------------------------------------------------------------------

#: Top-level realm groups, named EXACTLY (case-sensitive) like the
#: pairing-logic names in the app's pairing_logics table. Flat, no
#: nesting: the Group Membership mapper runs with "full group path"
#: OFF, so a path like /syzygy/logic/ARGOS would never match.
GROUP_NAMES: Tuple[str, ...] = ("ARGOS", "DMP", "HARMONY", "ACCESS", "TEMPO")

#: Access-token claim holding the JSON list of exact logic names.
LOGIC_CLAIM = "syzygy_logics"
#: Access-token claim holding the truthy logic-admin flag.
ADMIN_CLAIM = "syzygy_admin"
#: App env vars that name those claims (integration option b in the
#: handoff one-pager).
APP_CLAIM_ENV: Tuple[str, str] = ("LOGIC_CLAIM_NAME", "LOGIC_ADMIN_CLAIM")

#: The protocol every gating mapper must be stored under: the DEDICATED
#: scope's protocol. On Keycloak 26.8.1 a mapper stored with any other
#: protocol (notably ``openid``) is SILENTLY SKIPPED at token issuance
#: (verified live: its claims never appear), and a PUT cannot change an
#: existing mapper's protocol (the server reverts it) — repair is
#: delete + recreate.
MAPPER_PROTOCOL = "openid-connect"

#: Legacy name of the single group mapper this script's FIRST release
#: created (no claim.value -> inert on 26.8.1). ensure_mappers removes
#: it when present; the per-group family below replaces it.
LEGACY_GROUP_MAPPER_NAME = "syzygy logics"


def group_mapper_spec(group_name: str) -> Dict[str, Any]:
    """Spec for the built-in "Group Membership"
    (oidc-group-membership-mapper) mapper of ONE logic group.

    ``claim.value`` must name the group — a group mapper without one is
    a no-op on 26.8.1 — and ``full.path`` stays OFF: the groups are
    flat, so the exact name is the whole path. Every mapper of the
    family feeds the same ``syzygy_logics`` claim: when a user is a
    member of the mapper's group, Keycloak adds the user's group names
    to the claim, so across the family the claim accumulates into a
    JSON list of all of the user's logic-group memberships (verified
    live on 26.8.1).
    """
    return {
        "name": f"syzygy logics: {group_name}",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {
            "claim.name": LOGIC_CLAIM,
            "claim.value": group_name,
            "full.path": "false",
            "id.token.claim": "false",
            "access.token.claim": "true",
            "userinfo.token.claim": "false",
        },
    }


def group_mapper_specs() -> List[Dict[str, Any]]:
    """One group-membership mapper spec per logic group (full family)."""
    return [group_mapper_spec(name) for name in GROUP_NAMES]


#: The "User Attribute" (oidc-usermodel-attribute-mapper) mapper on the
#: same dedicated scope — the truthy logic-admin flag claim.
ADMIN_MAPPER: Dict[str, Any] = {
    "name": "syzygy admin",
    "protocolMapper": "oidc-usermodel-attribute-mapper",
    "config": {
        "user.attribute": ADMIN_CLAIM,
        "claim.name": ADMIN_CLAIM,
        "claim.value.type": "Boolean",
        "id.token.claim": "false",
        "access.token.claim": "true",
        "userinfo.token.claim": "false",
    },
}

#: User-profile declaration: ``syzygy_admin`` is a managed attribute
#: (admin view/edit) — the Keycloak >= 26-replacement for poking the
#: hidden Attributes tab. The dict holds ONLY the fields the REST
#: model round-trips (name, displayName, permissions, multivalued) so
#: a second run compares equal and reports "unchanged"; omitted
#: fields take their defaults (not required, editable).
MANAGED_ATTRIBUTE: Dict[str, Any] = {
    "name": ADMIN_CLAIM,
    "displayName": "Syzygy logic admin",
    "multivalued": False,
    "permissions": {"view": ["admin"], "edit": ["admin"]},
}
#: Realm attribute holding the user-profile unmanaged-attributes policy
#: (console: *Realm Settings* -> *User Profile* -> *Unmanaged
#: attributes* dropdown). In Keycloak 26 this is a REALM ATTRIBUTE, not
#: part of the PUT /users/profile representation.
#: Values: DISABLED, ADMIN, USER, USER_EDIT_VIEW. In 26.8.1 the value is
#: a plain STRING in the realm representation (an array value makes
#: PUT /admin/realms/{realm} fail with "Cannot parse the JSON").
UNMANAGED_POLICY_ATTR = "kc.user.profile.config.unmanagedAttributePolicy"
#: Realm user-profile unmanaged-attributes policy: keep every other
#: attribute admin-editable (the console equivalent is "Admin can edit").
UNMANAGED_ATTRIBUTE_POLICY = "ADMIN"

#: Dev personas (``--personas``; DEV COMPOSE ONLY — never against a
#: shared/federated realm; see the --allow-user-creation guard).
#: Mirrors the rollout UAT: an ARGOS-only user, a DMP-only user, and a
#: logic admin (all groups + the admin attribute).
PERSONAS: Tuple[Dict[str, Any], ...] = (
    {
        "username": "argos-only",
        "email": "argos-only@syzygy.dev",
        "password": "argos-only-dev-2026",
        "groups": ("ARGOS",),
        "admin": False,
    },
    {
        "username": "dmp-only",
        "email": "dmp-only@syzygy.dev",
        "password": "dmp-only-dev-2026",
        "groups": ("DMP",),
        "admin": False,
    },
    {
        "username": "logic-admin",
        "email": "logic-admin@syzygy.dev",
        "password": "logic-admin-dev-2026",
        "groups": GROUP_NAMES,
        "admin": True,
    },
)


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested without a live Keycloak)
# ---------------------------------------------------------------------------


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload segment WITHOUT verifying the signature.

    For display only (the "no DevTools" claims print); never use this
    to make a trust decision.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))


def _env_file_value(name: str) -> str:
    """Read a KEY=*** value from the repo-root .env (empty when absent)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_file = os.path.join(repo_root, ".env")
    if not os.path.exists(env_file):
        return ""
    with open(env_file, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def resolve_setting(flag_value: Optional[str], env_name: str, default: str = "") -> str:
    """flag > environment > .env > default (for the non-secret settings)."""
    if flag_value is not None:
        return flag_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    dotted = _env_file_value(env_name)
    return dotted or default


def resolve_client_secret(flag_value: Optional[str]) -> str:
    """--client-secret *** env KEYCLOAK_CLIENT_SECRET *** repo .env > empty."""
    if flag_value is not None:
        return flag_value
    if os.environ.get("KEYCLOAK_CLIENT_SECRET"):
        return os.environ["KEYCLOAK_CLIENT_SECRET"]
    return _env_file_value("KEYCLOAK_CLIENT_SECRET")


# ---------------------------------------------------------------------------
# Keycloak admin API
# ---------------------------------------------------------------------------


class SetupError(Exception):
    """Fatal, human-readable setup failure."""


class KcAdmin:
    """Thin Keycloak admin-REST client scoped to ONE realm."""

    def __init__(self, base_url: str, realm: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.http = httpx.Client(
            base_url=f"{self.base_url}/admin/realms/{realm}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params or None)
        return self._check(r)

    def put(self, path: str, body: Any) -> Any:
        r = self.http.put(path, json=body)
        return self._check(r)

    def post(self, path: str, body: Any = None) -> Any:
        r = self.http.post(path, json=body)
        return self._check(r)

    def delete(self, path: str) -> Any:
        r = self.http.delete(path)
        return self._check(r)

    def _check(self, r: httpx.Response) -> Any:
        if r.status_code == 401:
            raise SetupError(
                f"admin token rejected (401) for realm {self.realm!r} — "
                "check the credentials: they must be a realm-admin of "
                f"{self.realm!r}"
            )
        if not (200 <= r.status_code < 300):
            raise SetupError(f"Keycloak admin API error: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def close(self) -> None:
        self.http.close()


def mint_admin_token(
    base_url: str,
    realm: str,
    username: str,
    password: str,
    token_client: Optional[str] = None,
) -> str:
    """Mint an admin token for a realm-admin user.

    Tries the public ``admin-cli`` client: in the target realm first,
    then in the master realm (the standard dedicated-realm shape — the
    admin account lives in master and holds realm-management roles in
    the target realm). ``token_client`` pins one client explicitly.
    """
    candidates: List[Tuple[str, str]]
    if token_client:
        candidates = [(realm, token_client)]
    else:
        candidates = [(realm, "admin-cli"), ("master", "admin-cli")]
    last_error = ""
    for cand_realm, client in candidates:
        try:
            r = httpx.post(
                f"{base_url.rstrip('/')}/realms/{cand_realm}"
                "/protocol/openid-connect/token",
                data={
                    "client_id": client,
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                },
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            last_error = f"{client}@{cand_realm}: {type(exc).__name__}"
            continue
        if r.status_code == 200:
            return r.json()["access_token"]
        last_error = f"{client}@{cand_realm}: HTTP {r.status_code} {r.text[:200]}"
    raise SetupError(f"could not mint an admin token (tried {candidates}): {last_error}")


def get_client(admin: KcAdmin, client_id: str) -> Optional[dict]:
    clients = admin.get("/clients", clientId=client_id)
    if len(clients) > 1:
        raise SetupError(f"expected at most one client with client_id={client_id!r}, found {len(clients)}")
    return clients[0] if clients else None


#: Dev client shape (README, "Authentication (Keycloak)"): confidential,
#: standard flow, the host-side dev app's callback + web origin.
CLIENT_REDIRECT_URIS = ["http://localhost:8000/auth/callback"]
CLIENT_WEB_ORIGINS = ["http://localhost:8000"]


def ensure_client(admin: KcAdmin, client_id: str, client_secret: str) -> Tuple[dict, List[str]]:
    """Ensure the app's client exists with the dev shape (idempotent).

    Creates it when missing (fresh dev compose realm) using the
    provided/known client secret when one is set, otherwise a
    Keycloak-generated one (printed, for .env). When it exists, only
    the missing shape fields are filled in and a secret MISMATCH is
    warned about — the existing secret is never overwritten.
    Returns (client, changes).
    """
    client = get_client(admin, client_id)
    changes: List[str] = []
    if client is None:
        body: Dict[str, Any] = {
            "clientId": client_id,
            "name": client_id,
            "enabled": True,
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "standardFlowEnabled": True,
            "redirectUris": list(CLIENT_REDIRECT_URIS),
            "webOrigins": list(CLIENT_WEB_ORIGINS),
            # Needed for the persona token print at the end.
            "directAccessGrantsEnabled": True,
        }
        if client_secret:
            body["secret"] = client_secret
        # Keycloak answers 201 with an EMPTY body — fetch the client back.
        admin.post("/clients", body)
        client = get_client(admin, client_id)
        if client is None:
            raise SetupError(f"client {client_id!r} was created but cannot be read back")
        changes.append(f"client {client_id!r} created (confidential, standard flow, dev redirect URI)")
        if not client_secret:
            generated = admin.get(f"/clients/{client['id']}/client-secret") or {}
            if generated.get("value"):
                changes.append(
                    f"GENERATED client secret — set KEYCLOAK_CLIENT_SECRET in .env to {generated['value']!r}"
                )
        return client, changes

    shape = {
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": True,
    }
    for key, value in shape.items():
        if client.get(key) is not value:
            client[key] = value
            changes.append(f"client {client_id!r} field {key!r} set")
    for key, desired in (("redirectUris", CLIENT_REDIRECT_URIS), ("webOrigins", CLIENT_WEB_ORIGINS)):
        have = set(client.get(key) or [])
        missing = [v for v in desired if v not in have]
        if missing:
            client[key] = list(have) + missing
            changes.append(f"client {client_id!r} {key} += {missing!r}")
    if changes:
        admin.put(f"/clients/{client['id']}", client)

    # Warn (never overwrite) when the caller's known secret mismatches.
    if client_secret:
        stored = admin.get(f"/clients/{client['id']}/client-secret") or {}
        if stored.get("value") and stored["value"] != client_secret:
            changes.append(
                f"WARNING: client {client_id!r} secret does not match the provided "
                "KEYCLOAK_CLIENT_SECRET — persona token verification (and the app "
                "itself) will fail to authenticate; fix the .env value"
            )
    return client, changes


def ensure_groups(admin: KcAdmin) -> Tuple[Dict[str, str], List[str]]:
    """Create the flat exact-case logic groups; returns (name->id, changed).

    GET /groups has no name filter — the full (top-level) list is
    fetched and filtered by path ("/<name>" = top-level, flat layout).
    """
    existing = admin.get("/groups", max=-1)
    group_ids = {
        g["name"]: g["id"]
        for g in existing
        if g.get("path") == "/" + g["name"]
    }
    changed: List[str] = []
    for name in GROUP_NAMES:
        if name in group_ids:
            continue
        admin.post("/groups", {"name": name})  # 201 with an EMPTY body
        created = [
            g for g in admin.get("/groups", max=-1)
            if g.get("path") == "/" + name
        ]
        if len(created) != 1:
            raise SetupError(f"group {name!r} was created but cannot be read back")
        group_ids[name] = created[0]["id"]
        changed.append(name)
    return group_ids, changed


def ensure_user_profile(admin: KcAdmin) -> List[str]:
    """Declare ``syzygy_admin`` as a managed user-profile attribute and
    set the unmanaged-attributes policy to Admin-can-edit (idempotent).

    The policy is a realm ATTRIBUTE in Keycloak 26 (the console's
    Realm Settings -> User Profile -> "Unmanaged attributes" control
    writes it), NOT a field of PUT /users/profile — sending it there
    fails with "Cannot parse the JSON". Returns a list of
    human-readable changes.
    """
    changes: List[str] = []

    # 1) Managed attribute (PUT /users/profile — attributes only).
    profile = admin.get("/users/profile")
    attributes: List[dict] = list(profile.get("attributes") or [])
    attr_changed = False
    existing_attr = next((a for a in attributes if a.get("name") == ADMIN_CLAIM), None)
    if existing_attr is None:
        attributes.append(json.loads(json.dumps(MANAGED_ATTRIBUTE)))
        changes.append(f"managed attribute {ADMIN_CLAIM!r} declared")
        attr_changed = True
    else:
        for key, value in MANAGED_ATTRIBUTE.items():
            if existing_attr.get(key) != value:
                existing_attr[key] = value
                changes.append(f"managed attribute {ADMIN_CLAIM!r} field {key!r} updated")
                attr_changed = True
    if attr_changed:
        body: Dict[str, Any] = {"attributes": attributes}
        # Omitted keys are RESET server-side (an empty body wipes the
        # whole config) — carry existing groups across untouched.
        if profile.get("groups") is not None:
            body["groups"] = profile["groups"]
        admin.put("/users/profile", body)

    # 2) Unmanaged-attributes policy (realm attribute).
    realm = admin.get("/")
    realm_attributes: Dict[str, Any] = dict(realm.get("attributes") or {})
    raw = realm_attributes.get(UNMANAGED_POLICY_ATTR)
    current = raw[0] if isinstance(raw, (list, tuple)) else raw
    if current != UNMANAGED_ATTRIBUTE_POLICY:
        # Match the shape the server round-trips: KC 26.8.1 realm
        # attribute values are plain STRINGS in the realm
        # representation — an ARRAY value is rejected on
        # PUT /admin/realms/{realm} with "Cannot parse the JSON".
        listish = any(isinstance(v, (list, tuple))
                      for v in realm_attributes.values())
        realm_attributes[UNMANAGED_POLICY_ATTR] = (
            [UNMANAGED_ATTRIBUTE_POLICY] if listish else UNMANAGED_ATTRIBUTE_POLICY
        )
        realm = dict(realm)
        realm["attributes"] = realm_attributes
        admin.put("/", realm)
        changes.append(
            f"unmanaged attribute policy {current!r} -> {UNMANAGED_ATTRIBUTE_POLICY!r}"
        )
    return changes


def ensure_dedicated_scope(admin: KcAdmin, client: dict) -> Tuple[dict, List[str]]:
    """Ensure the client's DEDICATED client scope (``<client>-dedicated``)
    exists and is attached as a default scope of the client (idempotent).

    Mappers on a client scope only ride in tokens when the scope is
    active for the client — so attachment is part of the setup.
    Returns (scope, changes).
    """
    client_id = client["clientId"]
    name = f"{client_id}-dedicated"

    def _by_name() -> List[dict]:
        # In KC 26.8.1 GET /client-scopes ignores the `name` param —
        # fetch the full list and filter by exact name (same quirk as
        # GET /groups in ensure_groups).
        return [s for s in admin.get("/client-scopes") if s["name"] == name]

    matches = _by_name()
    if len(matches) > 1:
        raise SetupError(f"expected at most one client scope named {name!r}, found {len(matches)}")
    changes: List[str] = []
    if not matches:
        # Keycloak answers 201 with an EMPTY body — re-query by name.
        # KC 26.x client scopes carry a REQUIRED `protocol` field
        # (valid: an installed LoginProtocolFactory id — here
        # 'openid-connect' or 'saml'; the server round-trips both on
        # the pre-existing scopes). A missing/unknown value is
        # rejected with 400 "Unexpected protocol".
        admin.post(
            "/client-scopes",
            {
                "name": name,
                "protocol": "openid-connect",
                "description": (
                    f"Dedicated scope for the {client_id!r} client — "
                    "syzygy pairing-logic gating mappers (managed by "
                    "scripts/keycloak_gating_setup.py)"
                ),
                "consentRequired": False,
                "displayOnConsentScreen": False,
            },
        )
        matches = _by_name()
        if len(matches) != 1:
            raise SetupError(f"client scope {name!r} was created but cannot be read back")
        changes.append(f"client scope {name!r} created")
    scope = matches[0]
    # client.defaultClientScopes: in KC 26 this is a list of scope
    # NAMES (verified live: the built-ins appear as 'profile', 'roles',
    # ... — not UUIDs); accept either shape so the check is right in
    # both worlds.
    defaults = set(client.get("defaultClientScopes") or [])
    if scope["id"] not in defaults and scope["name"] not in defaults:
        # Keycloak admin REST: PUT /clients/{id}/default-client-scopes/{scopeId}
        # (a POST /default-scopes/... path does not exist -> 404).
        admin.put(f"/clients/{client['id']}/default-client-scopes/{scope['id']}", None)
        changes.append(f"client scope {name!r} attached to client {client_id!r} (default scope)")
    return scope, changes


def _config_matches(current_config: dict, spec_config: dict) -> bool:
    """True when every spec config key is present in ``current_config``
    with the same value. Extra keys the server normalizes in on write
    (e.g. ``introspection.token.claim``) are ignored — a second run
    must report "unchanged", not rewrite the mapper.
    """
    return all(current_config.get(k) == v for k, v in spec_config.items())


def ensure_mappers(admin: KcAdmin, scope: dict) -> List[str]:
    """Ensure the gating mappers on the client's DEDICATED scope
    (idempotent): the per-logic-group Group Membership family (all
    feeding the ``syzygy_logics`` list claim) + the ``syzygy_admin``
    user-attribute mapper. Returns a list of human-readable changes.

    Every mapper is stored under ``MAPPER_PROTOCOL`` (the scope's
    protocol). On Keycloak 26.8.1 a mapper stored under any other
    protocol is silently skipped at token issuance, and a PUT cannot
    change an existing mapper's protocol (the server reverts it) — so
    a wrong-protocol or drifted mapper is REPAIRED by delete +
    recreate. The pre-v2 single group mapper this script's first
    release created (no claim.value -> inert) is removed when present;
    nothing else on the scope is ever touched.
    """
    existing = admin.get(f"/client-scopes/{scope['id']}/protocol-mappers/models")
    by_name = {m["name"]: m for m in existing}
    changes: List[str] = []

    def _create(spec: Dict[str, Any]) -> None:
        admin.post(
            f"/client-scopes/{scope['id']}/protocol-mappers/models",
            {
                "name": spec["name"],
                "protocol": MAPPER_PROTOCOL,
                "protocolMapper": spec["protocolMapper"],
                "config": dict(spec["config"]),
            },
        )

    def _replace(current: dict, spec: Dict[str, Any], why: str) -> None:
        admin.delete(
            f"/client-scopes/{scope['id']}/protocol-mappers/models/{current['id']}"
        )
        _create(spec)
        changes.append(f"mapper {spec['name']!r} recreated ({why})")

    for spec in group_mapper_specs() + [ADMIN_MAPPER]:
        current = by_name.get(spec["name"])
        if current is None:
            _create(spec)
            changes.append(f"mapper {spec['name']!r} created")
        elif current.get("protocol") != MAPPER_PROTOCOL:
            _replace(
                current, spec,
                f"protocol {current.get('protocol')!r} -> {MAPPER_PROTOCOL!r}",
            )
        elif not _config_matches(current.get("config") or {}, spec["config"]):
            _replace(current, spec, "config drift")
        # else: unchanged

    # Legacy single group mapper from this script's first release
    # (no claim.value -> no-op): replaced by the per-group family.
    legacy = by_name.get(LEGACY_GROUP_MAPPER_NAME)
    if legacy is not None:
        admin.delete(
            f"/client-scopes/{scope['id']}/protocol-mappers/models/{legacy['id']}"
        )
        changes.append(
            f"legacy single {LEGACY_GROUP_MAPPER_NAME!r} mapper removed "
            "(replaced by the per-group family)"
        )
    return changes


def _find_user(admin: KcAdmin, email: str) -> Optional[dict]:
    users = admin.get("/users", email=email, exact="true")
    return users[0] if users else None


def ensure_personas(admin: KcAdmin, group_ids: Dict[str, str]) -> List[str]:
    """Create the dev persona users (passwords, groups, admin attribute)
    idempotently. Returns a list of human-readable changes."""
    changes: List[str] = []
    for persona in PERSONAS:
        user = _find_user(admin, persona["email"])
        created = user is None
        if created:
            admin.post(
                "/users",
                {
                    "username": persona["username"],
                    "email": persona["email"],
                    "enabled": True,
                    "emailVerified": True,
                    "firstName": persona["username"],
                    "lastName": "Persona",
                },
            )
            user = _find_user(admin, persona["email"])
            if user is None:
                raise SetupError(
                    f"user {persona['username']!r} was created but cannot be read back"
                )
            admin.put(
                f"/users/{user['id']}/reset-password",
                {"type": "password", "value": persona["password"]},
            )
            changes.append(f"user {persona['username']!r} created")

        assert user is not None  # created above when _find_user missed
        user_id = user["id"]
        # Group membership (idempotent; PUT is a no-op when already in).
        have = {g["name"] for g in user.get("groups") or []}
        missing = [g for g in persona["groups"] if g not in have]
        for gname in missing:
            if gname not in group_ids:
                raise SetupError(f"group {gname!r} missing while assigning {persona['username']!r}")
            admin.put(f"/users/{user_id}/groups/{group_ids[gname]}", None)
            changes.append(f"user {persona['username']!r} -> group {gname!r}")

        # Admin attribute (only on the admin persona; removed if stale).
        fresh = admin.get(f"/users/{user_id}")
        attributes = dict(fresh.get("attributes") or {})
        want = [ADMIN_CLAIM] if persona["admin"] else []
        have_admin = ADMIN_CLAIM in attributes
        if persona["admin"] and not have_admin:
            attributes[ADMIN_CLAIM] = ["true"]
            fresh = dict(fresh)
            fresh["attributes"] = attributes
            admin.put(f"/users/{user_id}", fresh)
            changes.append(f"user {persona['username']!r} attribute {ADMIN_CLAIM}=true set")
        elif not persona["admin"] and have_admin:
            del attributes[ADMIN_CLAIM]
            fresh = dict(fresh)
            fresh["attributes"] = attributes
            admin.put(f"/users/{user_id}", fresh)
            changes.append(f"user {persona['username']!r} stale {ADMIN_CLAIM} attribute removed")
    return changes


def persona_access_token(
    base_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
) -> str:
    """Mint one persona's access token via the password grant."""
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/token",
            data=data,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SetupError(f"token endpoint unreachable for {username!r}: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise SetupError(
            f"password grant failed for {username!r}: HTTP {r.status_code} {r.text[:200]}"
        )
    return r.json()["access_token"]


def print_persona_claims(
    base_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
) -> None:
    """Print each persona's decoded access-token claims (the
    entitlements, no DevTools needed)."""
    print("\n== Decoded access-token claims (dev personas) ==")
    for persona in PERSONAS:
        token = persona_access_token(
            base_url, realm, client_id, client_secret,
            persona["username"], persona["password"],
        )
        claims = decode_jwt_payload(token)
        print(f"\n{persona['username']} ({persona['email']})")
        for key in ("preferred_username", "email", LOGIC_CLAIM, ADMIN_CLAIM):
            value = claims.get(key)
            shown = json.dumps(value) if value is not None else "(absent)"
            print(f"  {key}: {shown}")


# ---------------------------------------------------------------------------
# P5b — identity-team handoff one-pager (rendered from the same constants)
# ---------------------------------------------------------------------------


def render_handoff() -> str:
    """Render the identity-team handoff one-pager (P5b).

    EVERY group name, mapper field, and claim shape below is read from
    the module constants above — the same values the setup script writes
    to Keycloak — so this document and the script cannot drift apart.
    """
    groups_rows = "\n".join(f"| `{name}` | `{name}` |" for name in GROUP_NAMES)
    group_specs = group_mapper_specs()
    shared_group_rows = "\n".join(
        f"| `{k}` | `{v}` |"
        for k, v in group_specs[0]["config"].items()
        if k != "claim.value"
    )
    per_group_rows = "\n".join(
        f"| `{spec['name']}` | `{spec['config']['claim.value']}` |"
        for spec in group_specs
    )
    admin_rows = "\n".join(
        f"| `{k}` | `{v}` |" for k, v in ADMIN_MAPPER["config"].items()
    )
    mapper_sections = (
        "### Group mappers — one per logic group\n\n"
        f"- Protocol mapper: `{group_specs[0]['protocolMapper']}`\n"
        "- Placement: the `syzygy` client's **dedicated client scope**\n"
        f"- Protocol: `{MAPPER_PROTOCOL}`\n\n"
        "All group mappers share this config — the claim values "
        "accumulate into the single `syzygy_logics` JSON list claim, so "
        "a user in any of the groups gets the list of ALL their "
        "logic-group memberships:\n\n"
        "| Config key | Value |\n|---|---|\n"
        f"{shared_group_rows}\n\n"
        "plus `claim.value` set to the exact group name — one mapper per "
        "group:\n\n"
        "| Mapper | `claim.value` |\n|---|---|\n"
        f"{per_group_rows}\n\n"
        f"### Admin mapper: `{ADMIN_MAPPER['name']}`\n\n"
        f"- Protocol mapper: `{ADMIN_MAPPER['protocolMapper']}`\n"
        "- Placement: the `syzygy` client's **dedicated client scope**\n"
        f"- Protocol: `{MAPPER_PROTOCOL}`\n\n"
        "| Config key | Value |\n|---|---|\n"
        f"{admin_rows}\n\n"
    )
    return f"""# Syzygy pairing-logic gating — identity-team handoff (Keycloak)

> Generated by `scripts/keycloak_gating_setup.py --emit-handoff` from the
> same constants that drive the dev setup. Do not edit by hand — change
> the script and regenerate (`--emit-handoff docs/keycloak_gating_handoff.md`).

## What syzygy needs

While `LOGIC_GATING_ENABLED=true`, the app derives each user's
pairing-logic entitlements from the **access token** of its confidential
`syzygy` client (`syzygy/auth.py`; full behavior in
`docs/logic_gating_setup.md`, "How the app reads entitlements"). Two
claims, both optional — an absent claim is fail-closed (no logics):

| Claim | Shape | Meaning |
|---|---|---|
| `{LOGIC_CLAIM}` | JSON list of exact logic names, e.g. `["ARGOS", "DMP"]` | pairing logics the user may see |
| `{ADMIN_CLAIM}` | truthy value (reference shape `true`) | may trigger pairing and edit pairing logics |

Logic names must match the app's `pairing_logics` table **exactly,
case-sensitive**; unknown names are ignored server-side with a warning.

## Option (a) — realm groups (the reference shape)

Flat **top-level** groups, **exact case**, one per pairing logic:

| Group | Pairing logic |
|---|---|
{groups_rows}

The mappers — all **built-in** types, no scripting — live on the
`syzygy` client's **dedicated client scope** (attached as a default
scope of the client). Every mapper must be stored under protocol
`{MAPPER_PROTOCOL}` (the scope's protocol): on Keycloak 26.8.1 a mapper
stored under any other protocol is **silently skipped at token
issuance**, and a PUT cannot change an existing mapper's protocol —
recreate it instead.

{mapper_sections}
User-profile requirement (Keycloak >= 26 hides the user *Attributes*
tab behind the User Profile feature): declare `{ADMIN_CLAIM}` as a
**managed attribute** (admin view/edit — `{MANAGED_ATTRIBUTE['displayName']}`,
not required) and set the realm's **unmanaged attributes** policy to
**Admin can edit** (`{UNMANAGED_ATTRIBUTE_POLICY}`).

Flow for the identity team: the users already exist (institutional
identity federation into our realm); merely **assign group membership**
for the logics each user should access, and set the `{ADMIN_CLAIM}`
attribute on logic admins. No app-side change.

## Option (b) — existing institutional claims, zero new groups

If the org realm already carries suitable claims, no groups or mappers
are needed at all: set the app's env vars to the org claim names (a
`.env` change — no code change):

| App env var | Default |
|---|---|
| `{APP_CLAIM_ENV[0]}` | `{LOGIC_CLAIM}` |
| `{APP_CLAIM_ENV[1]}` | `{ADMIN_CLAIM}` |

The claim shapes must match the table above (exact-name JSON list /
truthy boolean) and the claims must ride in the **access token** of the
`syzygy` client. If the org claim shape differs (e.g. a group *path*
like `/syzygy/logic/argos` instead of an exact name), a transform is
required that no built-in mapper provides — see the JS-mapper appendix
in `docs/logic_gating_setup.md`.

## Minimum-privilege asks

- A **realm-admin** on the syzygy realm is sufficient — never a
  master-realm admin.
- Option (a) needs, on the syzygy realm: manage **groups**; manage
  protocol mappers on the `syzygy` client's dedicated scope; manage the
  realm **user profile** (the `{ADMIN_CLAIM}` attribute declaration +
  the unmanaged-attributes policy); and **group-membership assignment**
  on users.
- Option (b) needs **no realm permissions** — it is an app-side
  configuration change only.

## Deferred

- **Brokered-realm group assignment**: end-to-end testing of assigning
  federated (brokered) users to the groups above is **DEFERRED** until
  the org realm exists. The shape above was built and verified on the
  dev compose Keycloak (26.8.1), where the same mappers/claims issue
  for directly-created users; brokered users were not yet testable.

## Reproduce on dev

    .venv/bin/python scripts/keycloak_gating_setup.py \\
        --realm master --username admin --password admin \\
        --personas --allow-user-creation

The script is idempotent (run twice, same result) and ends by printing
each dev persona's decoded access-token claims.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Idempotent Keycloak setup for syzygy pairing-logic "
        "gating (groups, dedicated-scope mappers, user-profile attribute, "
        "optional dev personas).",
    )
    ap.add_argument("--url", default=None, help="Keycloak base URL "
                    "(default: $KEYCLOAK_URL, .env, or http://localhost:8080)")
    ap.add_argument("--realm", default=None, help="target realm "
                    "(default: $KEYCLOAK_REALM, .env, or master)")
    ap.add_argument("--username", default=None, help="realm-admin username "
                    "(default: $KEYCLOAK_ADMIN_USERNAME)")
    ap.add_argument("--password", default=None, help="realm-admin password "
                    "(default: $KEYCLOAK_ADMIN_PASSWORD)")
    ap.add_argument("--admin-token-client", default=None,
                    help="pin the client used to mint the admin token "
                    "(default: try admin-cli in the target realm, then in master)")
    ap.add_argument("--client-id", default=None, help="the app's client id "
                    "(default: $KEYCLOAK_CLIENT_ID, .env, or syzygy)")
    ap.add_argument("--client-secret", default=None,
                    help="the app client's secret (default: "
                    "$KEYCLOAK_CLIENT_SECRET, .env). If the client is "
                    "missing and no secret is set, one is generated and "
                    "printed for .env.")
    ap.add_argument("--personas", action="store_true",
                    help="also create the dev persona users with passwords "
                    "and group memberships, then print their decoded "
                    "access-token claims (DEV COMPOSE ONLY)")
    ap.add_argument("--allow-user-creation", action="store_true",
                    help="confirm this is a dev stack: required for "
                    "--personas (user creation is refused without it, so a "
                    "federated/shared realm is never touched by accident)")
    ap.add_argument("--emit-handoff", nargs="?", const="-", default=None,
                    metavar="FILE",
                    help="print (or write to FILE) the identity-team handoff "
                    "one-pager rendered from the same source of truth, and "
                    "exit without touching Keycloak")
    return ap.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    base_url = resolve_setting(args.url, "KEYCLOAK_URL", "http://localhost:8080")
    realm = resolve_setting(args.realm, "KEYCLOAK_REALM", "master")
    client_id = resolve_setting(args.client_id, "KEYCLOAK_CLIENT_ID", "syzygy")
    client_secret = resolve_client_secret(args.client_secret)

    username = args.username or os.environ.get("KEYCLOAK_ADMIN_USERNAME")
    password = args.password if args.password is not None else os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not username or password is None:
        print(
            "error: realm-admin credentials required: pass --username/--password "
            "(or set KEYCLOAK_ADMIN_USERNAME / KEYCLOAK_ADMIN_PASSWORD). "
            "A realm-admin on the target realm is sufficient; never a "
            "master-realm admin.",
            file=sys.stderr,
        )
        return 2

    print(f"Keycloak: {base_url} | realm: {realm} | client: {client_id}")
    try:
        token = mint_admin_token(base_url, realm, username, password,
                                 args.admin_token_client)
        admin = KcAdmin(base_url, realm, token)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: Keycloak unreachable at {base_url}: {type(exc).__name__}",
              file=sys.stderr)
        return 1

    try:
        client, client_changes = ensure_client(admin, client_id, client_secret)
        print("client: " + (", ".join(client_changes)
                            if client_changes else f"unchanged ({client_id!r})"))

        group_ids, group_changes = ensure_groups(admin)
        print("groups: " + (", ".join(f"created {g!r}" for g in group_changes)
                            if group_changes else f"unchanged ({', '.join(GROUP_NAMES)})"))

        profile_changes = ensure_user_profile(admin)
        print("user profile: " + (", ".join(profile_changes)
                                  if profile_changes else "unchanged (managed attribute + policy already set)"))

        scope, scope_changes = ensure_dedicated_scope(admin, client)
        print("dedicated scope: " + (", ".join(scope_changes)
                                     if scope_changes else f"unchanged ({client_id!r}-dedicated)"))

        mapper_changes = ensure_mappers(admin, scope)
        print("mappers: " + (", ".join(mapper_changes)
                             if mapper_changes else "unchanged (all mappers already set)"))

        if args.personas:
            persona_changes = ensure_personas(admin, group_ids)
            print("personas: " + (", ".join(persona_changes)
                                  if persona_changes else "unchanged"))
            print_persona_claims(base_url, realm, client_id, client_secret)
        print("\nDone — Keycloak gating setup complete (idempotent; safe to re-run).")
        return 0
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        admin.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.personas and not args.allow_user_creation:
        print(
            "error: --personas creates Keycloak users; it is a DEV COMPOSE "
            "convenience and is refused against shared/federated realms. "
            "Pass --allow-user-creation to confirm this is a dev stack.",
            file=sys.stderr,
        )
        return 2
    if args.emit_handoff is not None:
        text = render_handoff()
        if args.emit_handoff == "-":
            print(text)
        else:
            with open(args.emit_handoff, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"wrote {args.emit_handoff}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
