"""Judge crew — independent quality review, separate model tier + prompt lineage.

Design constraint (§4 Layer-2): the judge crew MUST NOT share agents, tools, or
LLM config with the drafting crew. It uses MODEL_JUDGE (a different Claude tier)
and judge_rubric_v1 (a separate prompt lineage). This independence is recorded in
every audit event so the regulator can verify two-model provenance.

Instantiate with build_judge_crew(); the verification stage calls run_crew().
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def build_judge_crew(
    *,
    judge_model: str = "claude-haiku-4-5-20251001",
) -> "Crew":  # noqa: F821
    """Build and return the judge Crew.

    Parameters
    ----------
    judge_model:
        LiteLLM model for the judge — deliberately a different tier from drafting.
        Pass the value of settings.model_judge.
    """
    from crewai import Agent, Crew, Process, Task  # type: ignore[import]
    from crewai.llm import LLM  # type: ignore[import]

    llm_model = judge_model if "/" in judge_model else f"anthropic/{judge_model}"
    # Higher temperature for adversarial independence — the judge should not converge
    # with the analyst's framing.
    llm = LLM(model=llm_model, temperature=0.3)

    judge_agent = Agent(
        role="Independent Requirements Quality Judge",
        goal=(
            "Score the draft requirements against the rubric WITHOUT knowledge of who wrote "
            "them. Identify blocking issues. Be strict — a bank's delivery teams build from this."
        ),
        backstory=(
            "You are an independent auditor. You did not write this draft. You apply the "
            "rubric objectively and flag every ambiguity, gap, and untestable criterion."
        ),
        tools=[],  # judge has NO tools — pure reasoning, no retrieval
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    judge_task = Task(
        description=(
            "Score this requirements draft against the rubric. The draft must satisfy the "
            "intake. Apply the full rubric.\n\n"
            + _load("judge_rubric_v1")
            + "\n\n## Draft\n\n{draft}\n\n## Intake\n\n{intake}"
        ),
        expected_output=(
            "A JSON object with scores (unambiguous, complete, testable, consistent, atomic "
            "each 0-10), overall (float), blocking_issues (list), and reasoning (string)."
        ),
        agent=judge_agent,
    )

    return Crew(
        agents=[judge_agent],
        tasks=[judge_task],
        process=Process.sequential,
        verbose=False,
    )
