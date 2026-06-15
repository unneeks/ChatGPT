"""Microsoft Graph calendar client — app-only (daemon) auth via MSAL.

Only imported when BOT_APP_ID/GRAPH_CLIENT_ID are configured. Tests inject
FakeGraph via deps.set_graph(); this module is never imported in CI.

Scopes required (admin consent):
  - Calendars.ReadWrite      (findMeetingTimes + create event)
  - User.Read.All            (resolve user email → AAD UPN)
  - OnlineMeetings.ReadWrite (optional: attach Teams meeting link)
"""

from datetime import UTC, datetime

from reqsmith.adapters.graph.port import MeetingSlot


class GraphClient:
    """Real Microsoft Graph client. Defers msal import to runtime."""

    def __init__(self):
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    def _get_token(self) -> str:
        try:
            import msal
        except ImportError as exc:
            raise RuntimeError(
                "msal not installed. Run: pip install reqsmith[outreach]"
            ) from exc

        from reqsmith.settings import get_settings
        s = get_settings()
        if not s.graph_client_id:
            raise RuntimeError("GRAPH_CLIENT_ID not configured")

        now = datetime.now(UTC)
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token

        app = msal.ConfidentialClientApplication(
            client_id=s.graph_client_id,
            client_credential=s.graph_client_secret,
            authority=f"https://login.microsoftonline.com/{s.graph_tenant_id}",
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(f"MSAL token error: {result.get('error_description')}")

        self._token = result["access_token"]
        expires_in = result.get("expires_in", 3600)
        from datetime import timedelta
        self._token_expiry = now + timedelta(seconds=expires_in - 60)
        return self._token

    async def find_meeting_times(
        self, *, organizer: str, attendees: list[str], duration_minutes: int = 30,
    ) -> list[MeetingSlot]:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx not installed") from exc

        token = self._get_token()
        from datetime import timedelta
        now = datetime.now(UTC)
        body = {
            "attendees": [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ],
            "timeConstraint": {
                "activityDomain": "work",
                "timeslots": [{
                    "start": {"dateTime": now.isoformat(), "timeZone": "UTC"},
                    "end": {"dateTime": (now + timedelta(days=7)).isoformat(), "timeZone": "UTC"},
                }],
            },
            "meetingDuration": f"PT{duration_minutes}M",
            "maxCandidates": 5,
            "isOrganizerOptional": False,
            "returnSuggestionReasons": True,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://graph.microsoft.com/v1.0/me/findMeetingTimes",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

        slots = []
        for suggestion in data.get("meetingTimeSuggestions", []):
            ts = suggestion.get("meetingTimeSlot", {})
            start_str = ts.get("start", {}).get("dateTime")
            end_str = ts.get("end", {}).get("dateTime")
            if start_str and end_str:
                slots.append(MeetingSlot(
                    start=datetime.fromisoformat(start_str).replace(tzinfo=UTC),
                    end=datetime.fromisoformat(end_str).replace(tzinfo=UTC),
                ))
        return slots

    async def create_event(
        self, *, organizer: str, attendees: list[str], slot: MeetingSlot,
        subject: str, agenda: str,
    ) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx not installed") from exc

        token = self._get_token()
        body = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": agenda.replace("\n", "<br>")},
            "start": {"dateTime": slot.start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": slot.end.isoformat(), "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ],
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{organizer}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["id"]

    async def get_meeting_transcript(self, event_id: str, organizer: str) -> str | None:
        """Fetch VTT transcript for a calendar event via Graph.

        Requires OnlineMeetingTranscript.Read.All scope + tenant application access policy.
        Returns None on 403 (policy blocked), 404 (no transcript), or any other soft failure.
        """
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx not installed") from exc

        token = self._get_token()
        base = "https://graph.microsoft.com/v1.0"
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            # Step 1: get calendar event to find the online meeting join URL
            resp = await client.get(
                f"{base}/users/{organizer}/events/{event_id}",
                headers=headers, timeout=30,
            )
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            event_data = resp.json()

            join_url = (event_data.get("onlineMeeting") or {}).get("joinUrl")
            if not join_url:
                return None

            # Step 2: resolve join URL → onlineMeeting id
            from urllib.parse import quote
            resp = await client.get(
                f"{base}/users/{organizer}/onlineMeetings"
                f"?$filter=JoinWebUrl eq '{quote(join_url, safe='')}'",
                headers=headers, timeout=30,
            )
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            meetings = resp.json().get("value", [])
            if not meetings:
                return None
            meeting_id = meetings[0]["id"]

            # Step 3: list transcripts — pick the most recent
            resp = await client.get(
                f"{base}/users/{organizer}/onlineMeetings/{meeting_id}/transcripts",
                headers=headers, timeout=30,
            )
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            transcripts = resp.json().get("value", [])
            if not transcripts:
                return None
            transcript_id = transcripts[-1]["id"]  # most recent

            # Step 4: download VTT content
            resp = await client.get(
                f"{base}/users/{organizer}/onlineMeetings/{meeting_id}"
                f"/transcripts/{transcript_id}/content?$format=text/vtt",
                headers=headers, timeout=60,
            )
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            return resp.text
