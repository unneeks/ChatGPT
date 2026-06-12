"""Retrieval-lite: search the existing backlog (JQL via REST; Atlassian MCP adds
Confluence when configured) and capture every comment as a citable source document.
Returns cited context bundles only — uncited context is never given to the analyst."""

from reqsmith import deps
from reqsmith.orchestrator.engine import StageContext, StageOutcome, register_stage
from reqsmith.persistence.idempotency import content_hash
from reqsmith.persistence.models import RunState, SourceDocument
from reqsmith.persistence.repo import ArtifactRepo
from reqsmith.settings import get_settings


@register_stage("retrieval")
async def retrieval(ctx: StageContext) -> StageOutcome:
    jira = deps.get_jira()
    settings = get_settings()
    issue = await jira.get_issue(ctx.run.jira_issue_key)

    # comments become citable sources (answers to triage questions live here)
    for comment in issue.comments:
        ctx.session.add(
            SourceDocument(
                run_id=ctx.run.id, origin="jira_comment",
                external_ref=f"{issue.key}#comment-{comment['id']}",
                text=comment["body"], text_hash=content_hash(comment["body"]),
            )
        )

    # related backlog items (duplicate/conflict detection input)
    related = []
    if settings.jira_project_key:
        jql = (
            f'project = "{settings.jira_project_key}" AND key != "{issue.key}" '
            f"ORDER BY updated DESC"
        )
        for item in await jira.search(jql, max_results=10):
            related.append({"key": item.key, "summary": item.summary, "status": item.status})
            ctx.session.add(
                SourceDocument(
                    run_id=ctx.run.id, origin="jira_backlog", external_ref=item.key,
                    text=f"{item.summary}\n{item.description}",
                    text_hash=content_hash(item.summary + item.description),
                )
            )
    await ctx.session.flush()

    await ArtifactRepo(ctx.session).write_once(
        run_id=ctx.run.id, kind="context_bundle",
        content={"related_issues": related, "comment_count": len(issue.comments)},
        prompt_version="n/a", model_id="n/a",
    )
    return StageOutcome(next_state=RunState.RETRIEVAL, next_stage="drafting")
