"""Application entry point.

Serves the API and the frontend from one process on one port, so the whole
application is `python -m backend.app.main` and a browser tab.
"""
from __future__ import annotations

import logging
import threading
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import collection, companies, exports, research, search
from .config import settings
from .core.errors import register_error_handlers
from .core.logging import setup_logging
from .db import init_db

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Starting Job Market Research Platform")
    log.info("Data directory: %s", settings.data_dir)
    init_db()
    yield
    log.info("Shutting down")


app = FastAPI(
    title="Job Market Research Platform",
    description=(
        "Local job-market research tool. Collects only publicly accessible "
        "postings from permitted sources; contains no functionality for "
        "bypassing access controls, CAPTCHAs or rate limits."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://{settings.host}:{settings.port}",
                   "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(search.router, prefix="/api", tags=["search"])
app.include_router(collection.router, prefix="/api", tags=["collection"])
app.include_router(research.router, prefix="/api", tags=["research"])
app.include_router(companies.router, prefix="/api", tags=["companies"])
app.include_router(exports.router, prefix="/api", tags=["exports"])


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version,
            "database": str(settings.db_path)}


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------
frontend = settings.frontend_dir
if frontend.exists():
    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/")
    def index():
        return FileResponse(frontend / "index.html")
else:
    @app.get("/")
    def missing_frontend():
        return JSONResponse(
            {"error": {
                "message": "The frontend files were not found.",
                "hint": f"Expected them at {frontend}. The API is still "
                        f"available at /docs."}},
            status_code=500)


# --------------------------------------------------------------------------
def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:                                 # noqa: BLE001
        log.info("Could not open a browser automatically. Visit %s", url)


def main() -> None:
    import uvicorn

    url = f"http://{settings.host}:{settings.port}"
    print("\n" + "=" * 62)
    print("  Job Market Research Platform")
    print(f"  Open:     {url}")
    print(f"  API docs: {url}/docs")
    print(f"  Data:     {settings.data_dir}")
    print("  Press Ctrl+C to stop.")
    print("=" * 62 + "\n")

    if settings.open_browser:
        threading.Timer(1.5, _open_browser, args=(url,)).start()

    uvicorn.run("backend.app.main:app", host=settings.host,
                port=settings.port, reload=settings.reload, log_config=None)


if __name__ == "__main__":
    main()
