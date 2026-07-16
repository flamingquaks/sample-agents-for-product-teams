"""GitHub MCP server connection.

Connects to GitHub's official remote MCP server
(https://api.githubcopilot.com/mcp/) using an OAuth token.

Token loaded from SSM Parameter Store in production or
GITHUB_MCP_TOKEN environment variable for local development.
"""

import os

import boto3

_ssm = None
_cached_token = None


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def get_github_token() -> str:
    """Get the GitHub MCP access token from env or SSM."""
    global _cached_token
    if _cached_token:
        return _cached_token

    _cached_token = os.environ.get("GITHUB_MCP_TOKEN")
    if _cached_token:
        return _cached_token

    resp = _get_ssm().get_parameter(
        Name=os.environ.get("GITHUB_MCP_TOKEN_PARAM", "/sdlc-agents/github-mcp-token"),
        WithDecryption=True,
    )
    _cached_token = resp["Parameter"]["Value"]
    return _cached_token


# GitHub official remote MCP server (direct connection). Gateway routing, when
# enabled, is handled in agent.py via shared/tools/gateway.py — this constant is
# used only for the direct (non-gateway) bearer client.
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
