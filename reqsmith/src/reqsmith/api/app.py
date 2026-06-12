import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

import reqsmith.stages  # noqa: F401 — importing registers all stage handlers
from reqsmith.api import admin, jobs, webhooks_jira
from reqsmith.orchestrator.engine import worker_loop


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
    return app
