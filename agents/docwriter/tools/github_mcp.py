"""GitHub MCP server connection.

Connects to GitHub's official remote MCP server
(https://api.githubcopilot.com/mcp/) using an OAuth token.

Two credential modes, selected by ``GITHUB_AUTH_MODE`` (default ``pat``):
  - ``pat``: the shared token from SSM (``/sdlc-agents/github-mcp-token``) or
    the ``GITHUB_MCP_TOKEN`` env var for local dev.
  - ``app``: a per-owner GitHub App installation token minted just-in-time for
    the dispatched repo's owner — a bounded credential that can only touch that
    owner's installed repos (threat-model T-11). See
    agents/shared/tools/github_app.py.
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
    """Get the shared GitHub PAT from env or SSM (PAT mode)."""
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


def github_bearer_token(dispatch_repo: str | None = None) -> str:
    """The bearer token for the direct GitHub MCP client.

    In ``GITHUB_AUTH_MODE=app`` (per-owner GitHub App), mint a short-lived
    installation token scoped to the DISPATCHED repo's owner. In the default PAT
    mode, return the shared token. ``dispatch_repo`` is the ``owner/repo`` the
    agent was dispatched against; App mode needs it, PAT mode ignores it.
    """
    from shared.tools import github_app

    if github_app.app_mode():
        if not dispatch_repo:
            # App mode scopes the token to an owner; a GitHub dispatch always
            # carries owner/repo. Fail loudly rather than fall back to a broad
            # credential that App mode exists to eliminate.
            raise github_app.GitHubAppError(
                "GITHUB_AUTH_MODE=app but no dispatch repo/owner to scope the token"
            )
        return github_app.token_for_dispatch(dispatch_repo)
    return get_github_token()


# GitHub official remote MCP server (direct connection). Gateway routing, when
# enabled, is handled in agent.py via shared/tools/gateway.py — this constant is
# used only for the direct (non-gateway) bearer client.
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
