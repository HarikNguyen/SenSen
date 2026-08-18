"""SenSen API — FastAPI application factory and lifecycle.

Routes live in app/pages.py, business logic in app/logics.py and
app/scanning.py, Presidio setup in app/engine.py, auth in app/auth.py.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.engine import build_engines
from app.pages import router

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.analyzer, app.state.anonymizer = build_engines()
    yield


app = FastAPI(
    title="SenSen — Sensitive Data Classifier",
    description="Presidio-powered enterprise sensitive data discovery & classification API.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(router)
