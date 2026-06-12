"""TeamsPort — proactive outreach. Implemented in M7 (Bot Framework); fake here for
the ladder/budget logic to be testable from M1."""

from typing import Protocol


class TeamsPort(Protocol):
    async def send_card(self, *, user_aad_id: str, card: dict) -> str:
        """Sends an adaptive card 1:1; returns the external message id."""
        ...
