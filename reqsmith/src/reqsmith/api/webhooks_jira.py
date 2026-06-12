"""Jira webhook receiver — the front door.

Early-response pattern (design §1): validate, persist, return 202 + job_id
immediately; all processing is async. The webhook payload is treated as a hint —
the triage stage re-fetches the issue from Jira before acting.
"""

import hmac

from fastapi import APIRouter, HTTPException, Query, Request

from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.repo import JobRepo, RunRepo
from reqsmith.settings import get_settings

router = APIRouter()

INTAKE_STAGE = "triage"


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

    async with session_scope() as session:
        run, created = await RunRepo(session).create_run(issue_key, payload)
        job, _ = await JobRepo(session).enqueue(run.id, INTAKE_STAGE)
        await emit_event(
            session, actor="system", action="webhook.received", run_id=run.id, job_id=job.id,
            input_payload=payload,
            detail={"issue_key": issue_key, "duplicate": not created,
                    "webhook_event": payload.get("webhookEvent")},
        )

    return {
        "status": "accepted",
        "run_id": run.id,
        "job_id": job.id,
        "duplicate": not created,
        "status_url": f"/jobs/{job.id}",
    }
