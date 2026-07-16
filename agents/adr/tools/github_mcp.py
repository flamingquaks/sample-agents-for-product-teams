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


# The GitHub MCP endpoint. When the AgentCore Gateway is deployed, set
# GATEWAY_MCP_URL on the runtime to route tool calls through the gateway (and
# thus the Cedar policy engine -- the deterministic tool-call boundary).
# Absent, the agent connects direct to GitHub's official remote MCP server
# (pre-gateway behavior). The gateway fronts GitHub and Asana behind one URL.
_DIRECT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
GITHUB_MCP_URL = os.environ.get("GATEWAY_MCP_URL") or _DIRECT_GITHUB_MCP_URL
