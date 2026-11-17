# Per-user pairing-logic gating — IdP setup

How to configure the IdP side (Keycloak groups + mappers) for per-user
pairing-logic access, the app's env flags, and the rollout order.

> **The org's Keycloak approach is NOT yet decided.** This document is a
> **reference implementation** of the group + mapper walkthrough using the
> app's **default claim names** (`syzygy_logics` / `syzygy_admin`). When the
> org's Keycloak reality is known — a different group layout, an existing
> entitlement scheme, a different IdP — the adaptation knob is
> **configuration, not code**: `LOGIC_CLAIM_NAME` and `LOGIC_ADMIN_CLAIM`
> name the token claims the app reads, and the app accepts exactly the
> claim shapes described in [How the app reads entitlements](#how-the-app-reads-entitlements).
> Everything below (group names, mappers, claim shapes) is the reference
> shape to adapt.

## How the app reads entitlements

While `LOGIC_GATING_ENABLED=true`, the app derives each principal's
pairing-logic entitlements from their **access token** (ticket C1,
`syzygy/auth.py`):

- `LOGIC_CLAIM_NAME` (default `syzygy_logics`) — a **JSON list of logic
  names**, matched **exactly** (case-sensitive) against the names in the
  `pairing_logics` table, e.g. `["ARGOS", "DMP"]`. Names that are not known
  logics are ignored with a server warning. A non-list value is treated as
  a single name; a missing claim means **no logics**.
- `LOGIC_ADMIN_CLAIM` (default `syzygy_admin`) — any **truthy** value makes
  the principal a logic admin (the reference shape is boolean `true`).

What the flag does:

| State | Behavior |
|---|---|
| `LOGIC_GATING_ENABLED=false` (default) | Exactly today's behavior: every allow-listed user gets the full logic set and admin. Claims, if present, are ignored (dark launch). |
| `true` + claims present | The principal's `allowed_logics` = the valid names in their claim. Listings filter to those logics (Layer B), logic-scoped reads 403 `"Logic not accessible"` (Layer A), and trigger / pairing-logic writes require the admin claim, 403 `"Logic admin required"` (Layer C). |
| `true` + no / unknown claim | **Fail-closed**: `allowed_logics` is empty — the user sees no logic-scoped data (empty listings, 403s on logic-scoped reads). |

The dev-token principal (`DEV_AUTH_TOKEN`) always gets the full set +
admin. The UI reflects the same entitlements from `GET /auth/me`
(`allowed_logics`, `is_logic_admin`) — **UX only**; the server-side 403s
are the real gate (ticket C5).

## Env flags (app)

| Env var | Default | Meaning |
|---|---|---|
| `LOGIC_GATING_ENABLED` | `false` | Master switch (dark launch). `false` = today's behavior; no Keycloak changes needed. |
| `LOGIC_CLAIM_NAME` | `syzygy_logics` | Access-token claim holding the list of logic names. |
| `LOGIC_ADMIN_CLAIM` | `syzygy_admin` | Access-token claim holding the logic-admin flag. |

All three are documented in `.env.example` (the `Logic gating` section) and
map 1:1 to `Settings` fields in `syzygy/database.py`.

## Reference Keycloak layout (dev compose)

The dev stack (`docker-compose.dev.yml`) runs Keycloak 26.8.1 at
`http://localhost:8080` (admin `admin` / `admin`), realm **master**, client
**syzygy** (confidential, standard flow — the same client the app uses; see
the README, "Authentication (Keycloak)").

The reference layout uses **built-in mappers only** (no scripting — works on
any Keycloak version):

1. **Groups** — top-level groups in the realm, named **exactly** like the
   pairing-logic names (case-sensitive; must match the `pairing_logics`
   table):

   ```
   ARGOS
   DMP
   HARMONY
   ACCESS
   TEMPO
   ```

   Assign users to the groups for the logics they should access (Admin
   console → *Users* → user → *Group membership* → *Add group membership*).

2. **Logics mapper** — Admin console → *Clients* → `syzygy` → *Mappers* →
   *Create mapper* → *Protocol mapper* → **Group Membership**:

   | Field | Value |
   |---|---|
   | Name | `syzygy logics` |
   | Token claim name | `syzygy_logics` |
   | **Full group path** | **OFF** |
   | Include in **access token** | **ON** |

   → a user in `ARGOS` + `DMP` gets `"syzygy_logics": ["ARGOS", "DMP"]`;
   a user in no group gets `[]`.

   **Keep the groups flat and `Full group path` OFF** — paths like
   `/syzygy/logic/argos` do not match the exact logic names and would
   fail-closed (ignored with a warning).

3. **Admin claim** — set a **user attribute** `syzygy_admin` = `true` on
   each logic-admin user (*Users* → user → *Attributes* → *Add attribute*:
   name `syzygy_admin`, value `true`), then a second mapper — *Clients* →
   `syzygy` → *Mappers* → *Create mapper* → *Protocol mapper* →
   **User Attribute**:

   | Field | Value |
   |---|---|
   | Name | `syzygy admin` |
   | Token claim name | `syzygy_admin` |
   | User attribute | `syzygy_admin` |
   | Claim value type | **Boolean** |
   | Include in **access token** | **ON** |

   → logic admins get `"syzygy_admin": true`; everyone else gets no claim.

Notes:

- The Group Membership mapper puts **every** realm group the user belongs
  to into `syzygy_logics`; group names that are not known logics are
  ignored server-side with a warning. Harmless, but keep the realm tidy.
- The admin claim accepts any truthy value (`bool(...)` in the app);
  boolean `true` is the reference shape.
- The claims are read from the **access token** — leave *Include in access
  token* ON.

## Verifying the claims (before flipping the flag)

While `LOGIC_GATING_ENABLED=false`, `GET /auth/me` reports the full set for
everyone (dark launch), so verify the **raw access token**:

1. Log in to the app in the browser (host-side uvicorn, default
   `http://127.0.0.1:8000` in dev).
2. Copy the `auth_token` cookie (DevTools → *Application* → *Cookies* → the
   app's origin — it holds the access token, httpOnly).
3. Decode the JWT payload, e.g.:

   ```bash
   python3 - <<'EOF'
   import base64, json
   token = "PASTE_AUTH_TOKEN_COOKIE_VALUE"
   p = token.split(".")[1]
   p += "=" * (-len(p) % 4)
   print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))
   EOF
   ```

   (or paste the token into jwt.io — dev tokens only, never prod material).

   Check: `syzygy_logics` = the expected list of **exact** logic names for
   the user, and `syzygy_admin` = `true` for logic admins only.

## Production guidance (org-managed Keycloak)

- Create the equivalent groups / user attributes in the org's realm,
  **or** point the app at the org's existing entitlement claims: set
  `LOGIC_CLAIM_NAME` / `LOGIC_ADMIN_CLAIM` to the org's claim names (a
  `.env` change — no code change). The claim shapes the app accepts are
  the ones in [How the app reads entitlements](#how-the-app-reads-entitlements):
  an exact-name JSON list plus a truthy admin value, in the **access
  token** of the app's confidential client.
- If the org prefers a nested group layout (e.g. `syzygy/logic/argos`
  groups plus a `syzygy/admin` membership) instead of flat exact-name
  groups, the claim must be *transformed* (path → exact logic name) — a
  built-in mapper cannot do that; see the [JS-mapper appendix](#js-mapper-appendix-nested-group-layouts-keycloak-26).

## Rollout order

1. **Populate the IdP first** (while `LOGIC_GATING_ENABLED=false`): create
   the groups, assign users, set the admin attributes, add the mappers.
   Nothing changes behaviorally yet — the claims just ride along in
   tokens.
2. **Verify the claims** in the raw access token (procedure above).
3. **Flip the flag**: set `LOGIC_GATING_ENABLED=true` in the app's `.env`,
   restart the app.
4. **UAT three personas** — an ARGOS-only user, a DMP-only user, and a
   logic admin: listings show only each user's logics; the topbar View
   selector lists only their logics (pairing tabs hidden when they have
   none); the DMP-only-groups toggle appears only for DMP-allowed users;
   the admin can trigger pairing and edit pairing logics while a
   non-admin gets 403 on both.
5. **Rollback**: set the flag back to `false` and restart — no Keycloak
   changes needed.

## JS-mapper appendix: nested group layouts (Keycloak 26)

If the org wants the nested layout sketched in the design (`syzygy` →
`logic` → `argos|dmp|...` groups plus a `syzygy/admin` group) instead of
flat exact-name groups, the claim must be transformed (group path →
exact logic name / admin membership), which a built-in mapper cannot do.
That needs an **OpenID Connect Script (JavaScript) protocol mapper**:

- Keycloak 26: the `scripts` feature is a technology preview — enable it
  at startup with `--features=scripts` (env `KC_FEATURES=scripts`).
  Since 26.x, scripts can **no longer be pasted into the admin console**;
  they are deployed to the server as a JAR in the `providers/` directory
  containing a `META-INF/keycloak-scripts.json` descriptor (see the
  Keycloak server developer guide, "Scripts"), followed by
  `kc.sh build`. In the dev compose, bind-mount the JAR into the
  Keycloak container's `providers/` directory.
- Reference scripts (use an iterator — plain Java interop, engine-
  agnostic):

  ```js
  // Mapper 1 — token claim name `syzygy_logics`, claim value type JSON.
  // /syzygy/logic/<name> groups -> exact (upper-case) logic names.
  var result = [];
  if (user != null) {
    var it = user.getGroupsStream().iterator();
    while (it.hasNext()) {
      var m = String(it.next().getPath()).match(/^\/syzygy\/logic\/([a-z0-9_-]+)$/i);
      if (m) result.push(m[1].toUpperCase());
    }
  }
  $claim = result;
  ```

  ```js
  // Mapper 2 — token claim name `syzygy_admin`, claim value type Boolean.
  // Membership of /syzygy/admin.
  var admin = false;
  if (user != null) {
    var it = user.getGroupsStream().iterator();
    while (it.hasNext() && !admin) {
      admin = String(it.next().getPath()) === "/syzygy/admin";
    }
  }
  $claim = admin;
  ```

  Both mappers: *Include in access token* ON. Adapt the regex / group
  paths to the org's layout, and keep `LOGIC_CLAIM_NAME` /
  `LOGIC_ADMIN_CLAIM` pointing at the resulting claims.

## UI behavior (UX only)

- **View selector** (topbar): lists only the principal's accessible active
  logics; with none accessible it shows "No accessible pairing logics" and
  the three pairing tabs (Pair Report / Matches & Overrides / Try
  Pairing) are hidden. A persisted or server-default View that the
  principal may no longer access is dropped.
- **Patients tab**: the "DMP-only groups" toggle is shown only when DMP is
  accessible (ticket C4).
- **Admin-only controls**: the Override buttons in Matches & Overrides and
  the "Allow overrides" checkbox are shown only to logic admins (ticket
  C5).

Nothing here is enforcement: the server still 403s every access outside
the principal's entitlements, including deep links and direct API calls.
