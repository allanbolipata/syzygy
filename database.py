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

    # Optional read-only beagle DB (the restored beagle dump — local data,
    # never in production) used ONLY to resolve the DMP pair file's
    # bam_exists column (docs/dmp_reference_alignment.md section f, the
    # sanctioned deviation from the reference's BAM-mirror scan): a
    # sample's file record with a non-empty path -> 1, else 0. Unset or
    # unreachable -> bam_exists 0 (unresolvable -> 0).
    beagle_database_url: Optional[str] = None

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
    # DMP sample records from the dmp-gateway service: the JSON record in
    # the exact syzygy DB record format (docs/dmp_reference_alignment.md,
    # section f) — upserted as-is via dmp_translator.to_dmp_sample_kwargs.
    nats_dmp_sample_subject: str = (
        "MDB_STREAM.server.dmp-gateway.dmp-sample"
    )


settings = Settings()

#: Explicit pool sizing instead of SQLAlchemy's defaults (pool_size 5,
#: max_overflow 10, pool_timeout 30). A bulk pairing pass holds one session for
#: its entire multi-minute Python loop, and FastAPI runs `def` endpoints on a
#: 40-token AnyIO threadpool, so a few concurrent panel loads could check out
#: more connections than the pool had and raise
#: `QueuePool limit of size 5 overflow 10 reached ... timeout 30.00` mid
#: request. main.py caps the threadpool at THREADPOOL_TOKENS so in-flight DB
#: work can never outnumber POOL_SIZE + POOL_MAX_OVERFLOW.
POOL_SIZE = 10
POOL_MAX_OVERFLOW = 10
POOL_TIMEOUT_SECONDS = 10
THREADPOOL_TOKENS = 12

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=POOL_MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT_SECONDS,
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
