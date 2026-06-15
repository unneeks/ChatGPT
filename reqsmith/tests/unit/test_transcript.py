"""M9 unit tests: transcript ingestion pipeline.

Coverage:
- parse_vtt: basic cues, empty input, cue identifiers, cue settings on timestamp line
- find_answer_segments: keyword match, no match, threshold behaviour
- ingest_transcript: closes matching questions, records SourceDocument, posts Jira comment
- ingest_transcript: unmatched questions left open, unknown run returns error
- ingest_transcript: idempotent (same VTT ingested twice closes nothing the second time)
- fetch_and_ingest_transcript: Graph returns transcript → ingested
- fetch_and_ingest_transcript: Graph returns None → unavailable (not an error)
- Admin upload endpoint: 200 on valid VTT, 422 on empty segments
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from reqsmith import deps
from reqsmith.adapters.graph.fake import FakeGraph
from reqsmith.adapters.jira.fake import FakeJira
from reqsmith.adapters.jira.port import JiraIssue
from reqsmith.api.app import create_app
from reqsmith.outreach.transcript import (
    TranscriptSegment,
    fetch_and_ingest_transcript,
    find_answer_segments,
    ingest_transcript,
    parse_vtt,
)
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.idempotency import insert_or_get
from reqsmith.persistence.models import Question, RiskTier, Run, RunState, SourceDocument
from reqsmith.settings import get_settings

# ---------------------------------------------------------------------------
# Sample VTT fixture
# ---------------------------------------------------------------------------

SAMPLE_VTT = """\
WEBVTT

00:00:01.000 --> 00:00:05.000
Alice: The target go-live date for the onboarding module is March 15th next year.

00:00:06.000 --> 00:00:10.000
Bob: Compliance requirements are documented in section 4.2 of the handbook.

00:00:11.000 --> 00:00:15.000
Alice: We should also define the data retention policy before launch.

00:00:16.000 --> 00:00:20.000
Bob: Agreed. The weather forecast is sunny with no compliance clouds.
"""


# ---------------------------------------------------------------------------
# parse_vtt
# ---------------------------------------------------------------------------

def test_parse_vtt_basic():
    segments = parse_vtt(SAMPLE_VTT)
    assert len(segments) == 4
    assert segments[0].start == "00:00:01.000"
    assert segments[0].end == "00:00:05.000"
    assert "March 15th" in segments[0].text


def test_parse_vtt_empty_input():
    assert parse_vtt("") == []
    assert parse_vtt("WEBVTT\n\n") == []


def test_parse_vtt_with_cue_identifier():
    vtt = """\
WEBVTT

cue-1
00:00:01.000 --> 00:00:03.000
Hello world from the meeting.
"""
    segments = parse_vtt(vtt)
    assert len(segments) == 1
    assert segments[0].text == "Hello world from the meeting."


def test_parse_vtt_with_cue_settings():
    # cue settings appear after the end timestamp on the same line
    vtt = """\
WEBVTT

