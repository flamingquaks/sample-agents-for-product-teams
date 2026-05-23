"""Discord outbound posting tools for SDLC agents.

Two @tool functions:

  discord_post_followup  -- slash-command follow-up via the interaction token.
                            No bot auth header needed; the token is the auth.
                            Use this as the primary reply to a slash command.

  discord_post_message   -- channel post via the bot token.
                            Use this for additional messages after the first
                            follow-up, or for channel notifications not tied
                            to a slash command.

The bot token is fetched from SSM at module init and cached in _bot_token.
This is an agent-side module (not Lambda), so a module-level cache is
appropriate -- the container lifetime is bounded by AgentCore Runtime and the
token is only the Discord bot token, not a user credential.

Both tools handle HTTP 429 with a single bounded Retry-After retry (Discord
enforces per-route rate limits; the retry cap is 10 seconds to avoid
blocking an agent invocation).
"""

import logging
import os
import time

import boto3
import requests

from strands import tool

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_BOT_TOKEN_PARAM = os.environ.get(
    "DISCORD_BOT_TOKEN_PARAM", "/sdlc-agents/discord-bot-token"
)

_ssm = boto3.client("ssm")

# Module-level token cache. Fetched once on first use per container lifetime.
_bot_token: str | None = None


def _get_bot_token() -> str | None:
    """Fetch and cache the Discord bot token from SSM."""
    global _bot_token
    if _bot_token:
        return _bot_token
    try:
        resp = _ssm.get_parameter(Name=DISCORD_BOT_TOKEN_PARAM, WithDecryption=True)
        _bot_token = resp["Parameter"]["Value"]
        return _bot_token
    except Exception as exc:
        logger.error("Failed to fetch Discord bot token from SSM: %s", exc)
        return None


def _post_with_429_retry(url: str, payload: dict, headers: dict) -> requests.Response:
    """POST to a Discord endpoint with a single bounded 429 retry."""
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    if response.status_code == 429:
        retry_after = min(float(response.json().get("retry_after", 1)), 10.0)
        logger.warning("Discord rate-limited; retrying after %.1fs", retry_after)
        time.sleep(retry_after)  # nosemgrep: arbitrary-sleep -- bounded Discord rate-limit retry
        response = requests.post(url, json=payload, headers=headers, timeout=10)
    return response


@tool
def discord_post_followup(
    application_id: str,
    interaction_token: str,
    content: str,
) -> str:
    """Post a follow-up message to a Discord slash command interaction.

    This is the primary way to reply to a slash command. Discord shows
    the follow-up message to the user in the channel where they typed the
    command, replacing the "Bot is thinking..." state.

    The interaction token is valid for 15 minutes after the slash command
    was invoked. Use discord_post_message for additional messages after the
    first reply or when a token has expired.

    No Authorization header is needed -- the interaction token authenticates
    the request via POST /webhooks/{application_id}/{interaction_token}.

    Args:
        application_id: The Discord application (bot) snowflake ID.
        interaction_token: The interaction token from the slash command payload.
        content: The message text (up to 2000 characters).

    Returns:
        "ok" on success, an error description on failure.
    """
    if not application_id or not interaction_token or not content:
        return "error: application_id, interaction_token, and content are all required"

    url = f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}"
    try:
        response = _post_with_429_retry(
            url=url,
            payload={"content": content},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return "ok"
    except requests.RequestException as exc:
        logger.error(
            "Failed to post Discord follow-up to interaction %s: %s",
            interaction_token[:16] + "...",
            exc,
        )
        return f"error: {exc}"


@tool
def discord_post_message(channel_id: str, content: str) -> str:
    """Post a message to a Discord channel using the bot token.

    Use this for additional messages after the initial slash-command follow-up
    reply, or for channel notifications not tied to a specific slash command.

    The bot must have the Send Messages permission in the target channel.

    Args:
        channel_id: The Discord channel snowflake ID.
        content: The message text (up to 2000 characters).

    Returns:
        "ok" on success, an error description on failure.
    """
    if not channel_id or not content:
        return "error: channel_id and content are required"

    token = _get_bot_token()
    if not token:
        return "error: could not load Discord bot token from SSM"

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    try:
        response = _post_with_429_retry(
            url=url,
            payload={"content": content},
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return "ok"
    except requests.RequestException as exc:
        logger.error("Failed to post Discord message to channel %s: %s", channel_id, exc)
        return f"error: {exc}"
