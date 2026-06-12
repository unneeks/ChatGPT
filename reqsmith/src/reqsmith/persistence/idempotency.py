"""Idempotency helpers: natural-key UNIQUE + insert-or-noop + read-back.

Pattern (design doc §1): every external side effect and every run/event row is keyed
by a client-generated natural key; a retried operation collapses into a no-op and the
caller reads back the original row.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def content_hash(payload: Any) -> str:
    """Stable sha256 over JSON-serializable content."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def insert_or_get(session: AsyncSession, instance, model, key_column, key_value):
    """INSERT; on unique-key conflict, return the existing row instead.

    Uses a SAVEPOINT so the surrounding transaction survives the conflict on
    every backend (works for SQLite tests and Postgres alike).
    """
    try:
        async with session.begin_nested():
            session.add(instance)
            await session.flush()
        return instance, True
    except IntegrityError:
        existing = await session.scalar(select(model).where(key_column == key_value))
        if existing is None:  # conflict was something else — re-raise semantics
            raise
        return existing, False
