import itertools
from datetime import UTC, datetime, timedelta

from reqsmith.adapters.graph.port import MeetingSlot


class FakeGraph:
    def __init__(self):
        self.events: list[dict] = []
        self._ids = itertools.count(1)

    async def find_meeting_times(
        self, *, organizer: str, attendees: list[str], duration_minutes: int = 30,
    ) -> list[MeetingSlot]:
        start = datetime.now(UTC) + timedelta(days=1)
        return [MeetingSlot(start=start, end=start + timedelta(minutes=duration_minutes))]

    async def create_event(
        self, *, organizer: str, attendees: list[str], slot: MeetingSlot,
        subject: str, agenda: str,
    ) -> str:
        event_id = f"evt-{next(self._ids)}"
        self.events.append({
            "id": event_id, "organizer": organizer, "attendees": attendees,
            "subject": subject, "agenda": agenda, "slot": slot,
        })
        return event_id
