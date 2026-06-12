"""Layer-2 probabilistic scoring: cheap deterministic heuristics + the LLM judge.
Scores route work (threshold → human queue); they never auto-publish anything."""

import re
from dataclasses import dataclass

AMBIGUOUS_TERMS = re.compile(
    r"(?i)\b(fast|quick|easy|simple|some|several|various|etc\.?|as appropriate|"
    r"user[- ]friendly|robust|flexible|seamless|appropriate|reasonable)\b"
)

JUDGE_PASS_THRESHOLD = 7.0


@dataclass
class HeuristicScores:
    ambiguity_hits: list[str]
    stories_count: int

    @property
    def ambiguity_score(self) -> float:
        """10 = clean; subtract per vague term hit."""
        return max(0.0, 10.0 - len(self.ambiguity_hits))


def score_draft(draft: dict) -> HeuristicScores:
    hits = []
    for story in draft.get("stories", []):
        text = " ".join(
            [story.get("title", ""), story.get("story", "")]
            + list(story.get("acceptance_criteria", []))
        )
        hits.extend(AMBIGUOUS_TERMS.findall(text))
    return HeuristicScores(ambiguity_hits=hits, stories_count=len(draft.get("stories", [])))
