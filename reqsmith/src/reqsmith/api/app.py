import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import reqsmith.stages  # noqa: F401 — importing registers all stage handlers
from reqsmith.api import admin, jobs, reviewer, webhooks_jira
from reqsmith.orchestrator.engine import worker_loop

_CONSOLE_DIR = Path(__file__).resolve().parents[4] / "console" / "dist"


def create_app(*, run_worker: bool = True) -> FastAPI:
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker_task = None
        if run_worker:
            worker_task = asyncio.create_task(worker_loop(stop))
        yield
        stop.set()
        if worker_task is not None:
            await worker_task

    app = FastAPI(title="reqsmith", lifespan=lifespan)
    app.include_router(webhooks_jira.router)
    app.include_router(jobs.router)
    app.include_router(admin.router)
    app.include_router(reviewer.router)

    # Serve the pre-built Reviewer Console SPA when the dist/ directory exists.
    # In CI/dev the console is not built, so this mount is skipped gracefully.
    if _CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=_CONSOLE_DIR, html=True), name="console")

    return app
