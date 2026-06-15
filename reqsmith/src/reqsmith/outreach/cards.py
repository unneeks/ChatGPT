"""Adaptive Card builder + answer parser for Teams outreach (design §3a).

Cards follow Adaptive Cards v1.4 (Teams supports up to v1.5 but v1.4 is safer
across client versions). The card contains:
  - The question text
  - A free-text Input.Text field
  - Submit button (sends {"question_id": ..., "answer": ...} in Activity.value)
  - "Talk to a human" button (sends {"question_id": ..., "handoff": true})

The 3-turn cap is enforced by checking the turn count against the policy pack
before sending, and the card includes a warning when turns are running low.
"""

from reqsmith.verification.gates import load_policy_pack


def _max_turns() -> int:
    policy = load_policy_pack("outreach")
    return policy.get("conversation", {}).get("max_turns_per_question", 3)


def build_question_card(
    *,
    question_id: str,
    question_text: str,
    turn: int = 1,
    issue_key: str | None = None,
) -> dict:
    """Build an Adaptive Card payload for a clarification question.

    Args:
        question_id: The [REQ-Q:uuid] cross-channel key embedded in the card value.
        question_text: The human-readable question.
        turn: Current turn number (1-indexed). Controls the turn warning.
        issue_key: Optional Jira key shown in the card footer.
    """
    max_turns = _max_turns()
    remaining = max_turns - turn + 1

    body: list[dict] = [
        {
            "type": "Container",
            "style": "emphasis",
            "items": [
                {
                    "type": "TextBlock",
                    "text": "Requirements Assistant",
                    "weight": "bolder",
                    "size": "medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"Clarification needed{f' for {issue_key}' if issue_key else ''}",
                    "isSubtle": True,
                    "size": "small",
                },
            ],
        },
        {
            "type": "TextBlock",
            "text": question_text,
            "wrap": True,
            "spacing": "medium",
        },
        {
            "type": "Input.Text",
            "id": "answer",
            "placeholder": "Type your answer here…",
            "isMultiline": True,
        },
    ]

    if remaining <= 1:
        body.append({
            "type": "TextBlock",
            "text": "⚠ This is your last opportunity to respond in this thread.",
            "color": "warning",
            "size": "small",
            "wrap": True,
        })
    elif remaining < max_turns:
        body.append({
            "type": "TextBlock",
            "text": f"{remaining} response(s) remaining in this thread.",
            "isSubtle": True,
            "size": "small",
        })

    actions = [
        {
            "type": "Action.Submit",
            "title": "Submit answer",
            "style": "positive",
            "data": {"question_id": question_id, "handoff": False},
        },
        {
            "type": "Action.Submit",
            "title": "Talk to a human",
            "style": "default",
            "data": {"question_id": question_id, "handoff": True},
        },
    ]

    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
            "actions": actions,
        },
    }


def parse_card_answer(activity_value: dict) -> tuple[str | None, str | None, bool]:
    """Parse a Bot Framework Activity value from a card submit action.

    Returns:
        (question_id, answer_text, is_handoff)
        question_id is None if the value isn't from one of our cards.
        answer_text is None for handoff submissions.
    """
    if not isinstance(activity_value, dict):
        return None, None, False
    question_id: str | None = activity_value.get("question_id")
    if not question_id:
        return None, None, False
    is_handoff: bool = bool(activity_value.get("handoff", False))
    answer_text: str | None = activity_value.get("answer", "").strip() or None
    return question_id, answer_text, is_handoff
