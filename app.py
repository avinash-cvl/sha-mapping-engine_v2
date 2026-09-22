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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common import auth
from routers import engine, session

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")

app = FastAPI(
    title="Harmonization Engine Console",
    description="Run, configure and review the SKU harmonization engine.",
    version="0.1.0",
)

app.include_router(session.router)
app.include_router(engine.router)


@app.get("/health")
def health() -> dict:
    """Liveness only. Deliberately does not touch the database: a health check
    that fails when the DB is slow takes the console down exactly when an
    operator most needs to see what a run is doing."""
    return {"status": "ok"}


if os.path.isdir(WEB):
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def _page(request: Request, filename: str):
        """Serve a console page, or redirect to sign-in.

        The check belongs here rather than in the page's own JavaScript. A
        page served to a signed-out browser renders its empty shell first and
        only redirects once the script has run and a fetch has come back 401
        -- which looks exactly like a broken screen that loads no data. The
        server knows before a byte is sent.

        `next` carries the requested path so a deep link survives an expired
        session instead of dumping the user on the run page.
        """
        try:
            auth.current_user(request)
        except Exception:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return FileResponse(os.path.join(WEB, filename))

    @app.get("/")
    def index(request: Request):
        return _page(request, "run.html")

    @app.get("/configuration")
    def configuration(request: Request):
        return _page(request, "configuration.html")

    @app.get("/history")
    def history(request: Request):
        return _page(request, "history.html")

    @app.get("/login")
    def login_page(request: Request):
        """Already signed in? Don't show a sign-in form -- go where they
        were headed."""
        try:
            auth.current_user(request)
            return RedirectResponse(request.query_params.get("next") or "/", status_code=303)
        except Exception:
            return FileResponse(os.path.join(WEB, "login.html"))
