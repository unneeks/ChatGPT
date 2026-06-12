"""In-memory Jira for unit tests and the eval harness. Records every call so tests
can assert exactly-once side effects."""

import itertools

from reqsmith.adapters.jira.port import JiraIssue


class FakeJira:
    def __init__(self):
        self.issues: dict[str, JiraIssue] = {}
        self.calls: list[tuple] = []
        self._comment_ids = itertools.count(1)
        self._issue_ids = itertools.count(100)

    def seed(self, issue: JiraIssue) -> None:
        self.issues[issue.key] = issue

    async def get_issue(self, key: str) -> JiraIssue:
        self.calls.append(("get_issue", key))
        return self.issues[key]

    async def add_comment(self, key: str, body: str) -> str:
        comment_id = str(next(self._comment_ids))
        self.calls.append(("add_comment", key, body))
        self.issues[key].comments.append({"id": comment_id, "author": "reqsmith", "body": body})
        return comment_id

    async def set_fields(self, key: str, fields: dict) -> None:
        self.calls.append(("set_fields", key, fields))
        self.issues[key].fields.update(fields)

    async def transition(self, key: str, transition_name: str) -> None:
        self.calls.append(("transition", key, transition_name))
        self.issues[key].status = transition_name

    async def search(self, jql: str, max_results: int = 20) -> list[JiraIssue]:
        self.calls.append(("search", jql))
        return list(self.issues.values())[:max_results]

    async def create_issue(
        self, project_key: str, issue_type: str, summary: str, description: str,
        parent_key: str | None = None,
    ) -> str:
        key = f"{project_key}-{next(self._issue_ids)}"
        self.calls.append(("create_issue", key, issue_type, summary))
        self.issues[key] = JiraIssue(
            key=key, issue_type=issue_type, summary=summary, description=description,
            status="To Do", reporter="reqsmith",
            fields={"parent": parent_key} if parent_key else {},
        )
        return key

    def comments_containing(self, key: str, marker: str) -> list[dict]:
        return [c for c in self.issues[key].comments if marker in c["body"]]
