"""Append-only audit ledger — the regulator-replay record.

Every agent action, gate verdict, human decision, and external send goes through
emit_event(). The ledger only ever INSERTs; updates/deletes are blocked by a DB
trigger in Postgres and by not existing in this API.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith.persistence.idempotency import content_hash
from reqsmith.persistence.models import AuditEvent


async def emit_event(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    run_id: str | None = None,
    job_id: str | None = None,
    input_payload: Any | None = None,
    output_payload: Any | None = None,
    prompt_version: str | None = None,
    model_id: str | None = None,
    policy_version: str | None = None,
    detail: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        run_id=run_id,
        job_id=job_id,
        actor=actor,
        action=action,
        input_hash=content_hash(input_payload) if input_payload is not None else None,
        output_hash=content_hash(output_payload) if output_payload is not None else None,
        prompt_version=prompt_version,
        model_id=model_id,
        policy_version=policy_version,
        detail=detail,
    )
    session.add(event)
    await session.flush()
    return event
