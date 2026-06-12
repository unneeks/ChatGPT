import itertools


class FakeTeams:
    def __init__(self):
        self.sent: list[dict] = []
        self._ids = itertools.count(1)

    async def send_card(self, *, user_aad_id: str, card: dict) -> str:
        message_id = f"msg-{next(self._ids)}"
        self.sent.append({"user": user_aad_id, "card": card, "id": message_id})
        return message_id
