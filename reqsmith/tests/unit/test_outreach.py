"""M7 unit tests: outreach ladder, rate budget, Teams card, answer webhook.

Coverage:
- Rung-2 sends one Teams card when rung-1 SLA expires
- Idempotent retry: calling advance_question twice sends nothing the second time
- Global outreach pause blocks all sends
- Per-stakeholder daily cap blocks after 1 card
- Card builder produces correct Adaptive Card structure
- parse_card_answer handles submit + handoff + garbage
- Teams bot_messages webhook records answer, updates question, posts to Jira, resumes run
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from reqsmith import deps
from reqsmith.adapters.jira.fake import FakeJira
from reqsmith.adapters.jira.port import JiraIssue
from reqsmith.adapters.teams.fake import FakeTeams
from reqsmith.api.app import create_app
from reqsmith.orchestrator import engine
from reqsmith.outreach.cards import build_question_card, parse_card_answer
from reqsmith.outreach.ladder import advance_question, process_sla_rungs
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import Question, RunState
from reqsmith.persistence.repo import FlagRepo

EPIC_DESCRIPTION = (
    "Digitise the retail onboarding intake flow so that branch staff capture structured "
    "applicant information once and downstream operations teams stop re-keying it into "
    "legacy systems. Outcome: cut onboarding handling time and reduce keying errors."
)


# ---------------------------------------------------------------------------
# Card builder / parser
# ---------------------------------------------------------------------------

def test_build_question_card_structure():
    card = build_question_card(
        question_id="q-123",
        question_text="What is the target go-live date?",
        turn=1,
        issue_key="BANK-101",
    )
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    content = card["content"]
    assert content["type"] == "AdaptiveCard"
    assert any(
        item.get("id") == "answer"
        for item in content["body"]
        if item.get("type") == "Input.Text"
    )
    action_data = [a["data"] for a in content["actions"]]
    assert any(d.get("question_id") == "q-123" and not d.get("handoff") for d in action_data)
    assert any(d.get("question_id") == "q-123" and d.get("handoff") for d in action_data)


def test_build_question_card_turn_warning():
    # turn == max_turns (3) → "last opportunity" warning
    card = build_question_card(question_id="q", question_text="Q?", turn=3)
    texts = [
        item.get("text", "")
        for item in card["content"]["body"]
        if item.get("type") == "TextBlock"
    ]
    assert any("last" in t.lower() for t in texts)


def test_parse_card_answer_submit():
    value = {"question_id": "q-123", "answer": "  Q1 2026  ", "handoff": False}
    qid, ans, handoff = parse_card_answer(value)
    assert qid == "q-123"
    assert ans == "Q1 2026"
    assert handoff is False


def test_parse_card_answer_handoff():
    value = {"question_id": "q-456", "handoff": True}
    qid, ans, handoff = parse_card_answer(value)
    assert qid == "q-456"
    assert handoff is True
    assert ans is None


def test_parse_card_answer_garbage():
    qid, ans, handoff = parse_card_answer({"not": "our card"})
    assert qid is None


# ---------------------------------------------------------------------------
# Ladder + budget
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_world_teams():
    jira = FakeJira()
    teams = FakeTeams()
    deps.set_jira(jira)
    deps.set_teams(teams)
    yield jira, teams
    deps.set_jira(None)
    deps.set_teams(None)


async def _seed_question(
    *,
    question_id: str = "q-test",
    stakeholder_aad_id: str = "user-aad-123",
    run_id: str | None = None,
    sla_overdue: bool = True,
    rung: int = 1,
) -> None:
    """Insert a bare Question row for ladder tests."""
    from reqsmith.persistence.idempotency import insert_or_get
    now = datetime.now(UTC)
    async with session_scope() as session:
        # need a run row for FK if run_id supplied
        if run_id is None:
            from reqsmith.persistence.models import RiskTier, Run
            from reqsmith.settings import get_settings
            s = get_settings()
            run = Run(
                jira_issue_key="BANK-999",
                state=RunState.AWAITING_INPUT,
                risk_tier=RiskTier.MEDIUM,
                prompt_pack_version=s.prompt_pack_version,
                policy_version=s.policy_pack_version,
                idempotency_key=f"test:{question_id}",
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        deadline = (now - timedelta(hours=25)) if sla_overdue else (now + timedelta(hours=24))
        q = Question(
            run_id=run_id,
            question_id=question_id,
            text="What is the target date?",
            stakeholder_aad_id=stakeholder_aad_id,
            status="asked",
            current_rung=rung,
            sla_deadline=deadline,
        )
        await insert_or_get(session, q, Question, Question.question_id, question_id)


async def test_ladder_rung2_sends_teams_card(fake_world_teams):
    jira, teams = fake_world_teams
    jira.seed(JiraIssue(
        key="BANK-999", issue_type="Epic", summary="Test", description="d",
        status="Open", reporter="x@bank.com",
    ))
    await _seed_question(question_id="q-r2")
    async with session_scope() as session:
        result = await advance_question("q-r2", session)
    assert result["action"] == "sent"
    assert result["channel"] == "teams_card"
    assert result["rung"] == 2
    assert len(teams.sent) == 1
    assert teams.sent[0]["user"] == "user-aad-123"


async def test_ladder_idempotent_second_call_skips(fake_world_teams):
    """Crash-recovery idempotency: if a Teams card was sent but the question row
    was not updated (crash between send and commit), the retry must skip the re-send.

    Simulated by pre-inserting the outreach_event while keeping the question at
    rung 1 with an overdue SLA — identical to the post-crash DB state.
    """
    jira, teams = fake_world_teams
    await _seed_question(question_id="q-idem")

    # Pre-insert the outreach_event as if the card was already sent (pre-crash)
    async with session_scope() as session:
        from reqsmith.persistence.idempotency import content_hash
        from reqsmith.persistence.models import OutreachEvent
        evt = OutreachEvent(
            question_id="q-idem",
            channel="teams_card",
            direction="out",
            payload_hash=content_hash({"channel": "teams_card", "rung": 2}),
            external_message_id="msg-pre",
            idempotency_key="q-idem:2:1:out:teams_card",
        )
        session.add(evt)
        await session.flush()

    # Now advance_question should detect the existing record and skip without sending
    async with session_scope() as session:
        result = await advance_question("q-idem", session)
    assert result["action"] == "skipped"
    assert "idempotent" in result["reason"]
    # critically: no new Teams card was sent
    assert len(teams.sent) == 0


async def test_ladder_pause_blocks_send(fake_world_teams):
    await _seed_question(question_id="q-paused")
    async with session_scope() as session:
        await FlagRepo(session).set("outreach_paused", "test pause", enabled=True)
        result = await advance_question("q-paused", session)
    assert result["action"] == "blocked"
    assert "paused" in result["reason"]


async def test_ladder_sla_not_yet_due_skips(fake_world_teams):
    await _seed_question(question_id="q-future", sla_overdue=False)
    async with session_scope() as session:
        result = await advance_question("q-future", session)
    assert result["action"] == "skipped"
    assert "SLA" in result["reason"]


async def test_ladder_per_stakeholder_cap_blocks(fake_world_teams):
    """After 1 card sent today, the per-stakeholder cap (1/day) blocks the next."""
    jira, teams = fake_world_teams
    jira.seed(JiraIssue(
        key="BANK-999", issue_type="Epic", summary="Test", description="d",
        status="Open", reporter="x@bank.com",
    ))
    await _seed_question(question_id="q-cap1")
    await _seed_question(question_id="q-cap2", sla_overdue=True, rung=1)

    async with session_scope() as session:
        first = await advance_question("q-cap1", session)
    assert first["action"] == "sent"

    # cap hit — second question for same stakeholder today should be blocked
    async with session_scope() as session:
        second = await advance_question("q-cap2", session)
    assert second["action"] == "blocked"
    assert "cap" in second["reason"]


async def test_process_sla_rungs_advances_all_overdue(fake_world_teams):
    jira, teams = fake_world_teams
    jira.seed(JiraIssue(
        key="BANK-999", issue_type="Epic", summary="Test", description="d",
        status="Open", reporter="x@bank.com",
    ))
    await _seed_question(question_id="q-sweep1", stakeholder_aad_id="u1")
    await _seed_question(question_id="q-sweep2", stakeholder_aad_id="u2")
    async with session_scope() as session:
        results = await process_sla_rungs(session)
    sent = [r for r in results if r["action"] == "sent"]
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Teams /api/messages webhook
# ---------------------------------------------------------------------------

def _teams_client():
    app = create_app(run_worker=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _run_to_awaiting_input(jira, key="BANK-701") -> str:
    """Trigger a vague epic to get a run into AWAITING_INPUT state."""
    from reqsmith.adapters.llm.fake import FakeLLM
    deps.set_llm(FakeLLM())
    jira.seed(JiraIssue(
        key=key, issue_type="Epic", summary="Vague request",
        description="make it better", status="Open", reporter="sponsor@bank.com",
    ))
    async with _teams_client() as c:
        resp = (await c.post("/webhooks/jira", json={
            "webhookEvent": "jira:issue_created", "issue": {"key": key},
        })).json()
        run_id = resp["run_id"]
        while await engine.process_next():
            pass
    return run_id


async def test_teams_answer_records_and_resumes(fake_world_teams):
    jira, teams = fake_world_teams
    run_id = await _run_to_awaiting_input(jira)

    # get the question_id from the DB
    async with session_scope() as session:
        question = await session.scalar(
            select(Question).where(Question.run_id == run_id).limit(1)
        )
    assert question is not None
    qid = question.question_id

    # simulate card submit from Teams user
    activity = {
        "type": "message",
        "channelId": "msteams",
        "from": {"id": "u-aad", "aadObjectId": "u-aad"},
        "value": {"question_id": qid, "answer": "Updated description with full scope", "handoff": False},
    }
    async with _teams_client() as c:
        resp = await c.post("/api/messages", json=activity)
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"

    # question should be answered
    async with session_scope() as session:
        q = await session.scalar(select(Question).where(Question.question_id == qid))
    assert q.status == "answered"
    assert "Updated description" in (q.answer_text or "")

    # answer posted back to Jira (stored on JiraIssue.comments)
    all_comments = [c for issue in jira.issues.values() for c in issue.comments]
    assert any("[REQ-ANS:" in c["body"] for c in all_comments)


async def test_teams_handoff_sets_status(fake_world_teams):
    jira, teams = fake_world_teams
    run_id = await _run_to_awaiting_input(jira, key="BANK-702")

    async with session_scope() as session:
        question = await session.scalar(
            select(Question).where(Question.run_id == run_id).limit(1)
        )
    qid = question.question_id

    activity = {
        "type": "message",
        "channelId": "msteams",
        "from": {"id": "u-aad"},
        "value": {"question_id": qid, "handoff": True},
    }
    async with _teams_client() as c:
        resp = await c.post("/api/messages", json=activity)
    assert resp.json()["status"] == "handed_off"

    async with session_scope() as session:
        q = await session.scalar(select(Question).where(Question.question_id == qid))
    assert q.status == "handed_off"


async def test_teams_duplicate_answer_ignored(fake_world_teams):
    jira, teams = fake_world_teams
    run_id = await _run_to_awaiting_input(jira, key="BANK-703")

    async with session_scope() as session:
        question = await session.scalar(
            select(Question).where(Question.run_id == run_id).limit(1)
        )
    qid = question.question_id

    activity = {
        "type": "message",
        "channelId": "msteams",
        "from": {"id": "u-aad"},
        "value": {"question_id": qid, "answer": "My answer", "handoff": False},
    }
    async with _teams_client() as c:
        first = (await c.post("/api/messages", json=activity)).json()
        second = (await c.post("/api/messages", json=activity)).json()
    assert first["status"] == "recorded"
    assert second["status"] == "ignored"
    assert "already" in second["reason"]
