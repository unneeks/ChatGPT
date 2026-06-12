"""JiraPort — system-side Jira access (REST). Webhooks are hints; handlers re-fetch
the issue through this port before acting. Agent-facing reads go through the
Atlassian MCP adapter instead."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class JiraIssue:
    key: str
    issue_type: str
    summary: str
    description: str
    status: str
    reporter: str
    fields: dict = field(default_factory=dict)
    comments: list[dict] = field(default_factory=list)  # {id, author, body}


class JiraPort(Protocol):
    async def get_issue(self, key: str) -> JiraIssue: ...

    async def add_comment(self, key: str, body: str) -> str:
        """Returns the created comment id."""
        ...

    async def set_fields(self, key: str, fields: dict) -> None: ...

    async def transition(self, key: str, transition_name: str) -> None: ...

    async def search(self, jql: str, max_results: int = 20) -> list[JiraIssue]: ...

    async def create_issue(
        self, project_key: str, issue_type: str, summary: str, description: str,
        parent_key: str | None = None,
    ) -> str:
        """Returns the new issue key."""
        ...
