"""End-to-end smoke: webhook → triage → retrieval → drafting → verification →
human approval → publish, with fake adapters. This is the M6 milestone gate and
doubles as the live demo runbook (same flow against real Jira)."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from reqsmith import deps
from reqsmith.adapters.jira.fake import FakeJira
from reqsmith.adapters.jira.port import JiraIssue
from reqsmith.adapters.llm.port import LLMResult
from reqsmith.api.app import create_app
from reqsmith.orchestrator import engine
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import AuditEvent, RtmEdge, RunState
from reqsmith.persistence.repo import RunRepo

EPIC_DESCRIPTION = (
    "Digitise the retail onboarding intake flow so that branch staff capture structured "
    "applicant information once and downstream operations teams stop re-keying it into "
    "legacy systems. Outcome: cut onboarding handling time and reduce keying errors."
)

HIGH_TIER_DESCRIPTION = EPIC_DESCRIPTION + " This change touches the payments settlement flow."


class GroundedFakeLLM:
    """Fake analyst/judge that honours the citation contract: it parses real
    source_ids out of the prompt variables and cites them."""

    async def complete(self, *, prompt_id: str, variables: dict, model_role: str = "drafting",
                       max_tokens: int = 4096) -> LLMResult:
        if model_role == "judge":
            payload = {"scores": {"unambiguous": 8, "complete": 8, "testable": 9,
                                  "consistent": 8, "atomic": 8},
                       "overall": 8.2, "blocking_issues": [], "reasoning": "solid draft"}
        else:
            source_id = None
            for line in variables["sources"].splitlines():
                if line.startswith("[source_id: "):
                    source_id = line.split("[source_id: ")[1].split("]")[0]
                    break
            assert source_id, "drafting prompt must include at least one source"
            payload = {
                "epic_summary": "Retail onboarding digitisation",
                "stories": [
                    {
                        "title": "Structured intake form",
                        "story": "As a branch officer, I want a structured intake form, "
                                 "so that customer data is captured once",
                        "acceptance_criteria": [
                            "Given a new applicant When the officer submits the form "
                            "Then a single structured record is created"
                        ],
                        "citations": [{"source_id": source_id, "span_start": 0, "span_end": 50}],
                        "nfrs": ["Form submission completes within agreed response-time SLA"],
                    }
                ],
                "assumptions": ["Branch staff have SSO access"],
                "open_questions": [],
            }
        return LLMResult(text=json.dumps(payload), model_id=f"fake-{model_role}",
                         prompt_version=prompt_id, input_tokens=100, output_tokens=200)


@pytest.fixture
def fake_world():
    jira = FakeJira()
    deps.set_jira(jira)
    deps.set_llm(GroundedFakeLLM())
    yield jira
    deps.set_jira(None)
    deps.set_llm(None)


def _client():
    app = create_app(run_worker=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _drain():
    while await engine.process_next():
        pass


def _epic_payload(key="BANK-101"):
    return {"webhookEvent": "jira:issue_created", "issue": {"key": key}}


def _approval_payload(key, reviewer, status="Approved"):
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": key},
        "user": {"emailAddress": reviewer},
        "changelog": {"items": [{"field": "status", "toString": status}]},
    }


async def test_full_pipeline_medium_tier(fake_world):
    fake_world.seed(JiraIssue(
        key="BANK-101", issue_type="Epic", summary="Customer onboarding revamp",
        description=EPIC_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as client:
        accepted = (await client.post("/webhooks/jira", json=_epic_payload())).json()
        run_id = accepted["run_id"]
        await _drain()

        # pipeline stopped at human review with the draft posted on the epic
        run_view = (await client.get(f"/runs/{run_id}")).json()
        assert run_view["state"] == "review"
        assert run_view["risk_tier"] == "medium"
        assert fake_world.comments_containing("BANK-101", "[REQ-REVIEW]")

        # human approves via Jira workflow transition
        decision = (await client.post(
            "/webhooks/jira", json=_approval_payload("BANK-101", "po@bank.com"))).json()
        assert decision["status"] == "approved"
        await _drain()

        run_view = (await client.get(f"/runs/{run_id}")).json()
        assert run_view["state"] == "complete"

        # stories published under the epic
        created = [k for k in fake_world.issues if k != "BANK-101"]
        assert len(created) == 1
        assert fake_world.issues[created[0]].fields.get("parent") == "BANK-101"

        # RTM edges + full audit lineage
        async with session_scope() as session:
            edges = list(await session.scalars(select(RtmEdge).where(RtmEdge.run_id == run_id)))
            assert {e.edge_kind for e in edges} == {"derives_from", "cites"}

        audit = (await client.get(f"/runs/{run_id}/audit")).json()
        actions = [e["action"] for e in audit["events"]]
        for expected in ["run.created", "risk_tier.assigned", "draft.created",
                         "judge.scored", "stories.published"]:
            assert expected in actions, f"missing {expected} in lineage"
        draft_events = [e for e in audit["events"] if e["action"] == "draft.created"]
        assert draft_events[0]["prompt_version"] == "analyst_v1"
        assert draft_events[0]["model_id"] == "fake-drafting"


async def test_maker_checker_enforced_for_high_tier(fake_world):
    fake_world.seed(JiraIssue(
        key="BANK-201", issue_type="Epic", summary="Payments settlement onboarding",
        description=HIGH_TIER_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as client:
        accepted = (await client.post("/webhooks/jira", json=_epic_payload("BANK-201"))).json()
        run_id = accepted["run_id"]
        await _drain()
        assert (await client.get(f"/runs/{run_id}")).json()["risk_tier"] == "high"

        # maker approves → moves to checker review, not publish
        first = (await client.post(
            "/webhooks/jira", json=_approval_payload("BANK-201", "alice@bank.com"))).json()
        assert first["status"] == "awaiting_checker"

        # same identity tries to check own work → blocked
        blocked = (await client.post(
            "/webhooks/jira", json=_approval_payload("BANK-201", "alice@bank.com"))).json()
        assert blocked["status"] == "blocked"

        # independent checker approves → publish
        second = (await client.post(
            "/webhooks/jira", json=_approval_payload("BANK-201", "bob@bank.com"))).json()
        assert second["status"] == "approved"
        await _drain()
        assert (await client.get(f"/runs/{run_id}")).json()["state"] == "complete"


async def test_incomplete_intake_asks_questions_then_resumes(fake_world):
    fake_world.seed(JiraIssue(
        key="BANK-301", issue_type="Epic", summary="Vague ask",
        description="make it better", status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as client:
        accepted = (await client.post("/webhooks/jira", json=_epic_payload("BANK-301"))).json()
        run_id = accepted["run_id"]
        await _drain()

        assert (await client.get(f"/runs/{run_id}")).json()["state"] == "awaiting_input"
        questions = fake_world.comments_containing("BANK-301", "[REQ-Q:")
        assert questions, "expected a structured question comment on the issue"

        # replaying the webhook while waiting must not duplicate questions
        await client.post("/webhooks/jira", json=_epic_payload("BANK-301"))
        await _drain()
        assert fake_world.comments_containing("BANK-301", "[REQ-Q:") == questions

        # sponsor improves the description and replies → run resumes and proceeds
        fake_world.issues["BANK-301"].description = EPIC_DESCRIPTION
        resume_payload = {
            "webhookEvent": "comment_created",
            "issue": {"key": "BANK-301"},
            "comment": {"author": {"emailAddress": "sponsor@bank.com"},
                        "body": "Updated the description with outcome and scope."},
        }
        resumed = (await client.post("/webhooks/jira", json=resume_payload)).json()
        assert resumed["status"] == "resumed"
        await _drain()
        assert (await client.get(f"/runs/{run_id}")).json()["state"] == "review"


async def test_run_audit_is_replayable(fake_world):
    """Every run reconstructs: who did what, with which versions, in order."""
    fake_world.seed(JiraIssue(
        key="BANK-101", issue_type="Epic", summary="Customer onboarding revamp",
        description=EPIC_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as client:
        accepted = (await client.post("/webhooks/jira", json=_epic_payload())).json()
        await _drain()
        audit = (await client.get(f"/runs/{accepted['run_id']}/audit")).json()
        seqs = [e["seq"] for e in audit["events"]]
        assert seqs == sorted(seqs)
        transitions = [e["detail"] for e in audit["events"] if e["action"] == "run.transition"]
        assert transitions[0]["from"] == "intake"
        async with session_scope() as session:
            run = await RunRepo(session).get(accepted["run_id"])
            assert run.state == RunState.REVIEW
        # every event row exists forever — count is part of the regulator contract
        async with session_scope() as session:
            count = await session.scalar(select(func.count()).select_from(AuditEvent))
            assert count >= len(audit["events"])
