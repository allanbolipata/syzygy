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
pairing-logic entitlements from **two sources** (tickets C1 + P1;
`syzygy/auth.py`): a **default-open set** and the **access token**:

- `GATING_OPEN_LOGICS` (default `ARGOS,TEMPO,ACCESS`) — a **comma list of
  exact pairing-logic names** that are **default-open**: every
  authenticated principal sees them **without enrollment**, on top of
  whatever their claim grants. This is the asymmetric policy (decision 1):
  ARGOS, TEMPO, ACCESS are open to everyone; DMP and HARMONY are the real
  gate (visible only via enrollment). The open set applies to every
  authenticated principal; enrollment **adds, never subtracts**. Names the
  app does not know are ignored with a server warning (the same rule as the
  claim); an **empty** value disables the open set and restores the uniform
  fail-closed model (claim-derived only).
- `LOGIC_CLAIM_NAME` (default `syzygy_logics`) — a **JSON list of logic
  names**, matched **exactly** (case-sensitive) against the names in the
  `pairing_logics` table, e.g. `["ARGOS", "DMP"]`. Names that are not known
  logics are ignored with a server warning. A non-list value is treated as
  a single name; a missing claim means **no claimed logics** (the
  default-open set still applies).
- `LOGIC_ADMIN_CLAIM` (default `syzygy_admin`) — any **truthy** value makes
  the principal a logic admin (the reference shape is boolean `true`).

The principal's `allowed_logics` is the **union** of the default-open set
and the valid names in the claim, and `GET /auth/me` returns that union so
the UI View selector matches server-side truth.

What the flag does:

| State | Behavior |
|---|---|
| `LOGIC_GATING_ENABLED=false` (default) | Exactly today's behavior: every allow-listed user gets the full logic set and admin. Claims and the default-open set are both ignored (dark launch — byte-identical to today). |
| `true`, principal has a claim | `allowed_logics` = `GATING_OPEN_LOGICS` ∪ the valid names in the claim. Listings filter to that set (Layer B), logic-scoped reads 403 `"Logic not accessible"` (Layer A), and trigger / pairing-logic writes require the admin claim, 403 `"Logic admin required"` (Layer C). |
| `true`, principal has no claim | `allowed_logics` = the default-open set only (default: ARGOS, TEMPO, ACCESS). With `GATING_OPEN_LOGICS` **empty** this is the uniform **fail-closed** empty set — the user sees no logic-scoped data. |

The dev-token principal (`DEV_AUTH_TOKEN`) always gets the full set +
admin. The UI reflects the same entitlements from `GET /auth/me`
(`allowed_logics`, `is_logic_admin`) — **UX only**; the server-side 403s
are the real gate (ticket C5).

**Sample-family ownership (what Layer B filters on).** Layer B restricts
each listing to the union of the sample *families* of the principal's
`allowed_logics`. A sample belongs to a logic's family when **either** of
these holds (`logic_membership_condition`, `syzygy/bait_sets.py`):

| Family member | Rule (SQL-side) |
|---|---|
| Registered bait set | the sample's `bait_set` matches a **registered** `bait_sets` assignment for the logic (case-insensitive). |
| DMP null-bait family | `bait_set` is **NULL** and `sample_type == 'DMP'` (exact case). |
| HARMONY null-bait family (P2) | `bait_set` is **NULL** and `sample_type == 'HARMONY'` (exact case) — mirrors the DMP rule; HARMONY reuses the `dmp` pairing strategy but owns its own null-bait family. |

Unregistered bait sets (no `bait_sets` assignment) belong to **no** family
and stay invisible under any gate (fail-closed; decision 3). The two
null-bait families are disjoint from each other and from the registered-bait
families, so a HARMONY-enrolled principal sees only HARMONY-family samples
and a DMP-enrolled principal sees only DMP-family samples.

## Consent gate (P3): non-consent DMP samples

While `LOGIC_GATING_ENABLED=true`, a second gate applies on top of the
logic entitlements: **DMP-family samples whose `consent` is `FALSE` are
visible only to principals holding the non-consent entitlement**. `consent`
`TRUE` and `NULL` samples, and all non-DMP samples, are unaffected (a
non-DMP sample's consent can never be `FALSE`, so the single SQL predicate
`consent IS NOT FALSE` hides exactly the non-consent DMP samples).

