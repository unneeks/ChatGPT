"""Triage: re-fetch the issue (webhook was only a hint), snapshot intake, run
intake gates, set the risk tier deterministically, and either advance to retrieval
or post structured questions back to Jira and wait."""

import uuid

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.idempotency import content_hash, insert_or_get
from reqsmith.persistence.models import Question, RiskTier, RunState, SourceDocument
from reqsmith.persistence.repo import ArtifactRepo, GateResultRepo, OutreachRepo
from reqsmith.verification.gates import classify_risk_tier, evaluate

QUESTION_MARKER = "[REQ-Q:{qid}]"

GATE_QUESTION_TEXT = {
    "intake.description.min_length": (
        "Please expand the description: what problem are we solving, what business outcome is "
        "expected, which systems are impacted, and what data is touched?"
    ),
    "intake.summary.present": "Please provide a one-line summary of the demand.",
    "intake.reporter.present": "Who is the business sponsor for this demand?",
}


@register_stage("triage")
async def triage(ctx: StageContext) -> StageOutcome:
    jira = deps.get_jira()
    issue = await jira.get_issue(ctx.run.jira_issue_key)

    intake = {
        "key": issue.key,
        "issue_type": issue.issue_type,
        "summary": issue.summary,
        "description": issue.description,
        "reporter": issue.reporter,
    }

    # snapshot the intake as a source document (citation target) + artifact
    source = SourceDocument(
        run_id=ctx.run.id,
        origin="jira_description",
        external_ref=issue.key,
        text=f"{issue.summary}\n\n{issue.description}",
        text_hash=content_hash(issue.description),
    )
    ctx.session.add(source)
    await ctx.session.flush()
    await ArtifactRepo(ctx.session).write_once(
        run_id=ctx.run.id, kind="intake_snapshot", content=intake,
        prompt_version="n/a", model_id="n/a",
    )

    # deterministic risk tier — never set by an LLM
    tier, tier_rule = classify_risk_tier(f"{issue.summary}\n{issue.description}")
    ctx.run.risk_tier = RiskTier(tier)
    await emit_event(
        ctx.session, actor="triage", action="risk_tier.assigned", run_id=ctx.run.id,
        policy_version=ctx.run.policy_version, detail={"tier": tier, "rule": tier_rule},
    )

    # intake gates
    report = evaluate(intake, applies_to="intake")
    gate_repo = GateResultRepo(ctx.session)
    for result in report.results:
        await gate_repo.record(
            run_id=ctx.run.id, layer=1, rule_id=result.rule_id, verdict=result.verdict,
            reasoning=result.message if result.verdict == "fail" else None,
            policy_version=report.policy_version,
        )

    if report.passed:
        return StageOutcome(
            next_state=RunState.TRIAGE, next_stage="retrieval",
            checkpoint={"intake": intake, "risk_tier": tier},
        )

    # incomplete intake → structured questions on the issue, rung 1, then wait
    outreach = OutreachRepo(ctx.session)
    for failure in report.blocking_failures:
        question_text = GATE_QUESTION_TEXT.get(
            failure.rule_id, f"Intake check failed: {failure.message}"
        )
        # deterministic question id per (run, rule) → retries reuse the same question
        qid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{ctx.run.id}:{failure.rule_id}"))
        question = Question(
            run_id=ctx.run.id, question_id=qid, text=question_text,
            stakeholder_display=issue.reporter, status="asked", current_rung=1,
        )
        question, created = await insert_or_get(
            ctx.session, question, Question, Question.question_id, qid
        )
        if created:
            body = f"{QUESTION_MARKER.format(qid=qid)} {question_text}"
            comment_id = await jira.add_comment(issue.key, body)
            await outreach.record(
                question_id=qid, channel="jira_comment", direction="out",
                payload={"body": body}, rung=1, external_message_id=comment_id,
            )

    # ensure run leaves intake before waiting (first pass arrives in 'intake')
    if ctx.run.state == RunState.INTAKE:
        await ctx.run_repo.transition(ctx.run, RunState.TRIAGE, detail={"stage": "triage"})
    return StageOutcome(
        next_state=RunState.AWAITING_INPUT, needs_human=True,
        checkpoint={"intake": intake, "risk_tier": tier,
                    "open_gates": [f.rule_id for f in report.blocking_failures]},
    )
