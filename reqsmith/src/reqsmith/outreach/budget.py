"""Rate-cap enforcement for outreach (design §3a).

All rate caps are read from outreach-v1.yaml so they are version-controlled.
Caps are enforced *before* any send; the caller must check and abort if blocked.

Budget counters are derived from the append-only outreach_events table — no
separate mutable counter table, so there is no state to corrupt on retries.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith.persistence.models import OutreachEvent, Question
from reqsmith.verification.gates import load_policy_pack


def _policy():
    return load_policy_pack("outreach")


def _today_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start() -> datetime:
    now = datetime.now(UTC)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


async def _count_sent(
    session: AsyncSession,
    *,
    channel: str,
    since: datetime,
    stakeholder_aad_id: str | None = None,
) -> int:
    """Count outbound messages for the given channel since the cutoff time.

    If stakeholder_aad_id is supplied the count is scoped to questions for that
    stakeholder; otherwise the count is global.
    """
    if stakeholder_aad_id is not None:
        stmt = (
            select(func.count())
            .select_from(OutreachEvent)
            .join(Question, OutreachEvent.question_id == Question.question_id)
            .where(
                OutreachEvent.direction == "out",
                OutreachEvent.channel == channel,
                OutreachEvent.created_at >= since,
                Question.stakeholder_aad_id == stakeholder_aad_id,
            )
        )
    else:
        stmt = (
            select(func.count())
            .select_from(OutreachEvent)
            .where(
                OutreachEvent.direction == "out",
                OutreachEvent.channel == channel,
                OutreachEvent.created_at >= since,
            )
        )
    result = await session.scalar(stmt)
    return result or 0


async def check_send_allowed(
    session: AsyncSession,
    *,
    stakeholder_aad_id: str | None,
    channel: str,
) -> tuple[bool, str]:
    """Returns (allowed, reason). reason is empty when allowed.

    Checks (in order):
    1. Per-stakeholder chats-per-day cap (teams_card channel)
    2. Per-stakeholder meetings-per-week cap (meeting_invite channel)
    3. Global daily send budget across all channels
    """
    policy = _policy()
    limits = policy.get("rate_limits", {})

    if channel == "teams_card" and stakeholder_aad_id:
        cap = limits.get("per_stakeholder_chats_per_day", 1)
        sent = await _count_sent(
            session, channel="teams_card", since=_today_start(),
            stakeholder_aad_id=stakeholder_aad_id,
        )
        if sent >= cap:
            return False, f"per-stakeholder teams_card cap {cap}/day reached"

    if channel == "meeting_invite" and stakeholder_aad_id:
        cap = limits.get("per_stakeholder_meetings_per_week", 2)
        sent = await _count_sent(
            session, channel="meeting_invite", since=_week_start(),
            stakeholder_aad_id=stakeholder_aad_id,
        )
        if sent >= cap:
            return False, f"per-stakeholder meeting_invite cap {cap}/week reached"

    global_cap = limits.get("global_daily_send_budget", 50)
    # global count across all channels
    global_sent_stmt = (
        select(func.count())
        .select_from(OutreachEvent)
        .where(
            OutreachEvent.direction == "out",
            OutreachEvent.created_at >= _today_start(),
        )
    )
    global_sent = (await session.scalar(global_sent_stmt)) or 0
    if global_sent >= global_cap:
        return False, f"global daily send budget {global_cap} reached"

    return True, ""
