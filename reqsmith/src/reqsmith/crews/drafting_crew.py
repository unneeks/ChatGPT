"""Drafting crew — three-agent CrewAI crew for requirements synthesis.

Agents (sequential):
  1. retrieval_agent   — searches Jira/Confluence for context beyond the intake
  2. analyst_agent     — drafts epic + stories with GWT ACs, NFRs, inline citations
  3. elicitation_agent — identifies gaps; may mark draft insufficient (blocks → questions)

Output contract: same JSON schema as the single-agent fallback in stages/drafting.py so
the stage layer is agnostic to which mode ran.

Instantiate with build_drafting_crew(); the stage calls run_crew() from crews/base.py.
"""

from __future__ import annotations

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def build_drafting_crew(
    *,
    jira_project_key: str,
    atlassian_tools: list | None = None,
    drafting_model: str = "claude-sonnet-4-6",
) -> "Crew":  # noqa: F821  (crewai optional import)
    """Build and return the drafting Crew.

    Parameters
    ----------
    jira_project_key:
        Used to scope JQL searches to the pilot project.
    atlassian_tools:
        Pre-loaded CrewAI tools (from crews/tools.py). Pass None to auto-load;
        pass [] to force REST-only mode (e.g. in tests).
    drafting_model:
        LiteLLM-compatible model identifier for the analyst and elicitation agents.
        Pass the value of settings.model_drafting.
    """
    from crewai import Agent, Crew, Process, Task  # type: ignore[import]
    from crewai.llm import LLM  # type: ignore[import]

    if atlassian_tools is None:
        from reqsmith.crews.tools import get_atlassian_tools, get_jira_rest_tools
        tools = get_atlassian_tools() or get_jira_rest_tools()
    else:
        tools = atlassian_tools

    # LiteLLM model name: CrewAI prefixes with provider if not already set
    llm_model = drafting_model if "/" in drafting_model else f"anthropic/{drafting_model}"
    llm = LLM(model=llm_model, temperature=0.1)

    retrieval_agent = Agent(
        role="Requirements Retrieval Specialist",
        goal=(
            f"Search the {jira_project_key} Jira project and linked Confluence spaces for "
            "all relevant context: prior decisions, regulatory citations, related epics, "
            "and domain definitions that bear on the intake."
        ),
        backstory=(
            "You are a meticulous researcher who finds every relevant document before the "
            "analyst starts writing. You never fabricate citations — if you can't find it, "
            "you say so."
        ),
        tools=tools,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    analyst_agent = Agent(
        role="Senior Business Analyst",
        goal=(
            "Draft a complete, grounded epic breakdown (epic summary, user stories with GWT "
            "acceptance criteria, NFRs, assumptions, open questions) strictly citing the "
            "retrieved sources."
        ),
        backstory=(
            "You are a senior BA at a bank. Every claim you write must be traceable to a "
            "source document. You write in Given/When/Then. You never use placeholders."
        ),
        tools=[],
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    elicitation_agent = Agent(
        role="Requirements Elicitation Specialist",
        goal=(
            "Review the draft and the retrieved sources for gaps that would prevent a complete, "
            "testable specification. Produce a prioritised question list and flag whether the "
            "draft is sufficient or must be blocked pending answers."
        ),
        backstory=(
            "You are an expert at spotting missing actors, uncovered error paths, and "
            "unspecified NFR thresholds. You ask precise, answerable questions."
        ),
        tools=[],
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    retrieval_task = Task(
        description=(
            "Search Jira project {jira_project_key} and Confluence for all context relevant "
            "to this intake:\n\n{intake}\n\n"
            "Return a structured list of source excerpts with their Jira/Confluence references. "
            "Include: related epics, regulatory references, domain definitions, prior decisions."
        ),
        expected_output=(
            "A JSON object: {\"sources\": [{\"ref\": \"BANK-X or Confluence page\", "
            "\"excerpt\": \"...\", \"relevance\": \"...\"}]}"
        ),
        agent=retrieval_agent,
    )

    analyst_task = Task(
        description=(
            "Using ONLY the sources provided by the retrieval specialist, draft the "
            "requirements for this intake:\n\n{intake}\n\n"
            "Follow this schema exactly:\n" + _load("analyst_v1")
        ),
        expected_output=(
            "A JSON object matching the analyst_v1 schema with epic_summary, stories "
            "(each with title, story, acceptance_criteria, citations, nfrs), "
            "assumptions, and open_questions."
        ),
        agent=analyst_agent,
        context=[retrieval_task],
    )

    elicitation_task = Task(
        description=(
            "Review the draft produced by the analyst against the source documents. "
            "Identify gaps using this schema:\n" + _load("elicitation_v1") + "\n\n"
            "Intake: {intake}"
        ),
        expected_output=(
            "A JSON object with 'questions' list and 'sufficient_to_draft' boolean. "
            "Merge open_questions from the analyst draft into your output."
        ),
        agent=elicitation_agent,
        context=[retrieval_task, analyst_task],
    )

    return Crew(
        agents=[retrieval_agent, analyst_agent, elicitation_agent],
        tasks=[retrieval_task, analyst_task, elicitation_task],
        process=Process.sequential,
        verbose=False,
    )
