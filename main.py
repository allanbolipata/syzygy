from contextlib import asynccontextmanager

from fastapi import FastAPI

from database import Base, engine
from routers.pairings import router as pairings_router
from routers.samples import router as samples_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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
