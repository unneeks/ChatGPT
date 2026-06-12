"""Repositories. All state changes to runs go through RunRepo.transition() which
enforces the state machine; audit/outreach writers are INSERT-only."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith.audit.ledger import emit_event
from reqsmith.orchestrator.state_machine import assert_transition
from reqsmith.persistence.idempotency import content_hash, insert_or_get
from reqsmith.persistence.models import (
    Approval,
    Artifact,
    GateResult,
    Job,
    JobStatus,
    OutreachEvent,
    RtmEdge,
    Run,
    RunState,
    SystemFlag,
)
from reqsmith.settings import get_settings


class RunRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_run(self, jira_issue_key: str, trigger_payload: dict) -> tuple[Run, bool]:
        """Idempotent: same issue + same trigger content → same run (no duplicate)."""
        settings = get_settings()
        key = f"{jira_issue_key}:{content_hash(trigger_payload)[:32]}"
        run = Run(
            jira_issue_key=jira_issue_key,
            state=RunState.INTAKE,
            prompt_pack_version=settings.prompt_pack_version,
            policy_version=settings.policy_pack_version,
            idempotency_key=key,
        )
        run, created = await insert_or_get(self.session, run, Run, Run.idempotency_key, key)
        if created:
            await emit_event(
                self.session,
                actor="system",
                action="run.created",
                run_id=run.id,
                input_payload=trigger_payload,
                policy_version=settings.policy_pack_version,
                detail={"jira_issue_key": jira_issue_key},
            )
        return run, created

    async def get(self, run_id: str) -> Run | None:
        return await self.session.get(Run, run_id)

    async def transition(self, run: Run, target: RunState, *, actor: str = "system",
                         detail: dict | None = None) -> Run:
        """The only legal way to change run.state. Illegal transitions raise."""
        assert_transition(run.state, target)
        previous = run.state
        run.state = target
        await self.session.flush()
        await emit_event(
            self.session,
            actor=actor,
            action="run.transition",
            run_id=run.id,
            detail={"from": previous.value, "to": target.value, **(detail or {})},
        )
        return run

    async def save_checkpoint(self, run: Run, checkpoint: dict) -> None:
        run.checkpoint = checkpoint
        await self.session.flush()


class JobRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def enqueue(self, run_id: str, stage: str, attempt: int = 1) -> tuple[Job, bool]:
        job = Job(run_id=run_id, stage=stage, attempt=attempt, status=JobStatus.QUEUED)
        # (run_id, stage, attempt) unique → re-enqueue collapses
        existing = await self.session.scalar(
            select(Job).where(Job.run_id == run_id, Job.stage == stage, Job.attempt == attempt)
        )
        if existing is not None:
            return existing, False
        job, created = await insert_or_get(self.session, job, Job, Job.id, job.id)
        return job, created

    async def claim_next(self, worker_id: str) -> Job | None:
        """Claim one queued job. Postgres: FOR UPDATE SKIP LOCKED; harmless on SQLite."""
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.created_at)
            .limit(1)
        )
        if self.session.bind and self.session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        job = await self.session.scalar(stmt)
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.locked_by = worker_id
        job.locked_at = datetime.now(UTC)
        await self.session.flush()
        return job

    async def finish(self, job: Job, status: JobStatus, error: str | None = None) -> Job:
        job.status = status
        job.error = error
        job.locked_by = None
        await self.session.flush()
        return job

    async def get(self, job_id: str) -> Job | None:
        return await self.session.get(Job, job_id)

    async def for_run(self, run_id: str) -> list[Job]:
        rows = await self.session.scalars(
            select(Job).where(Job.run_id == run_id).order_by(Job.created_at)
        )
        return list(rows)


class ArtifactRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def write_once(
        self,
        *,
        run_id: str,
        kind: str,
        content: dict,
        prompt_version: str,
        model_id: str,
    ) -> Artifact:
        """Write-once: a new write of the same kind becomes the next version row."""
        settings = get_settings()
        latest = await self.session.scalar(
            select(Artifact)
            .where(Artifact.run_id == run_id, Artifact.kind == kind)
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        version = (latest.version + 1) if latest else 1
        artifact = Artifact(
            run_id=run_id,
            kind=kind,
            version=version,
            content=content,
            content_hash=content_hash(content),
            prompt_version=prompt_version,
            model_id=model_id,
            policy_version=settings.policy_pack_version,
        )
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def latest(self, run_id: str, kind: str) -> Artifact | None:
        return await self.session.scalar(
            select(Artifact)
            .where(Artifact.run_id == run_id, Artifact.kind == kind)
            .order_by(Artifact.version.desc())
            .limit(1)
        )


class OutreachRepo:
    """INSERT-only writer for outreach_events; idempotency key collapses retries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        *,
        question_id: str,
        channel: str,
        direction: str,
        payload: dict,
        rung: int,
        attempt: int = 1,
        external_message_id: str | None = None,
    ) -> tuple[OutreachEvent, bool]:
        key = f"{question_id}:{rung}:{attempt}:{direction}:{channel}"
        event = OutreachEvent(
            question_id=question_id,
            channel=channel,
            direction=direction,
            payload_hash=content_hash(payload),
            external_message_id=external_message_id,
            idempotency_key=key,
        )
        return await insert_or_get(
            self.session, event, OutreachEvent, OutreachEvent.idempotency_key, key
        )


class GateResultRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, **kwargs) -> GateResult:
        result = GateResult(**kwargs)
        self.session.add(result)
        await self.session.flush()
        return result


class ApprovalRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        *,
        run_id: str,
        artifact_id: str,
        role: str,
        decision: str,
        reviewer_identity: str,
        diff: dict | None = None,
        jira_transition_id: str | None = None,
    ) -> tuple[Approval, bool]:
        approval = Approval(
            run_id=run_id,
            artifact_id=artifact_id,
            role=role,
            decision=decision,
            reviewer_identity=reviewer_identity,
            diff=diff,
            jira_transition_id=jira_transition_id,
        )
        # dual-channel reconciliation: Jira webhook and console decision collapse into one row
        existing = await self.session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.artifact_id == artifact_id,
                Approval.role == role,
                Approval.reviewer_identity == reviewer_identity,
            )
        )
        if existing is not None:
            return existing, False
        return await insert_or_get(self.session, approval, Approval, Approval.id, approval.id)

    async def makers_for(self, run_id: str, artifact_id: str) -> set[str]:
        rows = await self.session.scalars(
            select(Approval.reviewer_identity).where(
                Approval.run_id == run_id,
                Approval.artifact_id == artifact_id,
                Approval.role == "maker",
            )
        )
        return set(rows)


class RtmRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def link(
        self, *, from_type: str, from_ref: str, to_type: str, to_ref: str, edge_kind: str,
        run_id: str | None = None,
    ) -> tuple[RtmEdge, bool]:
        edge = RtmEdge(
            from_type=from_type, from_ref=from_ref, to_type=to_type, to_ref=to_ref,
            edge_kind=edge_kind, run_id=run_id,
        )
        existing = await self.session.scalar(
            select(RtmEdge).where(
                RtmEdge.from_type == from_type, RtmEdge.from_ref == from_ref,
                RtmEdge.to_type == to_type, RtmEdge.to_ref == to_ref,
                RtmEdge.edge_kind == edge_kind,
            )
        )
        if existing is not None:
            return existing, False
        return await insert_or_get(self.session, edge, RtmEdge, RtmEdge.id, edge.id)


class FlagRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def set(self, key: str, value: str, enabled: bool) -> SystemFlag:
        flag = await self.session.get(SystemFlag, key)
        if flag is None:
            flag = SystemFlag(key=key, value=value, enabled=enabled)
            self.session.add(flag)
        else:
            flag.value = value
            flag.enabled = enabled
        await self.session.flush()
        return flag

    async def get(self, key: str) -> SystemFlag | None:
        return await self.session.get(SystemFlag, key)
