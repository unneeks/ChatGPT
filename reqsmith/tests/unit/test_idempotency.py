"""Design rule under test: re-running anything never duplicates side effects."""

from sqlalchemy import func, select

from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import AuditEvent, OutreachEvent, Run
from reqsmith.persistence.repo import ApprovalRepo, ArtifactRepo, OutreachRepo, RtmRepo, RunRepo


async def test_replayed_trigger_creates_one_run(sample_webhook_payload):
    async with session_scope() as session:
        repo = RunRepo(session)
        run1, created1 = await repo.create_run("BANK-101", sample_webhook_payload)
        run2, created2 = await repo.create_run("BANK-101", sample_webhook_payload)
        assert created1 is True and created2 is False
        assert run1.id == run2.id
        count = await session.scalar(select(func.count()).select_from(Run))
        assert count == 1


async def test_different_trigger_payload_creates_new_run(sample_webhook_payload):
    async with session_scope() as session:
        repo = RunRepo(session)
        _, created1 = await repo.create_run("BANK-101", sample_webhook_payload)
        changed = {**sample_webhook_payload, "webhookEvent": "jira:issue_updated"}
        _, created2 = await repo.create_run("BANK-101", changed)
        assert created1 and created2


async def test_outreach_double_send_collapses(sample_webhook_payload):
    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        from reqsmith.persistence.models import Question

        session.add(Question(run_id=run.id, question_id="q-1", text="Which KYC tiers apply?"))
        await session.flush()

        outreach = OutreachRepo(session)
        kwargs = dict(question_id="q-1", channel="teams_card", direction="out",
                      payload={"text": "Which KYC tiers apply?"}, rung=2)
        _, first = await outreach.record(**kwargs)
        _, second = await outreach.record(**kwargs)
        assert first is True and second is False
        count = await session.scalar(select(func.count()).select_from(OutreachEvent))
        assert count == 1


async def test_artifact_write_once_versions(sample_webhook_payload):
    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        artifacts = ArtifactRepo(session)
        a1 = await artifacts.write_once(
            run_id=run.id, kind="draft_story", content={"title": "v1"},
            prompt_version="analyst_v1", model_id="m",
        )
        a2 = await artifacts.write_once(
            run_id=run.id, kind="draft_story", content={"title": "v2"},
            prompt_version="analyst_v1", model_id="m",
        )
        assert (a1.version, a2.version) == (1, 2)
        assert a1.content == {"title": "v1"}  # prior version untouched


async def test_approval_double_decision_collapses(sample_webhook_payload):
    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        artifact = await ArtifactRepo(session).write_once(
            run_id=run.id, kind="draft_story", content={}, prompt_version="v", model_id="m",
        )
        approvals = ApprovalRepo(session)
        kwargs = dict(run_id=run.id, artifact_id=artifact.id, role="maker",
                      decision="approve", reviewer_identity="alice@bank.com")
        _, first = await approvals.record(**kwargs)
        _, second = await approvals.record(**kwargs)  # console + Jira webhook dual channel
        assert first is True and second is False


async def test_rtm_edge_unique(sample_webhook_payload):
    async with session_scope() as session:
        rtm = RtmRepo(session)
        kwargs = dict(from_type="story", from_ref="BANK-102", to_type="epic",
                      to_ref="BANK-101", edge_kind="derives_from")
        _, first = await rtm.link(**kwargs)
        _, second = await rtm.link(**kwargs)
        assert first is True and second is False


async def test_audit_events_written_for_run_creation(sample_webhook_payload):
    async with session_scope() as session:
        run, _ = await RunRepo(session).create_run("BANK-101", sample_webhook_payload)
        events = list(await session.scalars(
            select(AuditEvent).where(AuditEvent.run_id == run.id)
        ))
        assert any(e.action == "run.created" for e in events)
        assert all(e.policy_version for e in events if e.action == "run.created")
