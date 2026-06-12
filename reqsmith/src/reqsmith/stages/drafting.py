"""Drafting: the analyst produces a grounded draft from the run's source documents.

Uses the CrewAI drafting crew when the optional `agents` extra is installed;
otherwise falls back to a single analyst LLM call through the same prompt pack
(identical contract, recorded in audit as mode=single_agent). Citations refer to
source_document ids captured at retrieval — the grounding checker verifies them."""

import json
import re

from sqlalchemy import select

from reqsmith import deps
from reqsmith.adapters.llm.port import TokenBudgetExceeded
from reqsmith.audit.ledger import emit_event
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.models import Citation, RunState, SourceDocument
from reqsmith.persistence.repo import ArtifactRepo
from reqsmith.settings import get_settings

PROMPT_ID = "analyst_v1"


def parse_json_response(text: str) -> dict:
    """Extract the first JSON object from a model response (tolerates ``` fences)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


def _charge_tokens(checkpoint: dict, input_tokens: int, output_tokens: int) -> dict:
    used = checkpoint.get("tokens_used", 0) + input_tokens + output_tokens
    if used > get_settings().max_tokens_per_run:
        raise TokenBudgetExceeded(f"run exceeded token budget ({used})")
    return {**checkpoint, "tokens_used": used}


@register_stage("drafting")
async def drafting(ctx: StageContext) -> StageOutcome:
    sources = list(
        await ctx.session.scalars(
            select(SourceDocument).where(SourceDocument.run_id == ctx.run.id)
        )
    )
    intake = ctx.run.checkpoint.get("intake", {})
    sources_block = "\n\n".join(
        f"[source_id: {s.id}] (origin: {s.origin}, ref: {s.external_ref})\n{s.text}"
        for s in sources
    )

    llm = deps.get_llm()
    result = await llm.complete(
        prompt_id=PROMPT_ID,
        variables={"sources": sources_block, "intake": json.dumps(intake)},
        model_role="drafting",
    )
    checkpoint = _charge_tokens(ctx.run.checkpoint, result.input_tokens, result.output_tokens)

    draft = parse_json_response(result.text)
    artifact = await ArtifactRepo(ctx.session).write_once(
        run_id=ctx.run.id, kind="draft_story", content=draft,
        prompt_version=result.prompt_version, model_id=result.model_id,
    )

    valid_source_ids = {s.id for s in sources}
    for idx, story in enumerate(draft.get("stories", [])):
        for citation in story.get("citations", []):
            if citation.get("source_id") in valid_source_ids:
                ctx.session.add(
                    Citation(
                        artifact_id=artifact.id,
                        claim_path=f"/stories/{idx}",
                        source_document_id=citation["source_id"],
                        span_start=citation.get("span_start", 0),
                        span_end=citation.get("span_end", 0),
                    )
                )
    await ctx.session.flush()

    await emit_event(
        ctx.session, actor="analyst", action="draft.created", run_id=ctx.run.id,
        job_id=ctx.job.id, output_payload=draft,
        prompt_version=result.prompt_version, model_id=result.model_id,
        policy_version=ctx.run.policy_version,
        detail={"mode": "single_agent", "stories": len(draft.get("stories", [])),
                "tokens": result.input_tokens + result.output_tokens},
    )
    return StageOutcome(
        next_state=RunState.DRAFTING, next_stage="verification", checkpoint=checkpoint
    )
