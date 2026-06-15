"""Operator endpoints: health, cron tick, outreach kill switch, audit replay, transcript upload."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, UploadFile
from sqlalchemy import select

from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import AuditEvent
from reqsmith.persistence.repo import FlagRepo, RunRepo

router = APIRouter()

TICK_FLAG = "last_tick"
OUTREACH_FLAG = "outreach_paused"


@router.get("/healthz")
async def healthz():
    async with session_scope() as session:
        tick = await FlagRepo(session).get(TICK_FLAG)
    return {
        "status": "ok",
        "last_tick": tick.value if tick else None,  # SLA timers depend on external cron — surface staleness
    }


@router.post("/internal/tick")
async def tick():
    """External cron ping: advances SLA timers and outreach escalation ladder."""
    from reqsmith.outreach.ladder import process_sla_rungs
    now = datetime.now(UTC).isoformat()
    async with session_scope() as session:
        await FlagRepo(session).set(TICK_FLAG, now, enabled=True)
        await emit_event(session, actor="system", action="tick", detail={"at": now})
        advanced = await process_sla_rungs(session)
    return {"status": "ok", "tick": now, "advanced": advanced}


@router.post("/outreach/pause")
async def pause_outreach(paused: bool = True, reason: str = "manual"):
    """Global outreach kill switch (design §3a). Auto-pause also calls this."""
    async with session_scope() as session:
        await FlagRepo(session).set(OUTREACH_FLAG, reason, enabled=paused)
        await emit_event(
            session, actor="operator", action="outreach.paused" if paused else "outreach.resumed",
            detail={"reason": reason},
        )
    return {"outreach_paused": paused, "reason": reason}


@router.post("/runs/{run_id}/transcript")
async def upload_transcript(run_id: str, file: UploadFile):
    """Manual VTT upload fallback (T9.2). Ingests transcript and closes matching questions."""
    from reqsmith.outreach.transcript import ingest_transcript
    raw = await file.read()
    try:
        vtt_content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="file must be UTF-8 encoded text/vtt") from exc
    async with session_scope() as session:
        result = await ingest_transcript(run_id, vtt_content, session)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.post("/runs/{run_id}/transcript/fetch")
async def fetch_transcript(
    run_id: str,
    event_id: str = Query(..., description="Graph calendar event ID"),
    organizer: str = Query(..., description="Organizer AAD object ID or UPN"),
):
    """Fetch transcript from Microsoft Graph for a completed meeting (T9.1).

    Returns {"action": "ingested", ...} or {"action": "unavailable", ...} — never 5xx
    on tenant-policy block (Graph 403 is expected until admin enables transcript API).
    """
    from reqsmith.outreach.transcript import fetch_and_ingest_transcript
    async with session_scope() as session:
        result = await fetch_and_ingest_transcript(run_id, event_id, organizer, session)
    return result


@router.get("/runs/{run_id}/audit")
async def audit_replay(run_id: str):
    """Regulator view: every event that produced this run's artifacts, in order."""
    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        events = await session.scalars(
            select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
        )
        return {
            "run_id": run_id,
            "jira_issue_key": run.jira_issue_key,
            "state": run.state.value,
            "events": [
                {
                    "seq": e.id,
                    "at": e.created_at.isoformat(),
                    "actor": e.actor,
                    "action": e.action,
                    "input_hash": e.input_hash,
                    "output_hash": e.output_hash,
                    "prompt_version": e.prompt_version,
                    "model_id": e.model_id,
                    "policy_version": e.policy_version,
                    "detail": e.detail,
                }
                for e in events
            ],
        }
