"""Publish: after human approval, create the stories in Jira, link the RTM graph,
stamp custom fields, and complete the run. All writes idempotent — a retried publish
never duplicates stories (checkpoint records created keys)."""

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.models import RunState
from reqsmith.persistence.repo import ArtifactRepo, RtmRepo
from reqsmith.settings import get_settings


@register_stage("publish")
async def publish(ctx: StageContext) -> StageOutcome:
    if ctx.run.state in (RunState.REVIEW, RunState.CHECKER_REVIEW):
        await ctx.run_repo.transition(ctx.run, RunState.PUBLISHING, detail={"stage": "publish"})

    jira = deps.get_jira()
    settings = get_settings()
    rtm = RtmRepo(ctx.session)
    artifact = await ArtifactRepo(ctx.session).latest(ctx.run.id, "draft_story")
    if artifact is None:
        raise RuntimeError("nothing approved to publish")

    epic_key = ctx.run.jira_issue_key
    project_key = settings.jira_project_key or epic_key.split("-")[0]
    already_published: dict = ctx.run.checkpoint.get("published_stories", {})

    for idx, story in enumerate(artifact.content.get("stories", [])):
        story_idx = str(idx)
        if story_idx in already_published:
            continue  # idempotent retry: skip stories created before a crash
        description = "\n".join(
            [story.get("story", ""), ""]
            + [f"AC{i + 1}: {ac}" for i, ac in enumerate(story.get("acceptance_criteria", []))]
            + ([""] + [f"NFR: {n}" for n in story.get("nfrs", [])] if story.get("nfrs") else [])
            + ["", f"Provenance: /runs/{ctx.run.id}/audit"]
        )
        new_key = await jira.create_issue(
            project_key, "Story", story.get("title", f"Story {idx + 1}"), description,
            parent_key=epic_key,
        )
        already_published[story_idx] = new_key
        await ctx.run_repo.save_checkpoint(
            ctx.run, {**ctx.run.checkpoint, "published_stories": already_published}
        )
        await rtm.link(
            from_type="story", from_ref=new_key, to_type="epic", to_ref=epic_key,
            edge_kind="derives_from", run_id=ctx.run.id,
        )
        await rtm.link(
            from_type="story", from_ref=new_key, to_type="artifact", to_ref=artifact.id,
            edge_kind="cites", run_id=ctx.run.id,
        )

    fields = {}
    if settings.jira_field_risk_tier and ctx.run.risk_tier:
        fields[settings.jira_field_risk_tier] = {"value": ctx.run.risk_tier.value}
    if settings.jira_field_provenance:
        fields[settings.jira_field_provenance] = f"/runs/{ctx.run.id}/audit"
    if fields:
        await jira.set_fields(epic_key, fields)

    await emit_event(
        ctx.session, actor="publisher", action="stories.published", run_id=ctx.run.id,
        job_id=ctx.job.id, output_payload=already_published,
        policy_version=ctx.run.policy_version,
        detail={"epic": epic_key, "stories": list(already_published.values())},
    )
    return StageOutcome(
        next_state=RunState.COMPLETE,
        checkpoint={**ctx.run.checkpoint, "published_stories": already_published},
    )
