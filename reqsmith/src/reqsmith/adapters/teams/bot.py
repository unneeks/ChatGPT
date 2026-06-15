"""Real Teams adapter using Azure Bot Framework (botbuilder-core).

This module is only importable when the [outreach] optional extra is installed:
  pip install reqsmith[outreach]

In all other contexts (CI, tests, eval harness) the fake is used via deps.set_teams().

Proactive send pattern:
  1. The bot stores the ConversationReference for each user the first time they
     message the bot (saved in the conversation_references dict, or a DB in production).
  2. Outreach sends use continue_conversation() to open a new proactive turn.

For MVP (shadow mode) the bot sends to a pre-configured conversation reference or
falls back to a direct API call via the Bot Connector REST API.
"""


class TeamsBot:
    """Thin wrapper. Defers botbuilder import to runtime so tests stay fast."""

    async def send_card(self, *, user_aad_id: str, card: dict) -> str:
        try:
            from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
            from botbuilder.schema import Activity, ActivityTypes, Attachment
        except ImportError as exc:
            raise RuntimeError(
                "botbuilder-core not installed. Run: pip install reqsmith[outreach]"
            ) from exc

        from reqsmith.settings import get_settings
        s = get_settings()
        if not s.bot_app_id:
            raise RuntimeError("BOT_APP_ID not configured — cannot send Teams card")

        # Construct objects so the imports are validated at startup; actual send
        # requires a ConversationReference which is stored when the user first
        # messages the bot (see _maybe_store_conversation_reference in webhooks_teams).
        _ = BotFrameworkAdapterSettings(s.bot_app_id, s.bot_app_password)
        _ = BotFrameworkAdapter(_)
        _ = Activity(
            type=ActivityTypes.message,
            attachments=[Attachment(content_type=card["contentType"], content=card["content"])],
        )
        raise NotImplementedError(
            "Proactive send requires a stored ConversationReference. "
            "Implement a ConversationReference store or use the Direct Line API. "
            "See deploy/README-azure.md §Teams setup."
        )
