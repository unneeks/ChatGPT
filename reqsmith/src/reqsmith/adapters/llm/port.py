"""LLMPort — the only path to a model. Captures the version triple per call and
enforces the per-run token budget (circuit breaker)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    model_id: str
    prompt_version: str
    input_tokens: int
    output_tokens: int


class TokenBudgetExceeded(Exception):
    pass


class LLMPort(Protocol):
    async def complete(
        self, *, prompt_id: str, variables: dict, model_role: str = "drafting",
        max_tokens: int = 4096,
    ) -> LLMResult:
        """prompt_id names a versioned file in prompts/ (e.g. 'analyst_v1');
        model_role routes to MODEL_DRAFTING or MODEL_JUDGE."""
        ...
