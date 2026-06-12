from sqlalchemy import func, select

from reqsmith.orchestrator import engine
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import AuditEvent, JobStatus, RunState
from reqsmith.persistence.repo import JobRepo, RunRepo


async def _make_run_with_job(payload, stage):
    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", payload)
        job, _ = await JobRepo(session).enqueue(run.id, stage)
        return run.id, job.id


async def test_stage_executes_transitions_and_chains(sample_webhook_payload, monkeypatch):
    monkeypatch.setattr(engine, "_registry", {})

    @register_stage("triage")
    async def triage(ctx: StageContext) -> StageOutcome:
        return StageOutcome(next_state=RunState.TRIAGE, next_stage="noop",
                            checkpoint={"step": "triaged"})

    @register_stage("noop")
    async def noop(ctx: StageContext) -> StageOutcome:
        return StageOutcome()

    run_id, _ = await _make_run_with_job(sample_webhook_payload, "triage")

    assert await engine.process_next() is True   # triage
    assert await engine.process_next() is True   # chained noop
    assert await engine.process_next() is False  # queue drained

    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        assert run.state == RunState.TRIAGE
        assert run.checkpoint == {"step": "triaged"}
        jobs = await JobRepo(session).for_run(run_id)
        assert [j.status for j in jobs] == [JobStatus.COMPLETE, JobStatus.COMPLETE]
        events = list(await session.scalars(
            select(AuditEvent).where(AuditEvent.run_id == run_id)
        ))
        assert sum(1 for e in events if e.action == "stage.complete") == 2
        assert any(e.action == "run.transition" for e in events)


async def test_failing_stage_retries_then_quarantines(sample_webhook_payload, monkeypatch):
    monkeypatch.setattr(engine, "_registry", {})
    attempts = []

    @register_stage("triage")
    async def always_fails(ctx: StageContext) -> StageOutcome:
        attempts.append(ctx.job.attempt)
        raise RuntimeError("boom")

    run_id, _ = await _make_run_with_job(sample_webhook_payload, "triage")

    while await engine.process_next():
        pass

    assert attempts == [1, 2, 3]  # MAX_ATTEMPTS
    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        assert run.state == RunState.QUARANTINED
        events = list(await session.scalars(
            select(AuditEvent).where(AuditEvent.run_id == run_id)
        ))
        assert sum(1 for e in events if e.action == "stage.failed") == 3


async def test_unregistered_stage_fails_safely(sample_webhook_payload, monkeypatch):
    monkeypatch.setattr(engine, "_registry", {})
    run_id, job_id = await _make_run_with_job(sample_webhook_payload, "ghost-stage")

    while await engine.process_next():
        pass

    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        assert run.state == RunState.QUARANTINED


async def test_audit_count_stable_under_replay(sample_webhook_payload, monkeypatch):
    """Replaying the same trigger then draining must not inflate the ledger."""
    monkeypatch.setattr(engine, "_registry", {})

    @register_stage("triage")
    async def triage(ctx: StageContext) -> StageOutcome:
        return StageOutcome(next_state=RunState.TRIAGE)

    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        await JobRepo(session).enqueue(run.id, "triage")
        # replay: same trigger, same stage enqueue
        await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        await JobRepo(session).enqueue(run.id, "triage")

    while await engine.process_next():
        pass

    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "stage.complete"
            )
        )
        assert count == 1  # one job, executed once
