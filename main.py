from contextlib import asynccontextmanager

from fastapi import FastAPI
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
        for name in ("TEMPO", "IMPACT", "ACCESS"):
            existing = db.scalar(select(PairingLogic).where(PairingLogic.name == name))
            if existing is None:
                db.add(PairingLogic(name=name, strategy_name="default"))
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

app.include_router(samples_router, prefix="/v1")
app.include_router(pairings_router, prefix="/v1")
app.include_router(pairing_logic_router, prefix="/v1")
