"""Slack outbound tools for agent runtimes.

Provides two @tool functions for posting messages to Slack via the
chat.postMessage API:

- slack_post_message  — post to a channel (top-level or in a thread)
- slack_post_thread   — reply in a specific thread (convenience wrapper)

Token is fetched from SSM once at module load. AgentCore Runtime containers
are long-lived; fetching at init is intentional and safe — there is no
per-call refetch overhead and no per-invocation secret scope leakage because
the container is already scoped to this one agent's execution environment.

The SSM parameter name is read from the SLACK_BOT_TOKEN_PARAM environment
variable (default: /sdlc-agents/slack-bot-token). The per-agent runtime role
must have ssm:GetParameter on this parameter (granted by the fleet's IAM
provisioning; see skills/sdlc-agents-provision-aws/SKILL.md).
"""

import logging
import os

import boto3
import requests
from strands import tool

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_PARAM_NAME = os.environ.get("SLACK_BOT_TOKEN_PARAM", "/sdlc-agents/slack-bot-token")

# --- Token init --------------------------------------------------------------
# Fetched once per container startup. AgentCore runtimes are long-lived; the
# module-level cache is intentional here (unlike the webhook Lambda, where
# per-invocation fetch limits cross-request secret exposure). The container is
# isolated to this agent's execution environment, so the exposure window is
# bounded by the container's lifetime rather than a shared Lambda environment.

_bot_token: str | None = None


def _get_bot_token() -> str:
    global _bot_token
    if _bot_token is None:
        ssm = boto3.client("ssm")
        resp = ssm.get_parameter(Name=_PARAM_NAME, WithDecryption=True)
        _bot_token = resp["Parameter"]["Value"]
    return _bot_token


def _post_message(channel: str, text: str, thread_ts: str = "") -> str:
    """Internal: call chat.postMessage and return a result string for the LLM."""
    token = _get_bot_token()
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    try:
        resp = requests.post(
            f"{SLACK_API}/chat.postMessage",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            ts = data.get("message", {}).get("ts", "")
            return f"Message posted to {channel} (ts={ts})"
        error = data.get("error", "unknown")
        logger.error("chat.postMessage error for channel %s: %s", channel, error)
        return f"Failed to post to Slack channel {channel}: {error}"
    except requests.RequestException as exc:
        logger.error("HTTP error posting to Slack channel %s: %s", channel, exc)
        return f"HTTP error posting to Slack channel {channel}: {exc}"


# --- Tools -------------------------------------------------------------------


@tool
def slack_post_message(channel: str, text: str, thread_ts: str = "") -> str:
    """Post a message to a Slack channel, optionally as a thread reply.

    Call this to deliver agent results back to the Slack channel that
    triggered the work. If the request arrived in a thread, pass the
    thread_ts so the reply stays in context; otherwise omit it and Slack
    posts at the top level.

    Args:
        channel:   Slack channel ID (e.g. "C0123ABCDEF"). Do not use channel
                   names — they can be renamed; IDs are stable.
        text:      The message body. Slack markdown is supported.
        thread_ts: Timestamp of the parent message to reply in a thread.
                   Leave empty to post at channel top level.

    Returns:
        Confirmation string with the message timestamp, or an error message.
    """
    if not channel:
        return "Error: channel is required"
    return _post_message(channel=channel, text=text, thread_ts=thread_ts)


@tool
def slack_post_thread(thread_ts: str, channel: str, text: str) -> str:
    """Reply in an existing Slack thread.

    Convenience wrapper around slack_post_message for the common case where
    the agent needs to reply to the exact thread that triggered the work.
    Use this when the dispatch context provides both a channel_id and a
    thread_ts.

    Args:
        thread_ts: Timestamp of the parent message (from dispatch context
                   thread_ts field).
        channel:   Slack channel ID containing the thread.
        text:      The reply body. Slack markdown is supported.

    Returns:
        Confirmation string with the message timestamp, or an error message.
    """
    if not thread_ts or not channel:
        return "Error: thread_ts and channel are both required"
    return _post_message(channel=channel, text=text, thread_ts=thread_ts)