- **Entitlement.** `NONCONSENT_CLAIM` (default `syzygy_nonconsent`) names
  the access-token claim; `NONCONSENT_GROUP` (default `NONCONSENT`) names
  the realm group that carries it. Reference shape (Keycloak 26.8.1 built-in
  *Group Membership* mapper on the `NONCONSENT` group): the claim value is a
  **JSON list of the user's group names** — the mapper emits *all* of the
  user's groups for any user who has a group (it does not filter on its
  configured group; verified live against the 26.8.1 `GroupMembershipMapper`
  class). The app therefore derives the entitlement as **the
  `NONCONSENT_GROUP` name appearing in that list** — not claim presence; a
  non-member's token still carries the claim (with their other group names).
  A scalar truthy org claim (integration option b) is honored as well.
  Absent claim = fail-closed (the gate applies). Dark launch (gating off)
  never applies the gate.
- **Where it applies.** Layer B row filter on the four listings (samples /
  patients / requests / pairings) and on the Layer A logic-scoped DMP sample
  read (`GET /pairing/unmatched/`). `GET /auth/me` exposes the flag as
  `can_see_nonconsent` so the UI can react (UX only — the server-side filter
  is the real gate).
- **IdP side.** Add the `NONCONSENT` realm group (not a pairing logic — it
  never appears in `syzygy_logics` entitlements) and one *Group Membership*
  mapper feeding `syzygy_nonconsent`; `scripts/keycloak_gating_setup.py`
  does both (see the handoff one-pager, `docs/keycloak_gating_handoff.md`).

## Env flags (app)

| Env var | Default | Meaning |
|---|---|---|
| `LOGIC_GATING_ENABLED` | `false` | Master switch (dark launch). `false` = today's behavior; no Keycloak changes needed. |
| `LOGIC_CLAIM_NAME` | `syzygy_logics` | Access-token claim holding the list of logic names. |
| `LOGIC_ADMIN_CLAIM` | `syzygy_admin` | Access-token claim holding the logic-admin flag. |
| `GATING_OPEN_LOGICS` | `ARGOS,TEMPO,ACCESS` | Comma list of default-open logics (visible to every authenticated user without enrollment). Empty = uniform fail-closed (claim-only). |
| `NONCONSENT_CLAIM` | `syzygy_nonconsent` | Access-token claim for the P3 consent gate (non-consent DMP samples). List of the user's group names; the app checks for the `NONCONSENT_GROUP` name in it. |
| `NONCONSENT_GROUP` | `NONCONSENT` | The realm-group name that carries the P3 consent-gate entitlement (checked inside `NONCONSENT_CLAIM`). |

All six are documented in `.env.example` (the `Logic gating` section) and
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

   plus the P3 consent-gate entitlement group (step 4) — **not** a pairing
   logic, so it never belongs in the list above:

   ```
   NONCONSENT
   ```

   Assign users to the groups for the logics they should access (Admin
   console → *Users* → user → *Group membership* → *Add group membership*).

   > **Console path by version.** Keycloak ≤ 23: *Clients* → `syzygy` →
   > *Mappers* → *Create mapper* → *Protocol mapper*. Keycloak ≥ 24 (the dev
   > compose ships 26.8.1): *Clients* → `syzygy` → **Client scopes** tab →
   > **Assigned mappers** → *Add mapper* → *By configuration*. There is no
   > top-level *Mappers* menu item in ≥ 24; the sidebar's *Client scopes*
   > section is for shared scopes, not client mappers. In ≥ 24 client mappers
   > live in the client's **dedicated scope**: *Clients* → `syzygy` →
   > *Client Scopes* tab → *Edit* on `syzygy-dedicated` → *Assigned mappers*
   > panel → *Add mapper* → *By configuration*.

