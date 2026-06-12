"""Adapter wiring. Real clients by default (when configured); tests and the eval
harness inject fakes via the setters."""

from reqsmith.adapters.jira.port import JiraPort
from reqsmith.adapters.llm.port import LLMPort

_jira: JiraPort | None = None
_llm: LLMPort | None = None


def get_jira() -> JiraPort:
    global _jira
    if _jira is None:
        from reqsmith.adapters.jira.client import JiraClient

        _jira = JiraClient()
    return _jira


def set_jira(adapter: JiraPort | None) -> None:
    global _jira
    _jira = adapter


def get_llm() -> LLMPort:
    global _llm
    if _llm is None:
        from reqsmith.adapters.llm.anthropic import AnthropicLLM

        _llm = AnthropicLLM()
    return _llm


def set_llm(adapter: LLMPort | None) -> None:
    global _llm
    _llm = adapter
