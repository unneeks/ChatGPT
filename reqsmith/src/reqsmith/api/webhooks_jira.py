"""Jira webhook receiver — the front door.

Early-response pattern (design §1): validate, persist, return 202 + job_id
immediately; all processing is async. The webhook payload is a hint — stages
re-fetch the issue from Jira before acting.

Routing:
- no active run for the issue → new run + triage job
- active run AWAITING_INPUT + comment event → resume triage (answers arrived)
- active run REVIEW/CHECKER_REVIEW + status change → approval flow (maker–checker)
- anything else on an active run → audited no-op
"""

import hmac

from fastapi import APIRouter, HTTPException, Query, Request

from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import RiskTier, RunState
from reqsmith.persistence.repo import ApprovalRepo, ArtifactRepo, JobRepo, RunRepo
from reqsmith.settings import get_settings

router = APIRouter()

INTAKE_STAGE = "triage"
APPROVE_STATUSES = {"approved"}
REJECT_STATUSES = {"rejected"}


def _status_change(payload: dict) -> str | None:
    for item in (payload.get("changelog") or {}).get("items", []):
        if item.get("field") == "status":
            return (item.get("toString") or "").lower()
    return None


def _webhook_actor(payload: dict) -> str:
    user = payload.get("user") or {}
    return user.get("emailAddress") or user.get("displayName") or "unknown"


async def _handle_approval(session, run, payload: dict) -> dict:
    """Workflow transition = approval signal. Maker–checker for high tier: two
    distinct identities must approve before publish."""
    status = _status_change(payload)
    reviewer = _webhook_actor(payload)
    artifact = await ArtifactRepo(session).latest(run.id, "draft_story")
    if artifact is None:
        return {"status": "ignored", "reason": "no draft artifact"}
    run_repo = RunRepo(session)
    approvals = ApprovalRepo(session)

    if status in REJECT_STATUSES:
        await approvals.record(
            run_id=run.id, artifact_id=artifact.id, role="reviewer", decision="reject",
            reviewer_identity=reviewer,
        )
        await run_repo.transition(run, RunState.DRAFTING, actor=reviewer,
                                  detail={"decision": "reject"})
        job, _ = await JobRepo(session).enqueue(run.id, "drafting")
        return {"status": "rejected", "run_id": run.id, "job_id": job.id}

    if status not in APPROVE_STATUSES:
        return {"status": "ignored", "reason": f"status '{status}' not an approval signal"}

    high_tier = run.risk_tier == RiskTier.HIGH
    if run.state == RunState.REVIEW:
        role = "maker" if high_tier else "reviewer"
        await approvals.record(
            run_id=run.id, artifact_id=artifact.id, role=role, decision="approve",
            reviewer_identity=reviewer,
        )
        if high_tier:
            await run_repo.transition(run, RunState.CHECKER_REVIEW, actor=reviewer,
                                      detail={"maker": reviewer})
            return {"status": "awaiting_checker", "run_id": run.id}
        await run_repo.transition(run, RunState.PUBLISHING, actor=reviewer)
        job, _ = await JobRepo(session).enqueue(run.id, "publish")
        return {"status": "approved", "run_id": run.id, "job_id": job.id}

    # checker stage: identity must differ from every maker (two-person rule)
    makers = await approvals.makers_for(run.id, artifact.id)
    if reviewer in makers:
        await emit_event(
            session, actor=reviewer, action="approval.rejected_same_identity", run_id=run.id,
            detail={"reason": "checker must differ from maker"},
        )
        return {"status": "blocked", "reason": "checker must be a different person than maker"}
    await approvals.record(
        run_id=run.id, artifact_id=artifact.id, role="checker", decision="approve",
        reviewer_identity=reviewer,
    )
    await run_repo.transition(run, RunState.PUBLISHING, actor=reviewer,
                              detail={"checker": reviewer})
    job, _ = await JobRepo(session).enqueue(run.id, "publish")
    return {"status": "approved", "run_id": run.id, "job_id": job.id}


@router.post("/webhooks/jira", status_code=202)
async def jira_webhook(request: Request, secret: str = Query(default="")):
    settings = get_settings()
    if settings.jira_webhook_secret and not hmac.compare_digest(
        secret, settings.jira_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    payload = await request.json()
    issue = payload.get("issue") or {}
    issue_key = issue.get("key")
    if not issue_key:
        raise HTTPException(status_code=400, detail="no issue key in payload")
    event = payload.get("webhookEvent", "")

    async with session_scope() as session:
        run_repo = RunRepo(session)
        active = await run_repo.active_for_issue(issue_key)

        if active is None:
            run, created = await run_repo.create_run(issue_key, payload)
            job, _ = await JobRepo(session).enqueue(run.id, INTAKE_STAGE)
            await emit_event(
                session, actor="system", action="webhook.received", run_id=run.id,
                job_id=job.id, input_payload=payload,
                detail={"issue_key": issue_key, "duplicate": not created,
                        "webhook_event": event},
            )
            return {"status": "accepted", "run_id": run.id, "job_id": job.id,
                    "duplicate": not created, "status_url": f"/jobs/{job.id}"}

        await emit_event(
            session, actor="system", action="webhook.received", run_id=active.id,
            input_payload=payload,
            detail={"issue_key": issue_key, "webhook_event": event, "routed_to_active": True},
        )

        if active.state == RunState.AWAITING_INPUT and event == "comment_created":
            comment_author = ((payload.get("comment") or {}).get("author") or {})
            if comment_author.get("emailAddress", "") == "" and \
               "reqsmith" in (comment_author.get("displayName", "").lower()):
                return {"status": "ignored", "reason": "own comment echo"}
            await run_repo.transition(active, RunState.TRIAGE, detail={"resume": "answer received"})
            job, _ = await JobRepo(session).enqueue(active.id, INTAKE_STAGE)
            return {"status": "resumed", "run_id": active.id, "job_id": job.id,
                    "status_url": f"/jobs/{job.id}"}

        if active.state in (RunState.REVIEW, RunState.CHECKER_REVIEW) and _status_change(payload):
            result = await _handle_approval(session, active, payload)
            return result

        return {"status": "ignored", "run_id": active.id,
                "reason": f"no action for event '{event}' in state '{active.state.value}'"}
