"""GraphPort — calendar scheduling (M8) + transcript fetch (M9)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class MeetingSlot:
    start: datetime
    end: datetime


class GraphPort(Protocol):
    async def find_meeting_times(
        self, *, organizer: str, attendees: list[str], duration_minutes: int = 30,
    ) -> list[MeetingSlot]: ...

    async def create_event(
        self, *, organizer: str, attendees: list[str], slot: MeetingSlot,
        subject: str, agenda: str,
    ) -> str:
        """Returns the created event id."""
        ...

    async def get_meeting_transcript(self, event_id: str, organizer: str) -> str | None:
        """Fetch VTT transcript for a calendar event.

        Returns WebVTT text, or None if unavailable (tenant policy, no transcript yet, 404).
        Callers must treat None as a soft failure and fall back to manual upload.
        """
        ...
