"""Tests for scripts/keycloak_gating_setup.py (ticket P5a).

Covers the parts that are verifiable without a live Keycloak: the
--personas user-creation guard (refused without --allow-user-creation,
before any network call), the JWT payload decoder, the setting
resolution order (flag > env > .env > default), and the P5b handoff
one-pager's consistency with the script's single source of truth
(groups, mappers, claim shapes, integration options, deferred step).
The live idempotency acceptance (run twice on the dev compose stack,
same result) is exercised manually per the ticket's Accept list.
"""

import base64
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for _p in (REPO, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import keycloak_gating_setup as kgs


# --------------------------------------------------------------------------
# --personas guard
# --------------------------------------------------------------------------


def test_personas_refused_without_allow_user_creation(capsys):
    """User creation must be refused before ANY network call."""
    rc = kgs.main(["--personas", "--url", "http://127.0.0.1:9",
                   "--username", "u", "--password", "p"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--allow-user-creation" in err
    assert "federated" in err


def test_persons_allowed_flag_is_not_enough_without_personas(capsys, monkeypatch):
    """--allow-user-creation alone is harmless (no personas -> no users)."""
    # run() would need credentials/network; just assert the guard itself
    # passes when --allow-user-creation is present: main() must not exit
    # with the guard error (rc 2 with that message) on this combination.
    monkeypatch.setattr(kgs, "run", lambda args: 0)
    rc = kgs.main(["--personas", "--allow-user-creation"])
    assert rc == 0


# --------------------------------------------------------------------------
# decode_jwt_payload
# --------------------------------------------------------------------------


def _fake_jwt(payload: dict) -> str:
    body = json.dumps(payload).encode("ascii")
    seg = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    return f"header.{seg}.sig"


def test_decode_jwt_payload_roundtrip():
    payload = {
        "preferred_username": "argos-only",
        "email": "argos-only@syzygy.dev",
        "syzygy_logics": ["ARGOS"],
        "syzygy_admin": True,
    }
    assert kgs.decode_jwt_payload(_fake_jwt(payload)) == payload


def test_decode_jwt_payload_handles_padding():
    payload = {"a" * 39: 1}  # length not a multiple of 3 -> needs padding
    assert kgs.decode_jwt_payload(_fake_jwt(payload)) == payload


# --------------------------------------------------------------------------
# setting resolution order
# --------------------------------------------------------------------------


def test_resolve_setting_flag_wins(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_REALM", "env-realm")
    assert kgs.resolve_setting("flag-realm", "KEYCLOAK_REALM") == "flag-realm"


def test_resolve_setting_env_over_default(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_REALM", "env-realm")
    assert kgs.resolve_setting(None, "KEYCLOAK_REALM") == "env-realm"


def test_resolve_setting_default(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    assert kgs.resolve_setting(None, "KEYCLOAK_REALM", "master") == "master"


def test_resolve_client_secret_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "from-env")
    assert kgs.resolve_client_secret(None) == "from-env"
    assert kgs.resolve_client_secret("from-flag") == "from-flag"


# --------------------------------------------------------------------------
# single source of truth consistency
# --------------------------------------------------------------------------


def test_group_names_are_flat_exact_case():
    assert set(kgs.GROUP_NAMES) == {"ARGOS", "DMP", "HARMONY", "ACCESS", "TEMPO"}
    for name in kgs.GROUP_NAMES:
        assert name == name.upper()


def test_mapper_specs_cover_every_logic_group():
    """One group-membership mapper per logic group (flat, full path
    OFF), all feeding the syzygy_logics list claim; the admin mapper
    reads the syzygy_admin attribute. Every mapper lands in the ACCESS
    token (that's what the app reads)."""
    specs = kgs.group_mapper_specs()
    assert [s["config"]["claim.value"] for s in specs] == list(kgs.GROUP_NAMES)
    for spec in specs:
        assert spec["config"]["claim.name"] == kgs.LOGIC_CLAIM
        assert spec["config"]["full.path"] == "false"
        assert spec["protocolMapper"] == "oidc-group-membership-mapper"
    assert kgs.ADMIN_MAPPER["config"]["claim.name"] == kgs.ADMIN_CLAIM
    assert kgs.ADMIN_MAPPER["config"]["user.attribute"] == kgs.ADMIN_CLAIM
    for spec in specs + [kgs.ADMIN_MAPPER]:
        assert spec["config"]["access.token.claim"] == "true"


def test_mapper_protocol_is_the_scope_protocol():
    """26.8.1 silently skips mappers whose protocol is not the scope's
    protocol (verified live: 'openid' mappers emit no claims;
    'openid-connect' does), and a PUT cannot change an existing
    mapper's protocol — so the script must create under exactly this
    value."""
    assert kgs.MAPPER_PROTOCOL == "openid-connect"


def test_personas_cover_uat_shape():
    """The UAT personas: an ARGOS-only user, a DMP-only user, a logic admin."""
    by_user = {p["username"]: p for p in kgs.PERSONAS}
    assert set(by_user["argos-only"]["groups"]) == {"ARGOS"}
    assert set(by_user["dmp-only"]["groups"]) == {"DMP"}
    assert by_user["logic-admin"]["admin"] is True
    assert set(by_user["logic-admin"]["groups"]) == set(kgs.GROUP_NAMES)


# --------------------------------------------------------------------------
# P5b handoff one-pager
# --------------------------------------------------------------------------


def test_handoff_contains_exact_groups():
    text = kgs.render_handoff()
    for name in kgs.GROUP_NAMES:
        assert f"`{name}`" in text


def test_handoff_contains_mapper_specs_from_source_of_truth():
    """The one-pager must name every mapper (per-group family + admin),
    its protocol-mapper type, and every config key/value, plus the
    required mapper protocol — read from the same source of truth the
    script applies, so doc and script can't drift."""
    text = kgs.render_handoff()
    assert f"`{kgs.MAPPER_PROTOCOL}`" in text
    for spec in kgs.group_mapper_specs() + [kgs.ADMIN_MAPPER]:
        assert f"`{spec['name']}`" in text
        assert f"`{spec['protocolMapper']}`" in text
        for key, value in spec["config"].items():
            assert f"`{key}`" in text
            assert f"`{value}`" in text


def test_handoff_contains_claims_env_and_deferred():
    text = kgs.render_handoff()
    assert kgs.LOGIC_CLAIM in text
    assert kgs.ADMIN_CLAIM in text
    for env_name in kgs.APP_CLAIM_ENV:
        assert env_name in text
    # Option (b) is zero-new-groups; brokered-realm assignment is deferred.
    assert "zero new groups" in text
    assert "DEFERRED" in text
    assert "realm-admin" in text
    assert "master-realm admin" in text  # never-master ask, spelled out


def test_emit_handoff_flag_renders_without_network(capsys, monkeypatch):
    """--emit-handoff must not require credentials or a live Keycloak."""
    rc = kgs.main(["--emit-handoff"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("# Syzygy pairing-logic gating")
    assert kgs.LOGIC_CLAIM in out
    assert kgs.ADMIN_CLAIM in out


def test_emit_handoff_writes_file(tmp_path, capsys):
    target = tmp_path / "handoff.md"
    rc = kgs.main(["--emit-handoff", str(target)])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == kgs.render_handoff()


# --------------------------------------------------------------------------
# mapper repair (Keycloak 26.8.1: wrong-protocol mappers are skipped at
# token issuance and cannot be fixed with a PUT -> delete + recreate)
# --------------------------------------------------------------------------


class _FakeAdmin:
    """Just enough of kgs.KcAdmin for ensure_mappers — no network."""

    def __init__(self, mappers):
        self.mappers = {m["name"]: dict(m) for m in mappers}
        self.deleted = []
        self.created = []

    def get(self, path, **params):
        return list(self.mappers.values())

    def post(self, path, body):
        self.created.append(dict(body))
        self.mappers[body["name"]] = dict(body, id="new")
        return None

    def put(self, path, body):
        raise AssertionError("PUT must not be used for mapper repair")

    def delete(self, path):
        self.deleted.append(path)


def _legacy_first_release_mappers():
    """What this script's first release left on the dedicated scope."""
    return [
        {"id": "m1", "name": "syzygy logics", "protocol": "openid",
         "protocolMapper": "oidc-group-membership-mapper",
         "config": {"claim.name": "syzygy_logics",
                    "full.path": "false", "access.token.claim": "true"}},
        {"id": "m2", "name": "syzygy admin", "protocol": "openid",
         "protocolMapper": "oidc-usermodel-attribute-mapper",
         "config": {"user.attribute": "syzygy_admin",
                    "claim.name": "syzygy_admin",
                    "access.token.claim": "true"}},
    ]


def test_ensure_mappers_repairs_legacy_openid_protocol_mappers():
    """The first-release mappers (protocol 'openid' -> silently skipped
    at token issuance; the group one has no claim.value -> inert) must
    be replaced by the per-group family + admin, all under
    openid-connect, via delete + recreate."""
    fake = _FakeAdmin(_legacy_first_release_mappers())
    changes = kgs.ensure_mappers(fake, {"id": "s1"})
    names = {b["name"] for b in fake.created}
    assert names == ({f"syzygy logics: {g}" for g in kgs.GROUP_NAMES}
                     | {"syzygy admin"})
    for body in fake.created:
        assert body["protocol"] == "openid-connect"
        assert body["protocolMapper"] in (
            "oidc-group-membership-mapper", "oidc-usermodel-attribute-mapper")
    # The inert legacy single group mapper is removed.
    assert any("syzygy logics" in c and "removed" in c for c in changes)
    assert len(fake.deleted) == 2  # legacy group mapper + 'syzygy admin'


def test_ensure_mappers_idempotent_on_second_run():
    """A run over an already-correct scope (server-normalized extra
    config keys included) must change nothing."""
    mappers = []
    for i, spec in enumerate(kgs.group_mapper_specs() + [kgs.ADMIN_MAPPER]):
        cfg = dict(spec["config"])
        cfg["introspection.token.claim"] = "true"  # server adds this on write
        mappers.append({"id": f"i{i}", "name": spec["name"],
                        "protocol": "openid-connect",
                        "protocolMapper": spec["protocolMapper"],
                        "config": cfg})
    fake = _FakeAdmin(mappers)
    assert kgs.ensure_mappers(fake, {"id": "s1"}) == []
    assert fake.deleted == [] and fake.created == []


def test_ensure_mappers_repairs_config_drift():
    """A right-protocol mapper with a drifted config value must also be
    replaced (delete + recreate), not PUT."""
    mappers = list(_legacy_first_release_mappers())
    # Rebuild the per-group family with ONE wrong value.
    for spec in kgs.group_mapper_specs():
        mappers.append({"id": "x", "name": spec["name"],
                        "protocol": "openid-connect",
                        "protocolMapper": spec["protocolMapper"],
                        "config": dict(spec["config"])})
    mappers[2]["config"]["full.path"] = "true"  # drift in one mapper
    fake = _FakeAdmin(mappers)
    changes = kgs.ensure_mappers(fake, {"id": "s1"})
    assert any("config drift" in c for c in changes)
    drifted = [b for b in fake.created
               if b["name"] == mappers[2]["name"]]
    assert drifted and drifted[0]["config"]["full.path"] == "false"
