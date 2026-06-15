"""Meeting scheduler for rung-3 outreach (design §3a).

Enforces the rules from outreach-v1.yaml meeting section:
  - require_question_agenda: true  → a meeting with no agenda is impossible
  - human_owner_always_invited     → HUMAN_OWNER_AAD_ID is always an attendee
  - rebook_attempts: 1             → one rebook after a decline, then escalate

Entry point: `schedule_meeting_for_question(question_id, session)` is called by
the ladder when a question advances to rung 3 (meeting_invite channel).

No LLM agents involved — this is pure orchestration code. The agenda is
constructed deterministically from the stored question text.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.outreach.budget import check_send_allowed
from reqsmith.persistence.models import Question, Run
from reqsmith.persistence.repo import OutreachRepo
from reqsmith.settings import get_settings
from reqsmith.verification.gates import load_policy_pack


def _meeting_policy() -> dict:
    return load_policy_pack("outreach").get("meeting", {})


def _build_agenda(question: Question, issue_key: str) -> str:
    """Build a structured meeting agenda from the question text.

    Returns empty string if insufficient content (caller must check before
    creating the invite — "no agenda → no invite").
    """
    if not question.text or len(question.text.strip()) < 10:
        return ""
    lines = [
        f"Requirements Clarification — {issue_key}",
        "",
        "Agenda:",
        f"  1. {question.text.strip()}",
        "",
        "Please come prepared with:",
        "  - Relevant documentation or constraints",
        "  - Decision authority or delegation",
        "",
        "This meeting was scheduled automatically by the Requirements Assistant.",
        "Reply to this invite or contact your project coordinator to reschedule.",
    ]
    return "\n".join(lines)


async def schedule_meeting_for_question(
    question_id: str, session: AsyncSession, *, attempt: int = 1
) -> dict:
    """Schedule a clarification meeting for a question at rung 3.

    Enforces: agenda gate, human-owner attendee, rebook-once, budget cap.
    `attempt=1` (default) sends the initial invite and is idempotent if already sent.
    `attempt=2` sends the rebook invite; escalates if rebook limit already reached.

    Returns {"action": "scheduled"|"skipped"|"blocked"|"escalated", ...}
    """
    question = await session.scalar(
        select(Question).where(Question.question_id == question_id)
    )
    if question is None:
        return {"action": "skipped", "reason": "question not found"}
    if question.status not in ("open", "asked"):
        return {"action": "skipped", "reason": f"question is '{question.status}'"}

    run = await session.get(Run, question.run_id)
    if run is None:
        return {"action": "skipped", "reason": "run not found"}

    settings = get_settings()

    # --- agenda gate ("no agenda → no invite") ---
    agenda = _build_agenda(question, run.jira_issue_key)
    if not agenda:
        await emit_event(
            session, actor="scheduler", action="meeting.blocked_no_agenda",
            detail={"question_id": question_id},
        )
        return {"action": "blocked", "reason": "no question content for agenda"}

    # --- human owner must always be invited ---
    human_owner = settings.human_owner_aad_id
    attendees: list[str] = []
    if question.stakeholder_aad_id:
        attendees.append(question.stakeholder_aad_id)
    if human_owner and human_owner not in attendees:
        attendees.append(human_owner)
    if not attendees:
        return {"action": "skipped", "reason": "no attendees to invite"}

    # --- idempotency / rebook check (before budget — skips/escalates don't burn budget) ---
    outreach = OutreachRepo(session)
    from reqsmith.persistence.models import OutreachEvent

    attempt_key_1 = f"{question_id}:3:1:out:meeting_invite"
    attempt_key_2 = f"{question_id}:3:2:out:meeting_invite"

    if attempt == 1:
        existing_initial = await session.scalar(
            select(OutreachEvent).where(OutreachEvent.idempotency_key == attempt_key_1)
        )
        if existing_initial is not None:
            return {"action": "skipped", "reason": "already scheduled (idempotent)"}
    else:
        # attempt == 2: rebook path — escalate if rebook already recorded
        existing_rebook = await session.scalar(
            select(OutreachEvent).where(OutreachEvent.idempotency_key == attempt_key_2)
        )
        if existing_rebook is not None:
            if question.status != "escalated":
                question.status = "escalated"
                await session.flush()
                await emit_event(
                    session, actor="scheduler", action="question.escalated",
                    detail={"question_id": question_id, "reason": "rebook limit reached"},
                )
            return {"action": "escalated", "reason": "rebook limit reached"}

    # --- budget check ---
    allowed, reason = await check_send_allowed(
        session,
        stakeholder_aad_id=question.stakeholder_aad_id,
        channel="meeting_invite",
    )
    if not allowed:
        await emit_event(
            session, actor="scheduler", action="outreach.budget_blocked",
            detail={"question_id": question_id, "channel": "meeting_invite", "reason": reason},
        )
        return {"action": "blocked", "reason": reason}

    # --- find available meeting time ---
    graph = deps.get_graph()
    organizer = human_owner or (attendees[0] if attendees else "")
    slots = await graph.find_meeting_times(
        organizer=organizer,
        attendees=attendees,
        duration_minutes=30,
    )
    if not slots:
        await emit_event(
            session, actor="scheduler", action="meeting.no_slots",
            detail={"question_id": question_id},
        )
        return {"action": "skipped", "reason": "no available meeting slots found"}

    slot = slots[0]
    subject = f"[REQ-Q:{question_id}] Requirements Clarification — {run.jira_issue_key}"

    # --- create the calendar event ---
    event_id = await graph.create_event(
        organizer=organizer,
        attendees=attendees,
        slot=slot,
        subject=subject,
        agenda=agenda,
    )

    # record idempotently
    _, created = await outreach.record(
        question_id=question_id,
        channel="meeting_invite",
        direction="out",
        payload={"subject": subject, "slot_start": slot.start.isoformat(),
                 "attendees": attendees, "attempt": attempt},
        rung=3,
        attempt=attempt,
        external_message_id=event_id,
    )

    if not created:
        return {"action": "skipped", "reason": "already scheduled (idempotent)"}

    rebook_note = " (rebook)" if attempt == 2 else ""
    await emit_event(
        session, actor="scheduler", action="meeting.scheduled",
        detail={"question_id": question_id, "event_id": event_id,
                "slot_start": slot.start.isoformat(), "attendees": attendees,
                "attempt": attempt},
    )
    return {
        "action": "scheduled",
        "event_id": event_id,
        "slot_start": slot.start.isoformat(),
        "attendees": attendees,
        "attempt": attempt,
        "note": f"meeting invite sent{rebook_note}",
    }
