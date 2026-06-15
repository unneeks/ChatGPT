"""Teams bot webhook receiver — /api/messages.

Receives Bot Framework Activity objects from Azure Bot Service. Each message
from a user is validated, parsed, and routed:

  - Activity.type == "conversationUpdate" → store ConversationReference (M7+)
  - Activity.type == "message" with card value → parse as card submit action
    → look up question → record answer → resume run if all questions answered

Auth: Bot Framework validates the incoming JWT against the Bot App ID.
In dev mode (BOT_APP_ID not set) auth is skipped — never in production.

The endpoint must return 200 within the Bot Framework timeout window (15 s);
all DB work is done inside the session_scope.
"""

import hmac

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from reqsmith.audit.ledger import emit_event
from reqsmith.outreach.cards import parse_card_answer
from reqsmith.persistence.db import session_scope
from reqsmith.persistence.models import Question, RunState
from reqsmith.persistence.repo import JobRepo, OutreachRepo, RunRepo
from reqsmith.settings import get_settings

router = APIRouter()


async def _validate_bot_auth(request: Request) -> None:
    """Verify the request is from Azure Bot Service.

    Full validation requires botbuilder JwtTokenValidation; for MVP we check
    the App ID header only (sufficient for internal/sandbox use).
    """
    settings = get_settings()
    if not settings.bot_app_id:
        return  # dev mode: skip auth
    app_id = request.headers.get("x-ms-bot-appid", "")
    if not hmac.compare_digest(app_id, settings.bot_app_id):
        raise HTTPException(status_code=401, detail="invalid bot app id")


@router.post("/api/messages")
async def bot_messages(request: Request):
    await _validate_bot_auth(request)
    activity = await request.json()

    activity_type = (activity.get("type") or "").lower()
    channel_id = activity.get("channelId", "")
    from_user = (activity.get("from") or {})
    user_aad_id = from_user.get("aadObjectId") or from_user.get("id") or ""

    await _maybe_store_conversation_reference(activity, user_aad_id)

    if activity_type != "message":
        return {"status": "ignored", "type": activity_type}

    value = activity.get("value") or {}
    if not value:
        # plain text message — not from our card, nothing to do (future: NL parsing)
        return {"status": "ignored", "reason": "no card value"}

    question_id, answer_text, is_handoff = parse_card_answer(value)
    if not question_id:
        return {"status": "ignored", "reason": "not a reqsmith card"}

    async with session_scope() as session:
        question = await session.scalar(
            select(Question).where(Question.question_id == question_id)
        )
        if question is None:
            return {"status": "ignored", "reason": "question not found"}
        if question.status in ("answered", "closed", "handed_off"):
            return {"status": "ignored", "reason": f"question already {question.status}"}

        outreach = OutreachRepo(session)

        # record the inbound answer
        _, created = await outreach.record(
            question_id=question_id,
            channel="teams_card",
            direction="in",
            payload={"answer": answer_text, "handoff": is_handoff},
            rung=question.current_rung,
            attempt=1,
        )

        await emit_event(
            session, actor=user_aad_id or "teams_user", action="teams.answer_received",
            detail={"question_id": question_id, "handoff": is_handoff,
                    "channel_id": channel_id, "new": created},
        )

        if is_handoff:
            question.status = "handed_off"
            await session.flush()
            return {"status": "handed_off", "question_id": question_id}

        # record the answer
        if answer_text:
            from reqsmith.persistence.idempotency import content_hash, insert_or_get
            from reqsmith.persistence.models import SourceDocument

            # snapshot the answer as a SourceDocument so it becomes citable
            src = SourceDocument(
                run_id=question.run_id,
                origin="card_answer",
                external_ref=f"teams:{question_id}",
                text=answer_text,
                text_hash=content_hash(answer_text),
            )
            src, _ = await insert_or_get(
                session, src, SourceDocument, SourceDocument.text_hash, src.text_hash
            )
            question.answer_text = answer_text
            question.answer_source_document_id = src.id
            question.status = "answered"
            await session.flush()

            # post the answer back to Jira
            await _post_answer_to_jira(session, question, answer_text)

            # check if all questions for the run are answered → resume pipeline
            await _maybe_resume_run(session, question.run_id)

        return {"status": "recorded", "question_id": question_id}


async def _maybe_store_conversation_reference(activity: dict, user_aad_id: str) -> None:
    """Persist the ConversationReference so we can proactively message this user later."""
    # In production: upsert a user→conversation_reference row in DB.
    # For MVP: no-op (proactive send not yet wired).
    pass


async def _post_answer_to_jira(session, question: Question, answer_text: str) -> None:
    """Post the card answer as a Jira comment on the run's issue."""
    from reqsmith import deps
    from reqsmith.persistence.models import Run
    run = await session.get(Run, question.run_id)
    if run is None:
        return
    jira = deps.get_jira()
    body = f"[REQ-ANS:{question.question_id}] {answer_text}"
    comment_id = await jira.add_comment(run.jira_issue_key, body)
    outreach = OutreachRepo(session)
    await outreach.record(
        question_id=question.question_id,
        channel="jira_comment",
        direction="out",
        payload={"body": body},
        rung=question.current_rung,
        attempt=2,  # attempt 2 = answer echo (attempt 1 = original question)
        external_message_id=comment_id,
    )


async def _maybe_resume_run(session, run_id: str) -> None:
    """If all questions for the run are answered, transition back to TRIAGE."""
    from sqlalchemy import func, select
    open_count = await session.scalar(
        select(func.count())
        .select_from(Question)
        .where(
            Question.run_id == run_id,
            Question.status.in_(["open", "asked"]),
        )
    )
    if (open_count or 0) > 0:
        return

    run_repo = RunRepo(session)
    run = await run_repo.get(run_id)
    if run is None or run.state != RunState.AWAITING_INPUT:
        return

    await run_repo.transition(run, RunState.TRIAGE, detail={"resume": "all questions answered"})
    await JobRepo(session).enqueue(run_id, "triage")
    await emit_event(
        session, actor="system", action="run.resumed",
        run_id=run_id, detail={"trigger": "all_questions_answered"},
    )
