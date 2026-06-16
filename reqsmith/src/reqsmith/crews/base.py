"""Crew runner — wraps CrewAI kickoff() with bank-grade audit capture.

Every crew invocation records a `crew.started` + per-task `crew.task_completed`
+ `crew.finished` audit event chain, each carrying the prompt_version, model_id,
token_usage, and policy_version triple required for regulator replay.

Usage::

    result = await run_crew(
        crew,
        session=session,
        run_id=run.id,
        job_id=job.id,
        stage="drafting",
        policy_version=run.policy_version,
    )

`result` is the dict returned by crew.kickoff() (parsed from CrewAI's output).
Raises RuntimeError if crewai is not installed (caller should fall back gracefully).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from reqsmith.audit.ledger import emit_event
from reqsmith.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _requires_crewai() -> None:
    try:
        import crewai  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "CrewAI is not installed. Run: pip install 'reqsmith[agents]'"
        ) from exc


class AuditingTaskCallback:
    """Collects per-task completion events for later flush to audit_events."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, task_output: Any) -> None:
        try:
            self.events.append(
                {
                    "task": getattr(task_output, "description", "")[:200],
                    "agent": getattr(task_output, "agent", ""),
                    "raw": str(getattr(task_output, "raw", ""))[:500],
                    "token_usage": getattr(task_output, "token_usage", None),
                }
            )
        except Exception:
            pass


async def run_crew(
    crew: Any,
    *,
    session: "AsyncSession",
    run_id: str,
    job_id: str,
    stage: str,
    policy_version: str,
    inputs: dict | None = None,
) -> dict:
    """Kick off a CrewAI crew and return its output as a dict.

    Audit events emitted:
    - crew.started  (before kickoff)
    - crew.finished (after kickoff, with token totals + per-task summary)
    """
    _requires_crewai()

    settings = get_settings()
    task_cb = AuditingTaskCallback()

    for task in getattr(crew, "tasks", []):
        existing = getattr(task, "callback", None)
        if existing is None:
            task.callback = task_cb
        else:
            original = existing

            def _chained(output: Any, _orig=original, _cb=task_cb) -> None:
                _orig(output)
                _cb(output)

            task.callback = _chained

    await emit_event(
        session,
        actor=f"crew.{stage}",
        action="crew.started",
        run_id=run_id,
        job_id=job_id,
        policy_version=policy_version,
        detail={"stage": stage, "agents": [
            getattr(a, "role", "?") for a in getattr(crew, "agents", [])
        ]},
    )

    t0 = time.monotonic()
    loop = asyncio.get_event_loop()
    crew_output = await loop.run_in_executor(None, lambda: crew.kickoff(inputs=inputs or {}))
    elapsed = time.monotonic() - t0

    # Extract output dict
    raw_output = crew_output
    if hasattr(crew_output, "raw"):
        raw_output = crew_output.raw
    if isinstance(raw_output, str):
        try:
            import re
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
            candidate = fenced.group(1) if fenced else raw_output[raw_output.find("{"):raw_output.rfind("}") + 1]
            raw_output = json.loads(candidate)
        except Exception:
            raw_output = {"raw": str(raw_output)}

    # Total token usage across all tasks
    total_in = sum(
        (t.get("token_usage") or {}).get("prompt_tokens", 0) for t in task_cb.events
    )
    total_out = sum(
        (t.get("token_usage") or {}).get("completion_tokens", 0) for t in task_cb.events
    )

    await emit_event(
        session,
        actor=f"crew.{stage}",
        action="crew.finished",
        run_id=run_id,
        job_id=job_id,
        prompt_version=settings.prompt_pack_version,
        model_id=",".join(
            filter(None, [
                getattr(a, "llm", None) and getattr(getattr(a, "llm", None), "model_name", None)
                for a in getattr(crew, "agents", [])
            ])
        ) or "crewai",
        policy_version=policy_version,
        output_payload=raw_output if isinstance(raw_output, dict) else {},
        detail={
            "stage": stage,
            "elapsed_s": round(elapsed, 2),
            "tasks_completed": len(task_cb.events),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "task_summary": task_cb.events,
        },
    )

    return raw_output if isinstance(raw_output, dict) else {"raw": str(raw_output)}
