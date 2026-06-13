"""Reviewer Console API tests (M6.5).

Tests the backend of the reviewer console:
- Queue endpoint returns runs in review state
- Bundle endpoint returns full artifact + citations + gates
- Decision endpoint enforces maker≠checker and transitions state
- SSE events endpoint streams audit events
- Dual-channel reconciliation: console + Jira webhook decisions collapse idempotently
"""

import json

import httpx
import pytest

from reqsmith import deps
from reqsmith.adapters.jira.fake import FakeJira
from reqsmith.adapters.jira.port import JiraIssue
from reqsmith.adapters.llm.port import LLMResult
from reqsmith.api.app import create_app
from reqsmith.orchestrator import engine

EPIC_DESCRIPTION = (
    "Digitise the retail onboarding intake flow so that branch staff capture structured "
    "applicant information once and downstream operations teams stop re-keying it into "
    "legacy systems. Outcome: cut onboarding handling time and reduce keying errors."
)
HIGH_TIER_DESCRIPTION = EPIC_DESCRIPTION + " This change touches the payments settlement flow."


class SimpleFakeLLM:
    async def complete(self, *, prompt_id: str, variables: dict, model_role: str = "drafting",
                       max_tokens: int = 4096) -> LLMResult:
        if model_role == "judge":
            payload = {"scores": {"unambiguous": 8, "complete": 8, "testable": 9,
                                  "consistent": 8, "atomic": 8},
                       "overall": 8.2, "blocking_issues": [], "reasoning": "solid"}
        else:
            source_id = None
            for line in variables.get("sources", "").splitlines():
                if line.startswith("[source_id: "):
                    source_id = line.split("[source_id: ")[1].split("]")[0]
                    break
            if not source_id:
                source_id = "fallback-src"
            payload = {
                "epic_summary": "Retail onboarding digitisation",
                "stories": [{
                    "title": "Structured intake form",
                    "story": "As a branch officer I want a form so data is captured once",
                    "acceptance_criteria": [
                        "Given a new applicant When form submitted Then record created"
                    ],
                    "citations": [{"source_id": source_id, "span_start": 0, "span_end": 50}],
                    "nfrs": [],
                }],
                "assumptions": [],
                "open_questions": [],
            }
        return LLMResult(text=json.dumps(payload), model_id=f"fake-{model_role}",
                         prompt_version=prompt_id, input_tokens=50, output_tokens=100)


@pytest.fixture
def fake_world():
    jira = FakeJira()
    deps.set_jira(jira)
    deps.set_llm(SimpleFakeLLM())
    yield jira
    deps.set_jira(None)
    deps.set_llm(None)


