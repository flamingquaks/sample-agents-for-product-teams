"""Slack posting tools — direct Slack Web API, no MCP server required.

Agents call these custom tools to post results, threaded replies, and
reactions back to Slack. Unlike the Asana/GitHub paths (which go through
remote MCP servers), Slack posting talks to the Slack Web API directly using
the Bot User OAuth Token. This removes the dependency on a separately hosted
Slack MCP server.

Token resolution (same precedence as the rest of the fleet):
  1. SLACK_BOT_TOKEN environment variable (local dev)
  2. SSM Parameter Store at SLACK_BOT_TOKEN_PARAM (production, SecureString)

The token is cached for the lifetime of the process. get_slack_token()
returns None when Slack is not configured — the tools then return a clear
message the agent can relay, rather than raising. This keeps an unconfigured
Slack integration from crashing agents triggered by GitHub or Asana.

Usage in agent.py:
    from shared.tools.slack_post import slack_post_message, slack_add_reaction
    tools = [..., slack_post_message, slack_add_reaction]
"""

import logging
import os

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

_ssm = None
_cached_token: str | None = None


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def get_slack_token() -> str | None:
    """Return the Slack Bot User OAuth Token, or None if unavailable.

    Checks SLACK_BOT_TOKEN first (local dev), then SSM (production). Cached
    for the process lifetime. Returns None — never raises — when the token is
    missing so a misconfigured Slack integration cannot crash an agent that
    was triggered from another platform.
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        _cached_token = token
        return token

    param_name = os.environ.get("SLACK_BOT_TOKEN_PARAM", "/sdlc-agents/slack-bot-token")
    try:
        resp = _get_ssm().get_parameter(Name=param_name, WithDecryption=True)
        _cached_token = resp["Parameter"]["Value"]
        return _cached_token
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Slack bot token unavailable from SSM (%s): %s", param_name, exc)
        return None


def _slack_call(method: str, payload: dict) -> tuple[bool, str]:
    """POST to a Slack Web API method. Returns (ok, error_or_detail)."""
    token = get_slack_token()
    if not token:
        return False, "Slack is not configured (no bot token available)."

    try:
        response = requests.post(
            f"{SLACK_API}/{method}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=10,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Slack %s request failed: %s", method, exc)
        return False, str(exc)

    if not data.get("ok"):
        return False, data.get("error", "unknown_error")
    return True, ""


@tool
def slack_post_message(channel_id: str, message: str, thread_ts: str = "") -> str:
    """Post a message to a Slack channel, optionally as a threaded reply.

    Call this to deliver results back to a Slack channel or thread. The
    Dispatch Router supplies the channel ID (and thread timestamp, when the
    request originated in a thread) in the agent's dispatch context.

    Args:
        channel_id: Slack channel ID to post to (e.g. 'C0123ABCDEF').
        message: The message text. Slack mrkdwn is supported.
        thread_ts: Optional parent message timestamp. When set, the message is
            posted as a reply in that thread.

    Returns:
        A short status string describing success or the failure reason.
    """
    if not channel_id:
        return "Cannot post to Slack: no channel_id provided."

    payload: dict = {"channel": channel_id, "text": message}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    ok, detail = _slack_call("chat.postMessage", payload)
    if ok:
        where = f"channel {channel_id}" + (f" thread {thread_ts}" if thread_ts else "")
        return f"Posted message to Slack {where}."
    return f"Failed to post to Slack channel {channel_id}: {detail}"


@tool
def slack_add_reaction(channel_id: str, timestamp: str, name: str) -> str:
    """Add an emoji reaction to a Slack message.

    Args:
        channel_id: Slack channel ID containing the message (e.g. 'C0123ABCDEF').
        timestamp: The target message timestamp (the 'ts' field).
        name: Emoji name without colons (e.g. 'eyes', 'white_check_mark').

    Returns:
        A short status string describing success or the failure reason.
    """
    if not channel_id or not timestamp:
        return "Cannot add Slack reaction: channel_id and timestamp are required."

    ok, detail = _slack_call(
        "reactions.add",
        {"channel": channel_id, "timestamp": timestamp, "name": name},
    )
    if ok or detail == "already_reacted":
        return f"Added :{name}: reaction in {channel_id}."
    return f"Failed to add Slack reaction in {channel_id}: {detail}"
