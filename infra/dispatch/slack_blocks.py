"""Slack Block Kit formatting utilities.

Converts agent output into structured Block Kit JSON for rich Slack messages.
Used by reply.py for guardrail block notices and by agents for result posting.
"""


def format_agent_ack(agent_id: str, assignment_id: str) -> list[dict]:
    """Compact acknowledgment block when an agent picks up work."""
    return [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":eyes: *@{agent_id}* is working on this. Assignment: `{assignment_id[:8]}...`",
                }
            ],
        }
    ]


def format_agent_result(agent_id: str, summary: str, assignment_id: str = "", details: str = "") -> list[dict]:
    """Structured response block for agent results."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{agent_id.title()} Agent", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary},
        },
    ]

    if details:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": details}})

    context_elements = []
    if assignment_id:
        context_elements.append({"type": "mrkdwn", "text": f"Assignment: `{assignment_id[:8]}...`"})

    if context_elements:
        blocks.append({"type": "context", "elements": context_elements})

    return blocks


def format_error(message: str, assignment_id: str = "") -> list[dict]:
    """Error block with warning indicator."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: {message}",
            },
        },
    ]

    if assignment_id:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Assignment: `{assignment_id[:8]}...`"}],
        })

    return blocks


def format_guardrail_block(message: str, assignment_id: str = "") -> list[dict]:
    """Block notice for guardrail-intercepted requests.

    assignment_id is optional: when the caller's `message` already embeds the
    assignment id (as the Router's block templates do), pass "" to avoid
    rendering an empty/duplicate `Assignment:` line.
    """
    context_elements = []
    if assignment_id:
        context_elements.append({"type": "mrkdwn", "text": f"Assignment: `{assignment_id}`"})
    context_elements.append(
        {"type": "mrkdwn", "text": "If you believe this is a false positive, contact an operator."}
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":shield: *Request Blocked*\n{message}",
            },
        },
        {
            "type": "context",
            "elements": context_elements,
        },
    ]


def format_fleet_status(agent_counts: dict[str, int], recent_completions: int = 0) -> list[dict]:
    """Fleet dashboard blocks for App Home tab."""
    agent_lines = []
    for agent_id, active_count in sorted(agent_counts.items()):
        indicator = ":green_circle:" if active_count == 0 else ":large_blue_circle:"
        agent_lines.append(f"{indicator} *{agent_id}* — {active_count} active")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Fleet Status"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(agent_lines)},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":checkered_flag: {recent_completions} assignments completed in the last 24h"},
            ],
        },
    ]

    return blocks
