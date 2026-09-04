"""
FastAPI entrypoint. Mounts the /api router and serves the compiled React SPA
(frontend/dist) via StaticFiles at /. Same-origin, single process, no CORS in
prod. Auth is the app SP's injected credentials, reused verbatim by the core
modules — no new auth work here.

Run:  uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="NBA Console (React)", version="1.0.0")

app.include_router(api_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Static SPA — serve the Vite build. The frontend build outputs to
# frontend/dist (see vite.config.ts). We resolve that path relative to this
# file so it works both locally and in the deployed app.
# -----------------------------------------------------------------------------
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_INDEX = _DIST / "index.html"


if _DIST.exists():
    # Mount hashed assets (JS/CSS/fonts) under /assets.
    _assets = _DIST / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(_INDEX))

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Never shadow the API.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        # Serve real files (favicon, etc.) if present; otherwise SPA fallback.
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_INDEX))
else:
    @app.get("/")
    def index_missing():
        return JSONResponse(
            {"detail": "frontend/dist not built. Run `npm run build` in "
                       "src/app_react/frontend."},
            status_code=200,
        )
