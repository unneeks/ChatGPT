"""Outreach escalation ladder (design §3a).

The ladder has 3 rungs defined in outreach-v1.yaml:
  Rung 1: Jira comment (already sent by triage stage when questions are created)
  Rung 2: Teams adaptive card (proactive 1:1 DM)
  Rung 3: Meeting invite via Microsoft Graph

`advance_question(question_id, session)` is called by `/internal/tick` for every
open question past its SLA deadline. It checks:
  - global outreach pause (kill switch)
  - question is still open
  - SLA deadline passed for current rung
  - rate budget allows send
  - idempotency: already sent for this (question, rung, attempt) → skip

`process_sla_rungs(session)` sweeps all open questions and calls advance_question.

This module is pure orchestration code — no LLM agents involved.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.outreach.budget import check_send_allowed
from reqsmith.outreach.cards import build_question_card
from reqsmith.persistence.models import Question
from reqsmith.persistence.repo import FlagRepo, OutreachRepo
from reqsmith.verification.gates import load_policy_pack

OUTREACH_FLAG = "outreach_paused"


def _policy():
    return load_policy_pack("outreach")


def _rung_config(rung: int) -> dict:
    for r in _policy().get("ladder", []):
        if r["rung"] == rung:
            return r
    return {}


def _sla_hours(rung: int) -> int:
    return _rung_config(rung).get("sla_hours", 24 * rung)


async def _is_outreach_paused(session: AsyncSession) -> bool:
    flag = await FlagRepo(session).get(OUTREACH_FLAG)
    return flag is not None and flag.enabled


async def advance_question(question_id: str, session: AsyncSession) -> dict:
    """Try to advance one question up the escalation ladder.

    Returns a status dict: {"action": "sent"|"skipped"|"blocked"|"closed", "reason": ...}
    """
    question = await session.scalar(
        select(Question).where(Question.question_id == question_id)
    )
    if question is None:
        return {"action": "skipped", "reason": "question not found"}
    if question.status not in ("open", "asked"):
        return {"action": "skipped", "reason": f"question status is '{question.status}'"}

    # global kill switch check
    if await _is_outreach_paused(session):
        return {"action": "blocked", "reason": "outreach globally paused"}

    now = datetime.now(UTC)
    current_rung = question.current_rung

    # check if SLA for current rung has passed
    # SQLite returns tz-naive datetimes; coerce to UTC-aware for comparison
    sla = question.sla_deadline
    if sla is not None and sla.tzinfo is None:
        sla = sla.replace(tzinfo=UTC)
    if sla and now < sla:
        return {"action": "skipped", "reason": "SLA deadline not reached yet"}

    next_rung = current_rung + 1
    max_rung = max(r["rung"] for r in _policy().get("ladder", [{"rung": 1}]))

    if next_rung > max_rung:
        # exhausted all rungs → escalate
        question.status = "escalated"
        await session.flush()
        await emit_event(
            session, actor="ladder", action="question.escalated",
            detail={"question_id": question_id, "reason": "all rungs exhausted"},
        )
        return {"action": "escalated", "reason": "all ladder rungs exhausted"}

    rung_cfg = _rung_config(next_rung)
    channel = rung_cfg.get("channel", "jira_comment")

    outreach = OutreachRepo(session)

    # idempotency gate: check if already sent for (question, next_rung) BEFORE sending
    idempotency_key = f"{question_id}:{next_rung}:1:out:{channel}"
    from reqsmith.persistence.models import OutreachEvent
    existing_event = await session.scalar(
        select(OutreachEvent).where(OutreachEvent.idempotency_key == idempotency_key)
    )
    if existing_event is not None:
        return {"action": "skipped", "reason": "already sent (idempotent)"}

    # budget check — after idempotency so retries don't burn budget
    allowed, reason = await check_send_allowed(
        session, stakeholder_aad_id=question.stakeholder_aad_id, channel=channel,
    )
    if not allowed:
        await emit_event(
            session, actor="ladder", action="outreach.budget_blocked",
            detail={"question_id": question_id, "channel": channel, "reason": reason},
        )
        return {"action": "blocked", "reason": reason}

    external_id: str | None = None

    if channel == "teams_card":
        if not question.stakeholder_aad_id:
            return {"action": "skipped", "reason": "no stakeholder_aad_id for Teams card"}
        teams = deps.get_teams()
        # determine turn count: number of prior cards sent for this question
        from sqlalchemy import func as sqlfunc

        from reqsmith.persistence.models import OutreachEvent
        prior_card_count = await session.scalar(
            select(sqlfunc.count()).select_from(OutreachEvent)
            .where(
                OutreachEvent.question_id == question_id,
                OutreachEvent.channel == "teams_card",
                OutreachEvent.direction == "out",
            )
        )
        turn = (prior_card_count or 0) + 1
        card = build_question_card(
            question_id=question_id,
            question_text=question.text,
            turn=turn,
        )
        external_id = await teams.send_card(
            user_aad_id=question.stakeholder_aad_id, card=card
        )

    elif channel == "meeting_invite":
        # Delegate entirely to the scheduler which handles agenda gate, rebook-once,
        # attendee list, and Graph API call. It manages its own outreach_events rows.
        from reqsmith.outreach.scheduler import schedule_meeting_for_question
        result = await schedule_meeting_for_question(question_id, session)
        if result["action"] in ("scheduled",):
            # scheduler wrote the outreach_event; advance rung here
            question.current_rung = next_rung
            question.sla_deadline = datetime.now(UTC) + timedelta(hours=_sla_hours(next_rung))
            await session.flush()
        return result

    elif channel == "jira_comment":
        # Rung 1 is always jira_comment — sent by triage. Re-sending on later rungs
        # is unusual but handled here for completeness.
        jira = deps.get_jira()
        from reqsmith.persistence.models import Run
        run = await session.get(Run, question.run_id)
        if run:
            body = f"[REMINDER][REQ-Q:{question_id}] {question.text}"
            external_id = await jira.add_comment(run.jira_issue_key, body)

    # record the send (idempotent by key = question_id:rung:attempt:out:channel)
    _, created = await outreach.record(
        question_id=question_id, channel=channel, direction="out",
        payload={"channel": channel, "rung": next_rung},
        rung=next_rung, attempt=1, external_message_id=external_id,
    )

    if not created:
        return {"action": "skipped", "reason": "already sent (idempotent)"}

    # advance the question's rung and reset SLA
    question.current_rung = next_rung
    question.sla_deadline = datetime.now(UTC) + timedelta(hours=_sla_hours(next_rung))
    await session.flush()

    await emit_event(
        session, actor="ladder", action="outreach.sent",
        detail={"question_id": question_id, "rung": next_rung, "channel": channel,
                "external_id": external_id},
    )
    return {"action": "sent", "channel": channel, "rung": next_rung}


async def process_sla_rungs(session: AsyncSession) -> list[dict]:
    """Called by /internal/tick. Advances all open questions past their SLA."""
    now = datetime.now(UTC)
    overdue = list(await session.scalars(
        select(Question).where(
            Question.status.in_(["open", "asked"]),
            Question.sla_deadline <= now,
        )
    ))
    results = []
    for q in overdue:
        result = await advance_question(q.question_id, session)
        results.append({"question_id": q.question_id, **result})
    return results
