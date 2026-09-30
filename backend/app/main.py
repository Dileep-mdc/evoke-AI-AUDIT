import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.scans import router
from .crawler.extract import shutdown_pool
from .crawler.http import aclose_clients
from .db import init_db

# The crawler and the scoring engine report what they are doing at INFO -- which addresses
# were unreachable, how long a discovery round took, why a render pass was skipped. Uvicorn
# configures only its own loggers, so without this none of it reaches the console and a slow
# scan can only be guessed at from the progress bar.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

init_db()

app = FastAPI(title="AI Visibility Audit", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
async def _recalculate_saved_reports() -> None:
    """Bring reports saved under an older scoring method up to the current one, so the
    dashboard, the history cards and the downloads all show the same numbers."""
    from .api.scans import upgrade_saved_reports
    upgrade_saved_reports()


@app.on_event("shutdown")
async def _close_crawler_connections() -> None:
    """Return the crawler's pooled HTTP connections on the way out.

    The pool is process-wide and deliberately outlives a single scan -- that is what keeps a
    crawl from renegotiating TLS for every page -- so something has to hand the sockets back
    when the server stops rather than leaving them for the garbage collector.
    """
    await aclose_clients()
    shutdown_pool()


@app.get("/api/health")
async def health():
    return {"ok": True}


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="ui")
