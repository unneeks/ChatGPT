"""Central configuration. Every environment variable the system reads is defined here."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    database_url: str = "sqlite+aiosqlite:///./reqsmith.db"
    debug: bool = False
    server_port: int = 8000

    # Version triple — recorded on every artifact and audit event
    prompt_pack_version: str = "v1"
    policy_pack_version: str = "v1"

    # LLM
    anthropic_api_key: str = ""
    model_drafting: str = "claude-sonnet-4-6"
    model_judge: str = "claude-haiku-4-5-20251001"
    max_tokens_per_run: int = 200_000  # circuit breaker budget

    # Jira (REST: webhooks re-fetch, fields, transitions, publishes)
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_webhook_secret: str = ""
    jira_project_key: str = ""
    jira_field_risk_tier: str = ""
    jira_field_confidence: str = ""
    jira_field_provenance: str = ""
    jira_field_run_state: str = ""

    # Atlassian Remote MCP (agent-facing reads: Jira search/read, Confluence)
    atlassian_mcp_url: str = "https://mcp.atlassian.com/v1/sse"
    atlassian_mcp_client_id: str = ""
    atlassian_mcp_client_secret: str = ""
    atlassian_mcp_refresh_token: str = ""

    # Teams bot
    bot_app_id: str = ""
    bot_app_password: str = ""
    teams_tenant_id: str = ""

    # Microsoft Graph (calendar/scheduling daemon app)
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_tenant_id: str = ""
    human_owner_aad_id: str = ""

    # Outreach controls
    outreach_paused: bool = False  # global kill switch (env-level default; DB flag overrides)

    # Policy packs directory (YAML rule files, versioned in git)
    policy_dir: Path = CONFIG_DIR / "policies"


@lru_cache
def get_settings() -> Settings:
    return Settings()
