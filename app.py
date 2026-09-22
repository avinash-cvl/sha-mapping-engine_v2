"""Engine console — FastAPI app.

    uv run uvicorn app:app --reload --port 8000

Serves the three console pages and the API behind them. The CLI is unaffected
by anything here: this process launches `python -m <pkg>.batch_flow match ...`
as a subprocess, which is the same command a terminal user types. There is no
second code path for a run, and no engine logic in the web layer.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routers import engine

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")

app = FastAPI(
    title="Harmonization Engine Console",
    description="Run, configure and review the SKU harmonization engine.",
    version="0.1.0",
)

app.include_router(engine.router)


@app.get("/health")
def health() -> dict:
    """Liveness only. Deliberately does not touch the database: a health check
    that fails when the DB is slow takes the console down exactly when an
    operator most needs to see what a run is doing."""
    return {"status": "ok"}


if os.path.isdir(WEB):
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(WEB, "run.html"))

    @app.get("/configuration")
    def configuration() -> FileResponse:
        return FileResponse(os.path.join(WEB, "configuration.html"))

    @app.get("/history")
    def history() -> FileResponse:
        return FileResponse(os.path.join(WEB, "history.html"))
