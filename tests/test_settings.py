from database import Settings


def test_keycloak_settings_round_trip():
    s = Settings(
        keycloak_url="http://kc.example.com:8080",
        keycloak_realm="syzygy-test",
        keycloak_client_id="syzygy-client",
        keycloak_client_secret="s3cret",
    )
    assert s.keycloak_url == "http://kc.example.com:8080"
    assert s.keycloak_realm == "syzygy-test"
    assert s.keycloak_client_id == "syzygy-client"
    assert s.keycloak_client_secret == "s3cret"


def test_keycloak_settings_defaults(monkeypatch):
    for var in ("DATABASE_URL", "DEFAULT_PAIRING_LOGIC_NAME",
               "KEYCLOAK_URL", "KEYCLOAK_REALM",
               "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.keycloak_url == "http://localhost:8080"
    assert s.keycloak_realm == "master"
    assert s.keycloak_client_id == "syzygy"
    assert s.keycloak_client_secret == ""
