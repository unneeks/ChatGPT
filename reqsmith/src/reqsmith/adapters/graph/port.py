"""GraphPort — calendar scheduling (M8). Fake available from M1 for scheduler logic."""

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
