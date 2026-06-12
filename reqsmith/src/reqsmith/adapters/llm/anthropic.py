"""Anthropic implementation of LLMPort. The only place model calls happen; records
the version triple and enforces structure expected by the prompt registry."""

from functools import lru_cache
from pathlib import Path

from reqsmith.adapters.llm.port import LLMResult
from reqsmith.settings import get_settings

PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"


@lru_cache
def load_prompt(prompt_id: str) -> str:
    path = PROMPTS_DIR / f"{prompt_id}.md"
    return path.read_text()


def render_prompt(prompt_id: str, variables: dict) -> str:
    template = load_prompt(prompt_id)
    for key, value in variables.items():
        template = template.replace("{" + key + "}", str(value))
    return template


class AnthropicLLM:
    def __init__(self):
        import anthropic  # heavy import kept local

        settings = get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._models = {"drafting": settings.model_drafting, "judge": settings.model_judge}

    async def complete(
        self, *, prompt_id: str, variables: dict, model_role: str = "drafting",
        max_tokens: int = 4096,
    ) -> LLMResult:
        model = self._models[model_role]
        message = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": render_prompt(prompt_id, variables)}],
        )
        return LLMResult(
            text="".join(block.text for block in message.content if block.type == "text"),
            model_id=model,
            prompt_version=prompt_id,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