def _client():
    app = create_app(run_worker=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _drain():
    while await engine.process_next():
        pass


def _epic(key="BANK-101"):
    return {"webhookEvent": "jira:issue_created", "issue": {"key": key}}


async def _setup_run_in_review(fake_world, key="BANK-101"):
    """Helper: seed issue, trigger pipeline, drain to REVIEW state, return run_id."""
    fake_world.seed(JiraIssue(
        key=key, issue_type="Epic", summary="Customer onboarding revamp",
        description=EPIC_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as c:
        accepted = (await c.post("/webhooks/jira", json=_epic(key))).json()
        await _drain()
        return accepted["run_id"]


async def test_queue_shows_runs_awaiting_review(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    async with _client() as c:
        resp = await c.get("/reviewer/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    item = next(r for r in body["queue"] if r["run_id"] == run_id)
    assert item["state"] == "review"
    assert item["risk_tier"] == "MEDIUM"
    assert item["waiting_for"] == "reviewer"


async def test_bundle_returns_artifact_and_citations(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    async with _client() as c:
        resp = await c.get(f"/reviewer/runs/{run_id}/bundle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact"]["kind"] == "draft_story"
    assert len(body["citations"]) >= 1
    # citation must resolve to a source span
    cit = body["citations"][0]
    assert cit["source_full_text"] is not None
    assert cit["span_text"] != ""
    # gates present
    assert any(g["layer"] == 1 for g in body["gates"])


async def test_console_decision_approve_transitions_run(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    async with _client() as c:
        resp = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve", "note": "LGTM"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        await _drain()
        run_view = (await c.get(f"/runs/{run_id}")).json()
        assert run_view["state"] == "complete"


async def test_console_decision_reject_re_queues_draft(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    async with _client() as c:
        resp = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "reject", "note": "needs more detail"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        run_view = (await c.get(f"/runs/{run_id}")).json()
        assert run_view["state"] == "drafting"


async def test_console_decision_escalate(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    async with _client() as c:
        resp = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "escalate", "note": "regulatory concern"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "escalated"
        run_view = (await c.get(f"/runs/{run_id}")).json()
        assert run_view["state"] == "escalated"


async def test_console_edit_records_diff(fake_world):
    run_id = await _setup_run_in_review(fake_world)
    diff = {"epic_summary": "Revised summary by reviewer"}
    async with _client() as c:
        resp = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "edit", "diff": diff, "note": "minor correction"},
        )
    assert resp.status_code == 200
    # edit is treated as approve
    assert resp.json()["status"] == "approved"


async def test_maker_checker_enforced_for_high_tier_console(fake_world):
    """High-tier run: same identity cannot be maker and checker from the console."""
    fake_world.seed(JiraIssue(
        key="BANK-501", issue_type="Epic", summary="Payments settlement",
        description=HIGH_TIER_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as c:
        accepted = (await c.post("/webhooks/jira", json=_epic("BANK-501"))).json()
        run_id = accepted["run_id"]
        await _drain()

        run = (await c.get(f"/runs/{run_id}")).json()
        assert run["risk_tier"] == "high"

        # maker approves
        first = (await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve"},
            headers={"X-Reviewer-Identity": "alice@bank.com"},
        )).json()
        assert first["status"] == "awaiting_checker"

        # same identity tries to check — should be blocked (403)
        blocked = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve"},
            headers={"X-Reviewer-Identity": "alice@bank.com"},
        )
        assert blocked.status_code == 403

        # different identity checks — should succeed
        second = (await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve"},
            headers={"X-Reviewer-Identity": "bob@bank.com"},
        )).json()
        assert second["status"] == "approved"


async def test_dual_channel_reconciliation(fake_world):
    """Console approval and Jira webhook approval for same run collapse idempotently."""
    run_id = await _setup_run_in_review(fake_world)

    async with _client() as c:
        # Console approves first
        console_resp = (await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve"},
        )).json()
        assert console_resp["status"] == "approved"

        # Jira webhook delivers the same approval signal — must be a no-op
        jira_resp = (await c.post("/webhooks/jira", json={
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "BANK-101"},
            "user": {"emailAddress": "dev@localhost"},
            "changelog": {"items": [{"field": "status", "toString": "Approved"}]},
        })).json()
        # Either ignored (already publishing) or approved (idempotent)
        assert jira_resp.get("status") in ("approved", "ignored", "already_recorded")


async def test_bundle_404_for_missing_run(fake_world):
    async with _client() as c:
        resp = await c.get("/reviewer/runs/nonexistent-run/bundle")
    assert resp.status_code == 404


async def test_decision_rejected_for_wrong_state(fake_world):
    """Decision endpoint rejects decisions on runs not in review states."""
    fake_world.seed(JiraIssue(
        key="BANK-601", issue_type="Epic", summary="Onboarding",
        description=EPIC_DESCRIPTION, status="Open", reporter="sponsor@bank.com",
    ))
    async with _client() as c:
        accepted = (await c.post("/webhooks/jira", json=_epic("BANK-601"))).json()
        run_id = accepted["run_id"]
        # Don't drain — run is still in intake/triage
        resp = await c.post(
            f"/reviewer/runs/{run_id}/decision",
            json={"decision": "approve"},
        )
    assert resp.status_code == 409
