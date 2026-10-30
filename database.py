from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
    default_pairing_logic_name: Optional[str] = None
    keycloak_url: str = "http://localhost:8080"
    keycloak_realm: str = "master"
    keycloak_client_id: str = "syzygy"
    keycloak_client_secret: str = ""

    # Dev-only local login: when dev_auth_token is non-empty, that static
    # token (sent as Bearer or in the auth_token cookie) authenticates as
    # the local dev admin WITHOUT contacting Keycloak or consulting
    # allowed_users. Default "" = disabled; never set in production.
    dev_auth_token: str = ""
    dev_auth_email: str = "dev-admin@syzygy.local"

    # NATS ingest (internal endpoint used by the standalone smile-listener;
    # the app itself never connects to NATS). Fail-closed: an empty token
    # (the default) DISABLES the endpoint — every request is rejected 403.
    # The listener must send the same value as X-NATS-Token.
    nats_ingest_token: str = ""
    # Production subject names (JetStream stream MDB_STREAM); dispatch maps
    # each to its handler, so the mapping follows these settings values.
    nats_new_request_subject: str = (
        "MDB_STREAM.server.cpt-gateway.cmo-new-request"
    )
    nats_request_update_subject: str = (
        "MDB_STREAM.server.cpt-gateway.cmo-request-update"
    )
    nats_sample_update_subject: str = (
        "MDB_STREAM.server.cpt-gateway.cmo-sample-update"
    )

    # NATS cohort (TEMPO) subjects
    nats_cohort_submit_subject: str = (
        "MDB_STREAM.consumers.tempo.new-cohort-submit"
    )
    nats_cohort_update_subject: str = (
        "MDB_STREAM.consumers.tempo.cohort-update"
    )
    nats_cohort_result_subject: str = (
        "MDB_STREAM.consumers.tempo.cohort-result"
    )
    crf_output_dir: str = "./crf_output"
    cohort_publish_enabled: bool = False


settings = Settings()

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
