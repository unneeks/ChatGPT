"""Verification: Layer-1 gates on the draft, Layer-3 grounding, Layer-2 judge.
All verdicts persist to gate_results. Pass → post the draft for human review on the
Jira issue and stop (human-on-the-loop). Fail → block, never silently publish."""

import json

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.models import RunState
from reqsmith.persistence.repo import ArtifactRepo, GateResultRepo
from reqsmith.stages.drafting import parse_json_response
from reqsmith.verification.gates import evaluate
from reqsmith.verification.grounding import check_grounding
from reqsmith.verification.scoring import JUDGE_PASS_THRESHOLD, score_draft

JUDGE_PROMPT_ID = "judge_rubric_v1"
REVIEW_MARKER = "[REQ-REVIEW]"


@register_stage("verification")
async def verification(ctx: StageContext) -> StageOutcome:
    # enter the verification state explicitly (one transition per hop, all audited)
    if ctx.run.state == RunState.DRAFTING:
        await ctx.run_repo.transition(ctx.run, RunState.VERIFICATION, detail={"stage": "verification"})

    artifacts = ArtifactRepo(ctx.session)
    gate_repo = GateResultRepo(ctx.session)
    artifact = await artifacts.latest(ctx.run.id, "draft_story")
    if artifact is None:
        raise RuntimeError("no draft to verify")
    draft = artifact.content

    # Layer 1 — deterministic gates
    report = evaluate(draft, applies_to="draft")
    for result in report.results:
        await gate_repo.record(
            run_id=ctx.run.id, artifact_id=artifact.id, layer=1, rule_id=result.rule_id,
            verdict=result.verdict,
            reasoning=result.message if result.verdict == "fail" else None,
            policy_version=report.policy_version,
        )

    # Layer 3 — grounding (orphan claims hard-block)
    grounding = await check_grounding(ctx.session, artifact)
    await gate_repo.record(
        run_id=ctx.run.id, artifact_id=artifact.id, layer=3, rule_id="grounding.citations",
        verdict="pass" if grounding.passed else "fail",
        score=grounding.grounded,
        reasoning=None if grounding.passed else f"orphan claims: {grounding.orphans}",
        policy_version=report.policy_version,
    )

    # Layer 2 — heuristics + independent judge (separate prompt lineage + model tier)
    heuristics = score_draft(draft)
    await gate_repo.record(
        run_id=ctx.run.id, artifact_id=artifact.id, layer=2, rule_id="scoring.ambiguity",
        verdict="pass" if heuristics.ambiguity_score >= 7 else "fail",
        score=heuristics.ambiguity_score,
        reasoning=f"vague terms: {heuristics.ambiguity_hits}" if heuristics.ambiguity_hits else None,
        policy_version=report.policy_version,
    )

    llm = deps.get_llm()
    intake = ctx.run.checkpoint.get("intake", {})
    judge_result = await llm.complete(
        prompt_id=JUDGE_PROMPT_ID,
        variables={"draft": json.dumps(draft), "intake": json.dumps(intake)},
        model_role="judge",
    )
    judge = parse_json_response(judge_result.text)
    judge_overall = float(judge.get("overall", 0))
    await gate_repo.record(
        run_id=ctx.run.id, artifact_id=artifact.id, layer=2, rule_id="judge.rubric",
        verdict="pass" if judge_overall >= JUDGE_PASS_THRESHOLD else "fail",
        score=judge_overall, reasoning=judge.get("reasoning"),
        policy_version=report.policy_version,
    )
    await emit_event(
        ctx.session, actor="judge", action="judge.scored", run_id=ctx.run.id, job_id=ctx.job.id,
        output_payload=judge, prompt_version=judge_result.prompt_version,
        model_id=judge_result.model_id, policy_version=ctx.run.policy_version,
        detail={"overall": judge_overall, "blocking_issues": judge.get("blocking_issues", [])},
    )

    hard_blocked = bool(report.blocking_failures) or not grounding.passed
    if hard_blocked:
        # fixable by re-draft? gates/grounding say what failed; one re-draft attempt,
        # then quarantine for operator attention
        redrafts = ctx.run.checkpoint.get("redrafts", 0)
        if redrafts < 1:
            return StageOutcome(
                next_state=RunState.DRAFTING, next_stage="drafting",
                checkpoint={**ctx.run.checkpoint, "redrafts": redrafts + 1},
            )
        return StageOutcome(next_state=RunState.QUARANTINED)

    # judge below threshold is NOT a block — it routes to human with reasoning attached
    jira = deps.get_jira()
    tier = ctx.run.risk_tier.value if ctx.run.risk_tier else "medium"
    summary_lines = [
        f"{REVIEW_MARKER} Draft requirements ready for review (run {ctx.run.id}).",
        f"Risk tier: {tier} | judge score: {judge_overall:.1f} | "
        f"stories: {heuristics.stories_count} | citation coverage: "
        f"{grounding.grounded}/{grounding.total_claims}",
        "Approve via workflow transition 'Approved' (high tier requires independent checker), "
        "or 'Rejected' to send back.",
    ]
    for idx, story in enumerate(draft.get("stories", []), start=1):
        summary_lines.append(f"Story {idx}: {story.get('title', '')}")
    await jira.add_comment(ctx.run.jira_issue_key, "\n".join(summary_lines))

    return StageOutcome(
        next_state=RunState.REVIEW,
        needs_human=True,
        checkpoint={**ctx.run.checkpoint, "judge_overall": judge_overall},
    )