2. **Logics mapper** — *Group Membership* (path per the note above):

   | Field | Value |
   |---|---|
   | Name | `syzygy logics` |
   | Token claim name | `syzygy_logics` |
   | **Full group path** | **OFF** |
   | **Add to access token** | **ON** |

   (Keycloak ≤ 23 labeled this toggle *Include in access token*; 26 uses
   *Add to access token* / *Add to ID token* / *Add to userinfo*.)

   → a user in `ARGOS` + `DMP` gets `"syzygy_logics": ["ARGOS", "DMP"]`;
   a user in no group gets `[]`.

   **Keep the groups flat and `Full group path` OFF** — paths like
   `/syzygy/logic/argos` do not match the exact logic names and would
   fail-closed (ignored with a warning).

3. **Admin claim** — set a **user attribute** `syzygy_admin` = `true` on
   each logic-admin user (*Users* → user → *Attributes* → *Add attribute*:
   name `syzygy_admin`, value `true`), then a second mapper — *User Attribute*
   (path per the note above):

   > **Keycloak ≥ 26: the *Attributes* tab is hidden by default.** The
   > built-in User Profile feature hides it unless unmanaged attributes are
   > allowed. Enable once per realm: *Realm Settings* → *User Profile* tab →
   > **Unmanaged attributes** → *Admin can edit* → Save. (Alternative:
   > declare `syzygy_admin` as a managed attribute under *User Profile* →
   > *Attributes* instead of loosening the policy.)

   | Field | Value |
   |---|---|
   | Name | `syzygy admin` |
   | Token claim name | `syzygy_admin` |
   | User attribute | `syzygy_admin` |
   | Claim value type | **Boolean** |
   | **Add to access token** | **ON** |

   → logic admins get `"syzygy_admin": true`; everyone else gets no claim.

   > **Toggle label.** In Keycloak 26 the switches are labeled *Add to
   > access token* / *Add to ID token* / *Add to userinfo* (≤ 23 called it
   > *Include in access token*). If your *User Attribute* form shows no
   > access-token switch, that is fine — dedicated-scope mappers land in the
   > access token by default; verify the claim via the raw-token decode below.

4. **Non-consent entitlement mapper (P3)** — a second *Group Membership*
   mapper, same field values as step 2 except:

   | Field | Value |
   |---|---|
   | Name | `syzygy nonconsent` |
   | Token claim name | `syzygy_nonconsent` |
   | Group (claim value) | `NONCONSENT` |
   | **Full group path** | **OFF** |
   | **Add to access token** | **ON** |

   Assign the entitlement by putting a user in the `NONCONSENT` group.
   **Caveat (verified live on 26.8.1):** the built-in mapper ignores the
   configured group as a filter — it emits *all* of the user's group names
   into the claim for any user who has a group. So the claim is present in
   most tokens and the app's entitlement check is "`NONCONSENT` in the
   list", not claim presence (see [Consent gate (P3)](#consent-gate-p3-non-consent-dmp-samples)).
   A NONCONSENT member also gets `NONCONSENT` inside `syzygy_logics` — the
   app ignores unknown logic names (warning only), so this is cosmetic.

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
   the user, `syzygy_admin` = `true` for logic admins only, and
   `syzygy_nonconsent` contains the `NONCONSENT` group name exactly for the
   users who should see non-consent DMP samples (P3).

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
4. **UAT the personas under the asymmetric policy** (P1). With the default
   `GATING_OPEN_LOGICS=ARGOS,TEMPO,ACCESS`:
   - a user in **no group** still sees ARGOS, TEMPO, ACCESS (default-open,
     no enrollment) and nothing else;
   - a **DMP** group member sees the open set **plus** DMP (enrollment
     adds, never subtracts); a **HARMONY** group member sees the open set
     **plus** HARMONY;
   - the topbar View selector lists exactly `allowed_logics` (open ∪
     claimed); the DMP-only-groups toggle appears only for DMP-allowed
     users; the admin can trigger pairing and edit pairing logics while a
     non-admin gets 403 on both.
   To run the uniform fail-closed model instead (no default-open set), set
   `GATING_OPEN_LOGICS=` (empty) in `.env` before flipping the flag.
5. **Rollback**: set the flag back to `false` and restart — no Keycloak
   changes needed; behavior is byte-identical to today (open set or not).

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

  Both mappers: *Add to access token* ON (Keycloak ≤ 23: *Include in
  access token*). Adapt the regex / group
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
