"""Reviewer Console API — the human-on-the-loop backstage.

Exposes:
  GET  /reviewer/queue                      runs awaiting decision, ordered by SLA
  GET  /reviewer/runs/{id}/bundle           draft artifact + resolved source spans + gate results
  GET  /reviewer/runs/{id}/events           SSE stream of audit events (tail -f)
  POST /reviewer/runs/{id}/decision         approve / edit (with diff) / reject / escalate

Auth: Bearer token validated against REVIEWER_TOKENS env var
      (format: "email:role:token,..." where role is reviewer|checker).
      If REVIEWER_TOKENS is not set the service runs in dev mode — all requests are
      accepted with role=reviewer and identity=dev@localhost. This must never reach
      production without the env var set.

Maker≠checker is enforced server-side (same identity that recorded a maker approval
cannot be the checker).  The frontend surfaces the state; the backend enforces it.

Dual-channel reconciliation: a Jira workflow transition and a console decision writing
the same (run_id, artifact_id, role, reviewer_identity) UNIQUE tuple collapse into one
`approvals` row — whichever arrives first wins.
"""

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import (
    Approval,
    Artifact,
    AuditEvent,
    Citation,
    GateResult,
    RiskTier,
    Run,
    RunState,
    SourceDocument,
)
from reqsmith.persistence.repo import (
    ApprovalRepo,
    ArtifactRepo,
    JobRepo,
    RunRepo,
)
from reqsmith.settings import get_settings

router = APIRouter(prefix="/reviewer")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_REVIEWER_IDENTITY = "x-reviewer-identity"
_REVIEWER_TOKEN = "authorization"

_DEV_IDENTITY = "dev@localhost"
_DEV_ROLE = "reviewer"


def _parse_token_db() -> dict[str, tuple[str, str]]:
    """Return {token: (email, role)} from REVIEWER_TOKENS env var.
    Format: email:role:token,email2:role2:token2,..."""
    raw = get_settings().model_config.get("reviewer_tokens", "")
    # read from actual env
    import os
    raw = os.environ.get("REVIEWER_TOKENS", "")
    db: dict[str, tuple[str, str]] = {}
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) == 3:
            email, role, token = parts
            db[token] = (email, role)
    return db


class ReviewerContext(BaseModel):
    identity: str
    role: str  # reviewer | checker


async def _get_reviewer(
    authorization: str = Header(default=""),
    x_reviewer_identity: str = Header(default=""),
    x_reviewer_role: str = Header(default=""),
) -> ReviewerContext:
    """Resolve the caller's identity and role.

    Development mode (REVIEWER_TOKENS unset): identity/role taken from
    X-Reviewer-Identity / X-Reviewer-Role headers; defaults to dev@localhost/reviewer.
    Production mode: validates Bearer token against REVIEWER_TOKENS.
    """
    import os
    raw_tokens = os.environ.get("REVIEWER_TOKENS", "")
    if not raw_tokens:
        # dev bypass — must not reach production
        identity = x_reviewer_identity.strip() or _DEV_IDENTITY
        role = x_reviewer_role.strip() or _DEV_ROLE
        return ReviewerContext(identity=identity, role=role)

    token = authorization.removeprefix("Bearer ").strip()
    db = _parse_token_db()
    if token not in db:
        raise HTTPException(status_code=401, detail="invalid or missing reviewer token")
    email, role = db[token]
    return ReviewerContext(identity=email, role=role)


# ---------------------------------------------------------------------------
# Review Queue
# ---------------------------------------------------------------------------

REVIEW_STATES = {RunState.REVIEW, RunState.CHECKER_REVIEW}


def _risk_badge(tier: RiskTier | None) -> str:
    return (tier.value if tier else "unknown").upper()


