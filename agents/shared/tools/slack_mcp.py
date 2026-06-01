"""Slack MCP server connection helper.

Provides token management for connecting to Slack's MCP server.
Token loaded from SLACK_BOT_TOKEN environment variable for local development,
or from SSM Parameter Store in production.

Usage in agent.py:
    from shared.tools.slack_mcp import get_slack_token, SLACK_MCP_URL

    slack_client = MCPClient(
        lambda: streamablehttp_client(
            SLACK_MCP_URL,
            headers={"Authorization": f"Bearer {get_slack_token()}"},
        )
    )
"""

import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

SLACK_MCP_URL = os.environ.get("SLACK_MCP_URL", "https://api.slack.com/mcp")

_ssm = None
_cached_token: str | None = None


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def get_slack_token() -> str:
    """Get the Slack Bot User OAuth Token.

    Checks SLACK_BOT_TOKEN env var first (local dev), then falls back to
    SSM Parameter Store (production). Token is cached for the lifetime
    of the process.
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
        logger.error("Failed to fetch Slack bot token from SSM (%s): %s", param_name, exc)
        raise RuntimeError(f"Cannot load Slack bot token from {param_name}") from exc
