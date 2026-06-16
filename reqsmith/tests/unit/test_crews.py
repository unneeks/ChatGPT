"""Unit tests for the CrewAI crews layer (T4.2, T4.3, T4.4, T5.1).

All tests use fakes — no real LLM calls, no MCP server. CrewAI itself IS imported
(it's installed in the dev env) but the actual model calls are stubbed by swapping
the crew's LLM with a fake that returns fixed JSON fixtures.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_crew_output(data: dict):
    """Simulate what crew.kickoff() returns."""
    mock = MagicMock()
    mock.raw = json.dumps(data)
    return mock


# ---------------------------------------------------------------------------
# crews/base.py
# ---------------------------------------------------------------------------

class TestCrewRunner:
    async def test_run_crew_emits_started_and_finished_events(self, tmp_path):
        """run_crew() must emit crew.started + crew.finished audit events."""
        from reqsmith.crews.base import run_crew

        draft_fixture = {"epic_summary": "Test", "stories": []}

        fake_crew = MagicMock()
        fake_crew.agents = []
        fake_crew.tasks = []
        fake_crew.kickoff = MagicMock(return_value=_fake_crew_output(draft_fixture))

        events: list[dict] = []

        async def fake_emit(session, *, actor, action, **kwargs):
            events.append({"actor": actor, "action": action, **kwargs})

        with patch("reqsmith.crews.base.emit_event", new=fake_emit):
            result = await run_crew(
                fake_crew,
                session=AsyncMock(),
                run_id="run-1",
                job_id="job-1",
                stage="drafting",
                policy_version="v1",
            )

        assert result == draft_fixture
        actions = [e["action"] for e in events]
        assert "crew.started" in actions
        assert "crew.finished" in actions

    async def test_run_crew_returns_raw_dict_when_output_is_string(self, tmp_path):
        """If kickoff returns a string, run_crew parses it to a dict."""
        from reqsmith.crews.base import run_crew

        raw_json = '{"stories": [], "epic_summary": "hello"}'
        fake_crew = MagicMock()
        fake_crew.agents = []
        fake_crew.tasks = []
        mock_output = MagicMock()
        mock_output.raw = raw_json
        fake_crew.kickoff = MagicMock(return_value=mock_output)

        with patch("reqsmith.crews.base.emit_event", new=AsyncMock()):
            result = await run_crew(
                fake_crew,
                session=AsyncMock(),
                run_id="run-1",
                job_id="job-1",
                stage="test",
                policy_version="v1",
            )

        assert result["epic_summary"] == "hello"

    async def test_run_crew_raises_without_crewai(self):
        """run_crew() raises RuntimeError with a clear message if crewai not importable."""
        from reqsmith.crews.base import run_crew

        with patch.dict("sys.modules", {"crewai": None}):
            # Patch _requires_crewai to simulate ImportError
            with patch("reqsmith.crews.base._requires_crewai", side_effect=RuntimeError("not installed")):
                with pytest.raises(RuntimeError, match="not installed"):
                    await run_crew(
                        MagicMock(),
                        session=AsyncMock(),
                        run_id="r",
                        job_id="j",
                        stage="s",
                        policy_version="v1",
                    )


# ---------------------------------------------------------------------------
# crews/tools.py
# ---------------------------------------------------------------------------

class TestAtlassianTools:
    def test_get_atlassian_tools_returns_empty_when_mcp_unconfigured(self):
        """When MCP client ID is blank, get_atlassian_tools() returns []."""
        from reqsmith.crews.tools import get_atlassian_tools

        with patch("reqsmith.adapters.atlassian_mcp.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(atlassian_mcp_client_id="")
            result = get_atlassian_tools()

        assert result == []

    def test_get_jira_rest_tools_returns_two_tools(self):
        """get_jira_rest_tools() returns JQL search + issue fetch tools."""
        from reqsmith.crews.tools import get_jira_rest_tools

        tools = get_jira_rest_tools()
        names = {getattr(t, "name", "") for t in tools}
        assert "searchJiraIssuesUsingJql" in names
        assert "getJiraIssue" in names

    def test_rest_tool_jql_calls_jira_adapter(self):
        """JQL search tool calls jira.search() and formats results."""
        from reqsmith.crews.tools import get_jira_rest_tools
        from reqsmith.adapters.jira.port import JiraIssue

        fake_issue = JiraIssue(
            key="BANK-1", issue_type="Epic", summary="Test epic",
            description="A test description", status="To Do", reporter="dev@test.com"
        )

        import asyncio

        with patch("reqsmith.deps.get_jira") as mock_get_jira:
            fake_jira = MagicMock()
            fake_jira.search = AsyncMock(return_value=[fake_issue])
            mock_get_jira.return_value = fake_jira

            tools = get_jira_rest_tools()
            jql_tool = next(t for t in tools if t.name == "searchJiraIssuesUsingJql")

            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.run_until_complete = lambda coro: asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
                result = jql_tool._run(jql="project = BANK", max_results=5)

        assert "BANK-1" in result
        assert "Test epic" in result


# ---------------------------------------------------------------------------
# crews/drafting_crew.py
# ---------------------------------------------------------------------------

class TestDraftingCrew:
    def test_build_drafting_crew_has_three_agents(self):
        """build_drafting_crew() returns a Crew with retrieval, analyst, elicitation."""
        from reqsmith.crews.drafting_crew import build_drafting_crew

        crew = build_drafting_crew(
            jira_project_key="BANK",
            atlassian_tools=[],  # force REST-only for test speed
            drafting_model="claude-sonnet-4-6",
        )
        assert len(crew.agents) == 3
        roles = {a.role for a in crew.agents}
        assert any("Retrieval" in r for r in roles)
        assert any("Analyst" in r for r in roles)
        assert any("Elicitation" in r for r in roles)

    def test_build_drafting_crew_has_three_tasks(self):
        """Crew must have exactly 3 tasks (retrieval, analyst, elicitation)."""
        from reqsmith.crews.drafting_crew import build_drafting_crew

        crew = build_drafting_crew(jira_project_key="BANK", atlassian_tools=[])
        assert len(crew.tasks) == 3

    def test_analyst_task_has_no_tools(self):
        """Analyst and elicitation agents must have no tools (no direct Jira writes)."""
        from reqsmith.crews.drafting_crew import build_drafting_crew

        crew = build_drafting_crew(jira_project_key="BANK", atlassian_tools=[])
        analyst = next(a for a in crew.agents if "Analyst" in a.role)
        assert analyst.tools == [] or analyst.tools is None


# ---------------------------------------------------------------------------
# crews/judge_crew.py
# ---------------------------------------------------------------------------

class TestJudgeCrew:
    def test_build_judge_crew_has_one_agent(self):
        """Judge crew is intentionally one agent — no collaboration, pure reasoning."""
        from reqsmith.crews.judge_crew import build_judge_crew

        crew = build_judge_crew(judge_model="claude-haiku-4-5-20251001")
        assert len(crew.agents) == 1
        assert "Judge" in crew.agents[0].role

    def test_judge_agent_has_no_tools(self):
        """Judge must have no retrieval tools — isolation from drafting crew."""
        from reqsmith.crews.judge_crew import build_judge_crew

        crew = build_judge_crew()
        judge = crew.agents[0]
        assert judge.tools == [] or judge.tools is None

    def test_judge_uses_different_model_from_drafting(self):
        """Judge and drafting crews must use different model identifiers."""
        from reqsmith.crews.drafting_crew import build_drafting_crew
        from reqsmith.crews.judge_crew import build_judge_crew

        drafting = build_drafting_crew(jira_project_key="BANK", atlassian_tools=[],
                                       drafting_model="claude-sonnet-4-6")
        judge = build_judge_crew(judge_model="claude-haiku-4-5-20251001")

        drafting_model = getattr(drafting.agents[0].llm, "model", None) or \
                         getattr(drafting.agents[0].llm, "model_name", "")
        judge_model = getattr(judge.agents[0].llm, "model", None) or \
                      getattr(judge.agents[0].llm, "model_name", "")

        assert drafting_model != judge_model, (
            f"Judge and drafting crews must use different models; "
            f"both got {drafting_model!r}"
        )

    def test_judge_crew_has_one_task(self):
        """Judge crew has exactly one task."""
        from reqsmith.crews.judge_crew import build_judge_crew

        crew = build_judge_crew()
        assert len(crew.tasks) == 1
