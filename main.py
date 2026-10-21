from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from database import Base, SessionLocal, engine
from models import PairingLogic
from routers.pairing_logic import router as pairing_logic_router
from routers.pairings import router as pairings_router
from routers.samples import router as samples_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for name, strategy in (
            ("ARGOS", "argos"),
            ("TEMPO", "tempo"),
            ("ACCESS", "default"),
        ):
            existing = db.scalar(select(PairingLogic).where(PairingLogic.name == name))
            if existing is None:
                db.add(PairingLogic(name=name, strategy_name=strategy))
            elif existing.strategy_name != strategy:
                # Self-heal rows created before the beagle-aligned
                # strategies existed (e.g. TEMPO seeded as "default").
                existing.strategy_name = strategy
        db.commit()
    finally:
        db.close()
    yield
    engine.dispose()


app = FastAPI(
    title="Sample Pairing Service",
    version="1.0.0",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(samples_router, prefix="/v1")
app.include_router(pairings_router, prefix="/v1")
app.include_router(pairing_logic_router, prefix="/v1")

# Basic web front end (static, no build step).
app.mount(
    "/ui",
    StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
    name="ui",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")
