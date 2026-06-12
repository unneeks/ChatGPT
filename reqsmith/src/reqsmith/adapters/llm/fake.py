"""Deterministic fake LLM: canned responses keyed by prompt_id, for unit tests and
the eval harness."""

from reqsmith.adapters.llm.port import LLMResult


class FakeLLM:
    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.calls: list[dict] = []

    async def complete(
        self, *, prompt_id: str, variables: dict, model_role: str = "drafting",
        max_tokens: int = 4096,
    ) -> LLMResult:
        self.calls.append({"prompt_id": prompt_id, "variables": variables, "role": model_role})
        text = self.responses.get(prompt_id, f"[fake response for {prompt_id}]")
        return LLMResult(
            text=text,
            model_id=f"fake-{model_role}",
            prompt_version=prompt_id,
            input_tokens=10,
            output_tokens=20,
        )
