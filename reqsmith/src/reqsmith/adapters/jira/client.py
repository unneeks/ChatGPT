"""Jira Cloud REST v3 implementation of JiraPort — system-side writes and the
re-fetch-before-act reads. Agent-facing search/reads go through the Atlassian MCP
adapter instead."""

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from reqsmith.adapters.jira.port import JiraIssue
from reqsmith.settings import get_settings

_RETRY = dict(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(4),
    reraise=True,
)


def _adf_to_text(adf: dict | str | None) -> str:
    """Flatten Atlassian Document Format to plain text (good enough for sourcing)."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    parts: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)
        if node.get("type") in ("paragraph", "heading"):
            parts.append("\n")

    walk(adf)
    return "".join(parts).strip()


def _text_to_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in text.split("\n")
            if line.strip()
        ],
    }


class JiraClient:
    def __init__(self, base_url: str | None = None, email: str | None = None,
                 api_token: str | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.jira_base_url).rstrip("/")
        self._auth = (email or settings.jira_email, api_token or settings.jira_api_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, auth=self._auth, timeout=30)

    @staticmethod
    def _to_issue(data: dict) -> JiraIssue:
        fields = data.get("fields", {})
        comments = [
            {
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("emailAddress")
                or (c.get("author") or {}).get("displayName", ""),
                "body": _adf_to_text(c.get("body")),
            }
            for c in ((fields.get("comment") or {}).get("comments") or [])
        ]
        return JiraIssue(
            key=data["key"],
            issue_type=(fields.get("issuetype") or {}).get("name", ""),
            summary=fields.get("summary") or "",
            description=_adf_to_text(fields.get("description")),
            status=(fields.get("status") or {}).get("name", ""),
            reporter=(fields.get("reporter") or {}).get("emailAddress", ""),
            fields=fields,
            comments=comments,
        )

    @retry(**_RETRY)
    async def get_issue(self, key: str) -> JiraIssue:
        async with self._client() as client:
            resp = await client.get(f"/rest/api/3/issue/{key}", params={"expand": "comment"})
            resp.raise_for_status()
            return self._to_issue(resp.json())

    @retry(**_RETRY)
    async def add_comment(self, key: str, body: str) -> str:
        async with self._client() as client:
            resp = await client.post(
                f"/rest/api/3/issue/{key}/comment", json={"body": _text_to_adf(body)}
            )
            resp.raise_for_status()
            return resp.json()["id"]

    @retry(**_RETRY)
    async def set_fields(self, key: str, fields: dict) -> None:
        async with self._client() as client:
            resp = await client.put(f"/rest/api/3/issue/{key}", json={"fields": fields})
            resp.raise_for_status()

    @retry(**_RETRY)
    async def transition(self, key: str, transition_name: str) -> None:
        async with self._client() as client:
            resp = await client.get(f"/rest/api/3/issue/{key}/transitions")
            resp.raise_for_status()
            transitions = resp.json().get("transitions", [])
            match = next(
                (t for t in transitions if t["name"].lower() == transition_name.lower()), None
            )
            if match is None:
                available = [t["name"] for t in transitions]
                raise ValueError(f"transition '{transition_name}' not available; have {available}")
            resp = await client.post(
                f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": match["id"]}}
            )
            resp.raise_for_status()

    @retry(**_RETRY)
    async def search(self, jql: str, max_results: int = 20) -> list[JiraIssue]:
        async with self._client() as client:
            resp = await client.get(
                "/rest/api/3/search/jql",
                params={"jql": jql, "maxResults": max_results,
                        "fields": "summary,description,issuetype,status,reporter"},
            )
            resp.raise_for_status()
            return [self._to_issue(i) for i in resp.json().get("issues", [])]

    @retry(**_RETRY)
    async def create_issue(
        self, project_key: str, issue_type: str, summary: str, description: str,
        parent_key: str | None = None,
    ) -> str:
        fields: dict = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _text_to_adf(description),
        }
        if parent_key:
            fields["parent"] = {"key": parent_key}
        async with self._client() as client:
            resp = await client.post("/rest/api/3/issue", json={"fields": fields})
            resp.raise_for_status()
            return resp.json()["key"]
