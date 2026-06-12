"""DB-backed job engine.

A stage is an idempotent activity (Temporal-shaped: same name, same inputs, safe to
retry). The engine claims one queued job at a time (FOR UPDATE SKIP LOCKED on
Postgres), executes the registered handler, checkpoints, transitions the run via the
state machine, and enqueues the next stage. Crash anywhere → the job is retried; all
side effects inside handlers must go through idempotent repository writes.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import Job, JobStatus, Run, RunState
from reqsmith.persistence.repo import JobRepo, RunRepo

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class StageContext:
    session: AsyncSession
    run: Run
    job: Job
    run_repo: RunRepo
    job_repo: JobRepo


@dataclass
class StageOutcome:
    """What a stage decided. next_state goes through the state machine; next_stage
    (if any) is enqueued as the following job. checkpoint is persisted on the run."""

    next_state: RunState | None = None
    next_stage: str | None = None
    checkpoint: dict = field(default_factory=dict)
    needs_human: bool = False  # marks the job status REVIEW instead of COMPLETE


StageFn = Callable[[StageContext], Awaitable[StageOutcome]]

_registry: dict[str, StageFn] = {}


def register_stage(name: str):
    def decorator(fn: StageFn) -> StageFn:
        _registry[name] = fn
        return fn

    return decorator


def get_stage(name: str) -> StageFn:
    if name not in _registry:
        raise KeyError(f"no stage handler registered for '{name}'")
    return _registry[name]


def registered_stages() -> list[str]:
    return sorted(_registry)


async def process_next(worker_id: str = "worker-1") -> bool:
    """Claim and execute one job. Returns False when the queue is empty."""
    async with session_scope() as session:
        job_repo = JobRepo(session)
        run_repo = RunRepo(session)
        job = await job_repo.claim_next(worker_id)
        if job is None:
            return False

        run = await run_repo.get(job.run_id)
        if run is None:
            await job_repo.finish(job, JobStatus.FAILED, error="run not found")
            return True

        ctx = StageContext(session=session, run=run, job=job, run_repo=run_repo, job_repo=job_repo)
        try:
            handler = get_stage(job.stage)
            outcome = await handler(ctx)
        except Exception as exc:  # noqa: BLE001 — failure is a first-class event
            logger.exception("stage %s failed for run %s", job.stage, run.id)
            await job_repo.finish(job, JobStatus.FAILED, error=str(exc)[:2000])
            await emit_event(
                session, actor="system", action="stage.failed", run_id=run.id, job_id=job.id,
                detail={"stage": job.stage, "attempt": job.attempt, "error": str(exc)[:500]},
            )
            if job.attempt < MAX_ATTEMPTS:
                await job_repo.enqueue(run.id, job.stage, attempt=job.attempt + 1)
            else:
                # repeated failure → quarantine, never silent loss (design §1 failure-first)
                if run.state != RunState.QUARANTINED:
                    await run_repo.transition(
                        run, RunState.QUARANTINED,
                        detail={"reason": "max attempts exhausted", "stage": job.stage},
                    )
            return True

        if outcome.checkpoint:
            await run_repo.save_checkpoint(run, outcome.checkpoint)
        if outcome.next_state is not None and outcome.next_state != run.state:
            await run_repo.transition(run, outcome.next_state, detail={"stage": job.stage})
        if outcome.next_stage is not None:
            await job_repo.enqueue(run.id, outcome.next_stage)
        await job_repo.finish(job, JobStatus.REVIEW if outcome.needs_human else JobStatus.COMPLETE)
        await emit_event(
            session, actor="system", action="stage.complete", run_id=run.id, job_id=job.id,
            detail={"stage": job.stage, "next_stage": outcome.next_stage,
                    "next_state": outcome.next_state.value if outcome.next_state else None},
        )
        return True


async def worker_loop(stop: asyncio.Event, worker_id: str = "worker-1",
                      idle_sleep: float = 1.0) -> None:
    """In-process worker (single container deployment). Drains the queue, then naps."""
    while not stop.is_set():
        try:
            had_work = await process_next(worker_id)
        except Exception:  # noqa: BLE001
            logger.exception("worker loop iteration failed")
            had_work = False
        if not had_work:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
            except TimeoutError:
                pass
