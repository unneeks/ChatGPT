"""Atlassian Remote MCP adapter — agent-facing reads (Jira search/read, Confluence).

The crews consume these tools via crewai-tools' MCPServerAdapter (see crews/tools.py).
This module owns the connection parameters and the read-only tool allowlist; if the
MCP server is unavailable the retrieval stage degrades to REST JQL-only (flagged in
audit) rather than failing the run.
"""

from dataclasses import dataclass

from reqsmith.settings import get_settings

# Read-only allowlist: agents must never write to Jira/Confluence through MCP.
# Writes go through the REST adapter with idempotency keys.
READONLY_TOOL_ALLOWLIST = (
    "search",                # rovo unified search
    "getJiraIssue",
    "searchJiraIssuesUsingJql",
    "getConfluencePage",
    "searchConfluenceUsingCql",
    "getPagesInConfluenceSpace",
)


@dataclass
class McpServerParams:
    url: str
    headers: dict


def get_mcp_server_params() -> McpServerParams:
    """Connection parameters for crewai-tools MCPServerAdapter (SSE transport)."""
    settings = get_settings()
    if not settings.atlassian_mcp_client_id:
        raise RuntimeError(
            "Atlassian MCP not configured (ATLASSIAN_MCP_CLIENT_ID empty); "
            "retrieval degrades to REST JQL-only"
        )
    return McpServerParams(
        url=settings.atlassian_mcp_url,
        headers={"Authorization": f"Bearer {settings.atlassian_mcp_refresh_token}"},
    )


def filter_readonly(tools: list) -> list:
    """Keep only allowlisted read tools, by tool name."""
    return [t for t in tools if getattr(t, "name", "") in READONLY_TOOL_ALLOWLIST]