00:00:01.000 --> 00:00:03.000 align:left position:10%
Speaker said something important.
"""
    segments = parse_vtt(vtt)
    assert len(segments) == 1
    assert segments[0].end == "00:00:03.000"  # settings stripped
    assert "important" in segments[0].text


# ---------------------------------------------------------------------------
# find_answer_segments
# ---------------------------------------------------------------------------

def test_find_answer_segments_matches():
    segments = parse_vtt(SAMPLE_VTT)
    question = "What is the target go-live date for the onboarding module?"
    matched = find_answer_segments(question, segments)
    assert len(matched) >= 1
    assert any("March 15th" in s.text for s in matched)


def test_find_answer_segments_no_match():
    segments = [TranscriptSegment("00:00:01.000", "00:00:02.000", "Rain falls down today")]
    matched = find_answer_segments("What is the go-live date?", segments, threshold=2)
    assert matched == []


def test_find_answer_segments_threshold():
    # segment has exactly 1 keyword overlap — below default threshold of 2
    segments = [TranscriptSegment("00:00:01.000", "00:00:02.000", "onboarding starts soon")]
    matched = find_answer_segments(
        "What are the onboarding compliance requirements?", segments, threshold=2
    )
    # "onboarding" matches but only 1 keyword → below threshold
    assert matched == []


# ---------------------------------------------------------------------------
# Fixtures for DB-backed tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_world_transcript(monkeypatch):
    jira = FakeJira()
    graph = FakeGraph()
    deps.set_jira(jira)
    deps.set_graph(graph)
    monkeypatch.setenv("HUMAN_OWNER_AAD_ID", "owner-aad")
    get_settings.cache_clear()
    yield jira, graph
    deps.set_jira(None)
    deps.set_graph(None)
    get_settings.cache_clear()


async def _seed_run_with_questions(
    *,
    run_id_hint: str,
    issue_key: str = "BANK-900",
    questions: list[tuple[str, str]],  # [(question_id, question_text), ...]
) -> str:
    """Returns run_id."""
    settings = get_settings()
    async with session_scope() as session:
        run = Run(
            jira_issue_key=issue_key,
            state=RunState.AWAITING_INPUT,
            risk_tier=RiskTier.MEDIUM,
            prompt_pack_version=settings.prompt_pack_version,
            policy_version=settings.policy_pack_version,
            idempotency_key=f"test-transcript:{run_id_hint}",
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        for q_id, q_text in questions:
            q = Question(
                run_id=run_id,
                question_id=q_id,
                text=q_text,
                status="asked",
                current_rung=2,
                sla_deadline=datetime.now(UTC) - timedelta(hours=1),
            )
            await insert_or_get(session, q, Question, Question.question_id, q_id)
    return run_id


# ---------------------------------------------------------------------------
# ingest_transcript
# ---------------------------------------------------------------------------

async def test_ingest_closes_matching_question(fake_world_transcript):
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    run_id = await _seed_run_with_questions(
        run_id_hint="close-match",
        questions=[("q-tm1", "What is the target go-live date for the onboarding module?")],
    )
    async with session_scope() as session:
        result = await ingest_transcript(run_id, SAMPLE_VTT, session)

    assert result["questions_closed"] == 1
    assert result["results"][0]["action"] == "answered"

    async with session_scope() as session:
        q = await session.scalar(select(Question).where(Question.question_id == "q-tm1"))
    assert q.status == "answered"
    assert q.answer_text is not None
    assert "00:00:01.000" in q.answer_text  # timestamp citation

    # Jira comment posted with REQ-ANS marker and question_id
    ans_comments = jira.comments_containing("BANK-900", "REQ-ANS")
    assert any("q-tm1" in c["body"] for c in ans_comments), "Expected [REQ-ANS:q-tm1] Jira comment"


async def test_ingest_leaves_unmatched_question_open(fake_world_transcript):
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    run_id = await _seed_run_with_questions(
        run_id_hint="no-match",
        questions=[("q-nm1", "Completely unrelated question about spaceship fuel?")],
    )
    async with session_scope() as session:
        result = await ingest_transcript(run_id, SAMPLE_VTT, session)

    assert result["questions_closed"] == 0
    assert result["results"][0]["action"] == "no_match"

    async with session_scope() as session:
        q = await session.scalar(select(Question).where(Question.question_id == "q-nm1"))
    assert q.status == "asked"


async def test_ingest_creates_source_document(fake_world_transcript):
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    run_id = await _seed_run_with_questions(
        run_id_hint="src-doc",
        questions=[("q-sd1", "What is the target go-live date for the onboarding module?")],
    )
    async with session_scope() as session:
        await ingest_transcript(run_id, SAMPLE_VTT, session, event_id="evt-123")

    async with session_scope() as session:
        doc = await session.scalar(
            select(SourceDocument).where(
                SourceDocument.run_id == run_id,
                SourceDocument.origin == "transcript",
            )
        )
    assert doc is not None
    assert doc.external_ref == "evt-123"


async def test_ingest_idempotent(fake_world_transcript):
    """Second ingest of the same VTT skips already-answered questions."""
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    run_id = await _seed_run_with_questions(
        run_id_hint="idem",
        questions=[("q-id1", "What is the target go-live date for the onboarding module?")],
    )
    async with session_scope() as session:
        first = await ingest_transcript(run_id, SAMPLE_VTT, session)
    assert first["questions_closed"] == 1

    async with session_scope() as session:
        second = await ingest_transcript(run_id, SAMPLE_VTT, session)
    # question already answered → status no longer "open"/"asked" → 0 closed
    assert second["questions_closed"] == 0


async def test_ingest_unknown_run(fake_world_transcript):
    async with session_scope() as session:
        result = await ingest_transcript("nonexistent-run-id", SAMPLE_VTT, session)
    assert "error" in result
    assert result["questions_closed"] == 0


# ---------------------------------------------------------------------------
# fetch_and_ingest_transcript
# ---------------------------------------------------------------------------

async def test_fetch_ingests_when_transcript_available(fake_world_transcript):
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    graph.seed_transcript("evt-999", SAMPLE_VTT)
    run_id = await _seed_run_with_questions(
        run_id_hint="fetch-ok",
        questions=[("q-fo1", "What is the target go-live date for the onboarding module?")],
    )
    async with session_scope() as session:
        result = await fetch_and_ingest_transcript(run_id, "evt-999", "owner-aad", session)

    assert result["action"] == "ingested"
    assert result["questions_closed"] == 1


async def test_fetch_returns_unavailable_when_graph_returns_none(fake_world_transcript):
    jira, graph = fake_world_transcript
    run_id = await _seed_run_with_questions(
        run_id_hint="fetch-unavail",
        questions=[("q-ua1", "Any question?")],
    )
    # No transcript seeded → FakeGraph.get_meeting_transcript returns None
    async with session_scope() as session:
        result = await fetch_and_ingest_transcript(run_id, "evt-missing", "owner-aad", session)

    assert result["action"] == "unavailable"
    assert "reason" in result


# ---------------------------------------------------------------------------
# Admin upload endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(fake_world_transcript):
    app = create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_admin_upload_endpoint_success(admin_client, fake_world_transcript):
    jira, graph = fake_world_transcript
    jira.seed(JiraIssue(
        key="BANK-900", issue_type="Epic", summary="T", description="d",
        status="Open", reporter="x@bank.com",
    ))
    run_id = await _seed_run_with_questions(
        run_id_hint="admin-up",
        questions=[("q-au1", "What is the target go-live date for the onboarding module?")],
    )
    async with admin_client as client:
        resp = await client.post(
            f"/runs/{run_id}/transcript",
            files={"file": ("meeting.vtt", SAMPLE_VTT.encode(), "text/vtt")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["questions_closed"] == 1


async def test_admin_upload_endpoint_empty_vtt(admin_client, fake_world_transcript):
    run_id = await _seed_run_with_questions(
        run_id_hint="admin-empty",
        questions=[("q-ae1", "Some question?")],
    )
    empty_vtt = "WEBVTT\n\n"
    async with admin_client as client:
        resp = await client.post(
            f"/runs/{run_id}/transcript",
            files={"file": ("empty.vtt", empty_vtt.encode(), "text/vtt")},
        )
    assert resp.status_code == 422