@router.get("/queue")
async def review_queue(ctx: ReviewerContext = Depends(_get_reviewer)):
    """All runs currently awaiting a human decision, newest first."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(Run)
            .where(Run.state.in_(list(REVIEW_STATES)))
            .order_by(Run.updated_at.asc())
        )
        runs = list(rows)
        items = []
        for run in runs:
            # surface the latest draft artifact metadata without its full content
            artifact = await session.scalar(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.kind == "draft_story")
                .order_by(Artifact.version.desc())
                .limit(1)
            )
            has_maker = (
                await session.scalar(
                    select(Approval)
                    .where(
                        Approval.run_id == run.id,
                        Approval.role == "maker",
                        Approval.decision == "approve",
                    )
                    .limit(1)
                )
                is not None
            ) if run.state == RunState.CHECKER_REVIEW else None

            items.append({
                "run_id": run.id,
                "jira_issue_key": run.jira_issue_key,
                "state": run.state.value,
                "risk_tier": _risk_badge(run.risk_tier),
                "waiting_for": (
                    "checker" if run.state == RunState.CHECKER_REVIEW else "reviewer"
                ),
                "maker_approved": has_maker,
                "draft_version": artifact.version if artifact else None,
                "draft_prompt_version": artifact.prompt_version if artifact else None,
                "draft_model_id": artifact.model_id if artifact else None,
                "updated_at": run.updated_at.isoformat(),
            })
        return {"queue": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Artifact bundle (3-pane workspace data)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/bundle")
async def run_bundle(run_id: str, ctx: ReviewerContext = Depends(_get_reviewer)):
    """Full data needed to render the 3-pane Review Workspace.

    Returns:
    - run metadata (state, tier, version triple)
    - latest draft_story artifact content
    - citations resolved to their source document text + span
    - gate results for the run (all layers)
    - judge scores (from artifact or gate_results layer 2)
    - maker approvals list (so checker can verify identity separation)
    - audit event count (traceability signal)
    """
    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        # latest draft
        artifact = await ArtifactRepo(session).latest(run_id, "draft_story")
        if artifact is None:
            raise HTTPException(status_code=404, detail="no draft artifact for run")

        # citations → resolved source spans
        citation_rows = list(await session.scalars(
            select(Citation).where(Citation.artifact_id == artifact.id)
        ))
        resolved_citations = []
        for c in citation_rows:
            src = await session.get(SourceDocument, c.source_document_id)
            span_text = ""
            if src:
                text = src.text
                start = max(0, c.span_start)
                end = min(len(text), c.span_end) if c.span_end > 0 else len(text)
                span_text = text[start:end]
            resolved_citations.append({
                "citation_id": c.id,
                "claim_path": c.claim_path,
                "source_document_id": c.source_document_id,
                "source_origin": src.origin if src else None,
                "source_external_ref": src.external_ref if src else None,
                "span_start": c.span_start,
                "span_end": c.span_end,
                "span_text": span_text,
                "entailment_verdict": c.entailment_verdict,
                # full source for side-pane highlight
                "source_full_text": src.text if src else None,
            })

        # gate results
        gate_rows = list(await session.scalars(
            select(GateResult)
            .where(GateResult.run_id == run_id)
            .order_by(GateResult.layer, GateResult.id)
        ))
        gates = [
            {
                "layer": g.layer,
                "rule_id": g.rule_id,
                "verdict": g.verdict,
                "score": float(g.score) if g.score is not None else None,
                "reasoning": g.reasoning,
                "policy_version": g.policy_version,
            }
            for g in gate_rows
        ]

        # judge score (layer 2, rule_id starts with 'judge.')
        judge_gate = next(
            (g for g in gate_rows if g.rule_id.startswith("judge.")), None
        )
        judge_scores = judge_gate.reasoning if judge_gate else None

        # maker approvals (for checker to see who approved first)
        approval_rows = list(await session.scalars(
            select(Approval)
            .where(Approval.run_id == run_id, Approval.artifact_id == artifact.id)
            .order_by(Approval.created_at)
        ))
        approvals = [
            {
                "role": a.role,
                "decision": a.decision,
                "reviewer_identity": a.reviewer_identity,
                "has_diff": a.diff is not None,
                "at": a.created_at.isoformat(),
            }
            for a in approval_rows
        ]

        # event count for traceability signal
        event_count = await session.scalar(
            select(AuditEvent.id)
            .where(AuditEvent.run_id == run_id)
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )

        return {
            "run": {
                "id": run.id,
                "jira_issue_key": run.jira_issue_key,
                "state": run.state.value,
                "risk_tier": _risk_badge(run.risk_tier),
                "prompt_pack_version": run.prompt_pack_version,
                "policy_version": run.policy_version,
            },
            "artifact": {
                "id": artifact.id,
                "kind": artifact.kind,
                "version": artifact.version,
                "content_hash": artifact.content_hash,
                "prompt_version": artifact.prompt_version,
                "model_id": artifact.model_id,
                "policy_version": artifact.policy_version,
                "content": artifact.content,
            },
            "citations": resolved_citations,
            "gates": gates,
            "judge_scores": judge_scores,
            "approvals": approvals,
            "audit_event_count": event_count,
        }


# ---------------------------------------------------------------------------
# SSE event stream (live run timeline)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    since_id: int = 0,
    ctx: ReviewerContext = Depends(_get_reviewer),
):
    """Server-Sent Events stream: emits audit events for the run in real time.

    Clients connect once; the stream polls the DB every 2 s and pushes new rows.
    The `since_id` query param allows clients to reconnect without replaying history.
    The stream ends when the run reaches a terminal state.
    """
    async def _generate() -> AsyncGenerator[str, None]:
        last_id = since_id
        terminal = {"complete", "failed", "quarantined", "escalated"}
        while True:
            async with session_scope() as session:
                rows = list(await session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.run_id == run_id,
                        AuditEvent.id > last_id,
                    )
                    .order_by(AuditEvent.id)
                    .limit(50)
                ))
                run = await session.get(Run, run_id)

            if rows:
                for row in rows:
                    data = json.dumps({
                        "id": row.id,
                        "at": row.created_at.isoformat(),
                        "actor": row.actor,
                        "action": row.action,
                        "prompt_version": row.prompt_version,
                        "model_id": row.model_id,
                        "policy_version": row.policy_version,
                        "detail": row.detail,
                    })
                    yield f"id: {row.id}\ndata: {data}\n\n"
                    last_id = row.id

            if run is None or run.state.value in terminal:
                yield "event: done\ndata: {}\n\n"
                return

            await asyncio.sleep(2)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Decision endpoint
# ---------------------------------------------------------------------------

class DecisionRequest(BaseModel):
    decision: str        # approve | reject | escalate | edit
    diff: dict | None = None   # present when decision == "edit"
    note: str = ""


@router.post("/runs/{run_id}/decision")
async def post_decision(
    run_id: str,
    body: DecisionRequest,
    ctx: ReviewerContext = Depends(_get_reviewer),
):
    """Record a reviewer decision, transition the run, and enqueue the next job.

    Approval / editing / rejection are idempotent: same (run, artifact, role, identity)
    always collapses to the first recorded decision — double-clicks and dual-channel
    (console + Jira) both handled by the UNIQUE constraint on `approvals`.

    Maker≠checker enforced: same identity that made a `maker` approval cannot be checker.
    """
    if body.decision not in {"approve", "reject", "escalate", "edit"}:
        raise HTTPException(status_code=400, detail=f"unknown decision: {body.decision}")

    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.state not in REVIEW_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"run is in state '{run.state}' — decisions only accepted in review states",
            )

        artifact = await ArtifactRepo(session).latest(run_id, "draft_story")
        if artifact is None:
            raise HTTPException(status_code=404, detail="no draft artifact")

        run_repo = RunRepo(session)
        approvals = ApprovalRepo(session)

        # --- reject ---
        if body.decision == "reject":
            await approvals.record(
                run_id=run.id, artifact_id=artifact.id, role=ctx.role,
                decision="reject", reviewer_identity=ctx.identity,
                diff=body.diff,
            )
            await run_repo.transition(run, RunState.DRAFTING, actor=ctx.identity,
                                      detail={"decision": "reject", "note": body.note})
            job, _ = await JobRepo(session).enqueue(run.id, "drafting")
            await emit_event(
                session, actor=ctx.identity, action="decision.rejected", run_id=run.id,
                detail={"note": body.note},
            )
            return {"status": "rejected", "run_id": run.id, "job_id": job.id}

        # --- escalate ---
        if body.decision == "escalate":
            await approvals.record(
                run_id=run.id, artifact_id=artifact.id, role=ctx.role,
                decision="escalate", reviewer_identity=ctx.identity,
            )
            await run_repo.transition(run, RunState.ESCALATED, actor=ctx.identity,
                                      detail={"note": body.note})
            await emit_event(
                session, actor=ctx.identity, action="decision.escalated", run_id=run.id,
                detail={"note": body.note},
            )
            return {"status": "escalated", "run_id": run.id}

        # --- edit (submit revised draft; counts as an approval for workflow purposes) ---
        is_edit = body.decision == "edit"
        if is_edit:
            if not body.diff:
                raise HTTPException(status_code=400, detail="diff is required for 'edit' decision")
            await approvals.record(
                run_id=run.id, artifact_id=artifact.id, role=ctx.role,
                decision="edit", reviewer_identity=ctx.identity, diff=body.diff,
            )
            await emit_event(
                session, actor=ctx.identity, action="decision.edited", run_id=run.id,
                detail={"note": body.note, "diff_keys": list(body.diff.keys())},
            )

        # --- approve (and the tail of "edit") ---
        high_tier = run.risk_tier == RiskTier.HIGH

        if run.state == RunState.REVIEW:
            role = "maker" if high_tier else "reviewer"
            if not is_edit:
                _, is_new = await approvals.record(
                    run_id=run.id, artifact_id=artifact.id, role=role,
                    decision="approve", reviewer_identity=ctx.identity,
                )
                if not is_new:
                    # idempotent double-click
                    return {"status": "already_recorded", "run_id": run.id}

            if high_tier:
                await run_repo.transition(run, RunState.CHECKER_REVIEW, actor=ctx.identity,
                                          detail={"maker": ctx.identity})
                await emit_event(
                    session, actor=ctx.identity, action="decision.maker_approved", run_id=run.id,
                )
                return {"status": "awaiting_checker", "run_id": run.id}

            await run_repo.transition(run, RunState.PUBLISHING, actor=ctx.identity)
            job, _ = await JobRepo(session).enqueue(run.id, "publish")
            await emit_event(
                session, actor=ctx.identity, action="decision.approved", run_id=run.id,
            )
            return {"status": "approved", "run_id": run.id, "job_id": job.id}

        # checker stage
        makers = await approvals.makers_for(run.id, artifact.id)
        if ctx.identity in makers:
            await emit_event(
                session, actor=ctx.identity, action="approval.rejected_same_identity",
                run_id=run.id, detail={"reason": "checker must differ from maker"},
            )
            raise HTTPException(
                status_code=403,
                detail="checker must be a different person than the maker (two-person rule)",
            )

        if not is_edit:
            _, is_new = await approvals.record(
                run_id=run.id, artifact_id=artifact.id, role="checker",
                decision="approve", reviewer_identity=ctx.identity,
            )
            if not is_new:
                return {"status": "already_recorded", "run_id": run.id}

        await run_repo.transition(run, RunState.PUBLISHING, actor=ctx.identity,
                                  detail={"checker": ctx.identity})
        job, _ = await JobRepo(session).enqueue(run.id, "publish")
        await emit_event(
            session, actor=ctx.identity, action="decision.checker_approved", run_id=run.id,
        )
        return {"status": "approved", "run_id": run.id, "job_id": job.id}
