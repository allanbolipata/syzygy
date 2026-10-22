from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from database import Base, SessionLocal, engine
from models import PairingLogic
from pairing_strategies import seed_pooled_machines
from bait_sets import seed_bait_sets
from routers.bait_sets import router as bait_sets_router
from routers.machines import router as machines_router
from routers.patients import router as patients_router
from routers.pairing_logic import router as pairing_logic_router
from routers.pairings import router as pairings_router
from routers.requests import router as requests_router
from routers.samples import router as samples_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Idempotent machine reference rows for ARGOS pooled run-mode
        # resolution (existing rows are left untouched).
        seed_pooled_machines(db)
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
        seed_bait_sets(db)
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
app.include_router(patients_router, prefix="/v1")
app.include_router(requests_router, prefix="/v1")
app.include_router(pairings_router, prefix="/v1")
app.include_router(pairing_logic_router, prefix="/v1")
app.include_router(machines_router, prefix="/v1")
app.include_router(bait_sets_router, prefix="/v1")


# Local dev UI: never serve stale cached assets (no build step, no
# versioned filenames — always refetch from disk).
@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Basic web front end (static, no build step).
app.mount(
    "/ui",
    StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
    name="ui",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")
