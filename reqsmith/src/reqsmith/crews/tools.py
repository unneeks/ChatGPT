"""CrewAI tool definitions — agent toolset for Jira/Confluence read access.

Agents MUST NOT write to Jira or Confluence through these tools; all writes go
through the REST adapter with idempotency keys. The allowlist in atlassian_mcp/client.py
enforces this at the MCP layer.

Usage::

    tools = get_atlassian_tools()  # empty list if MCP not configured
    agent = Agent(tools=tools, ...)

Audit note: MCP tool calls are captured in the crew audit events via the task callback
in crews/base.py (each task's token_usage includes MCP round-trips).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_atlassian_tools() -> list:
    """Return read-only CrewAI tools backed by the Atlassian Remote MCP server.

    Returns an empty list (with a log warning) if:
    - crewai-tools is not installed
    - MCP client ID is not configured
    - MCP server is unreachable at startup

    The caller (crew definitions) checks for empty tools and degrades gracefully
    (retrieval falls back to REST JQL-only, flagged in the crew audit event).
    """
    try:
        from crewai_tools import MCPServerAdapter  # type: ignore[import]
    except ImportError:
        logger.warning("crewai-tools not installed; Atlassian MCP tools unavailable")
        return []

    try:
        from reqsmith.adapters.atlassian_mcp.client import (
            READONLY_TOOL_ALLOWLIST,
            get_mcp_server_params,
        )
        params = get_mcp_server_params()
    except RuntimeError as exc:
        logger.info("Atlassian MCP not configured (%s); falling back to REST JQL-only", exc)
        return []

    try:
        adapter = MCPServerAdapter(
            server_params={
                "url": params.url,
                "headers": params.headers,
                "transport": "sse",
            }
        )
        tools = adapter.tools
        filtered = [t for t in tools if getattr(t, "name", "") in READONLY_TOOL_ALLOWLIST]
        logger.info("Atlassian MCP: %d read-only tools loaded", len(filtered))
        return filtered
    except Exception as exc:
        logger.warning("Atlassian MCP tool load failed (%s); falling back to REST JQL-only", exc)
        return []


def get_jira_rest_tools() -> list:
    """CrewAI-compatible tool wrappers around the REST JiraClient for fallback retrieval.

    Used when MCP is unavailable. These are thin wrappers that call the same REST
    adapter used by the stage pipeline, providing a consistent interface for agents.
    """
    try:
        from crewai.tools import BaseTool  # type: ignore[import]
        from pydantic import BaseModel, Field
    except ImportError:
        return []

    class JqlSearchInput(BaseModel):
        jql: str = Field(description="JQL query string")
        max_results: int = Field(default=10, description="Maximum results to return")

    class JiraIssueInput(BaseModel):
        key: str = Field(description="Jira issue key, e.g. BANK-123")

    class JqlSearchTool(BaseTool):
        name: str = "searchJiraIssuesUsingJql"
        description: str = "Search Jira issues using JQL. Returns issue keys, summaries, and descriptions."
        args_schema: type[BaseModel] = JqlSearchInput

        def _run(self, jql: str, max_results: int = 10) -> str:
            import asyncio
            from reqsmith import deps
            jira = deps.get_jira()
            issues = asyncio.get_event_loop().run_until_complete(jira.search(jql, max_results))
            return "\n\n".join(
                f"[{i.key}] {i.summary}\n{i.description[:500]}" for i in issues
            )

    class JiraIssueTool(BaseTool):
        name: str = "getJiraIssue"
        description: str = "Fetch a single Jira issue by key including description and comments."
        args_schema: type[BaseModel] = JiraIssueInput

        def _run(self, key: str) -> str:
            import asyncio
            from reqsmith import deps
            jira = deps.get_jira()
            issue = asyncio.get_event_loop().run_until_complete(jira.get_issue(key))
            comments = "\n".join(f"  [{c['author']}]: {c['body'][:300]}" for c in issue.comments)
            return f"[{issue.key}] {issue.summary}\n\n{issue.description}\n\nComments:\n{comments}"

    return [JqlSearchTool(), JiraIssueTool()]
