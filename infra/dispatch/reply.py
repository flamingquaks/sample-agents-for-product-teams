"""Post "blocked by safety filter" replies back to the originating thread.

When the Dispatch Router's guardrail check blocks a request, we post a
short note back to the GitHub issue or Asana task that originated the
mention so the sender sees what happened (no silent failures).

Both helpers return a bool instead of raising: the block decision has
already been made by the time we call these, and a failed reply must
not revert that decision. Reply failures emit a CloudWatch metric so
operators can alarm separately (`GuardrailReplyFailed`).
"""

import logging
import os
from typing import Optional

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
ASANA_API = "https://app.asana.com/api/1.0"
SLACK_API = "https://slack.com/api"

ASANA_PAT_PARAM_ENV = "ASANA_PAT_PARAM"

_ssm = boto3.client("ssm")


def post_github_comment(repo: str, issue_number: str | int, body: str) -> bool:
    """Post a comment to a GitHub issue or PR. Returns True on success."""
    if not repo or not issue_number:
        logger.error("post_github_comment missing repo or issue_number")
        return False

    token = _github_token(repo)
    if not token:
        return False

    url = f"{GITHUB_API}/repos/{repo}/issues/{issue_number}/comments"
    try:
        response = requests.post(
            url,
            json={"body": body},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Failed to post GitHub comment to %s#%s: %s", repo, issue_number, exc)
        return False


def post_asana_comment(task_gid: str, body: str) -> bool:
    """Post a story (comment) to an Asana task. Returns True on success."""
    if not task_gid:
        logger.error("post_asana_comment missing task_gid")
        return False

    token = _get_secret(os.environ.get(ASANA_PAT_PARAM_ENV, "/sdlc-agents/asana-pat"))
    if not token:
        return False

    url = f"{ASANA_API}/tasks/{task_gid}/stories"
    try:
        response = requests.post(
            url,
            json={"data": {"text": body}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Failed to post Asana comment to task %s: %s", task_gid, exc)
        return False


def slack_bot_token_param(team_id: str) -> str:
    """The SSM SecureString path holding a workspace's bot token. Layout matches
    config_store._slack_secret_param (the admin API writes it). ``STAGE`` names
    the deploy stage (the receiver Lambda has it in env)."""
    stage = os.environ.get("STAGE", "dev")
    return f"/sdlc-agents/{stage}/slack/{team_id}/bot-token"


def post_slack_message(
    team_id: str, channel: str, body: str, thread_ts: str | None = None
) -> bool:
    """Post a message to a Slack channel/thread via chat.postMessage. Returns True
    on success. Thin bool wrapper over ``post_slack_message_ts`` for the reply
    call sites that don't need the message ts (block/reject notices)."""
    ok, _ = post_slack_message_ts(team_id, channel, body, thread_ts)
    return ok


def post_slack_message_ts(
    team_id: str, channel: str, body: str, thread_ts: str | None = None
) -> tuple[bool, str | None]:
    """Post to Slack and return ``(ok, ts)`` — ``ts`` is the posted message's
    timestamp (the value a follow-up passes as ``thread_ts`` to thread under it),
    or None on failure. Used by notify.py for threaded notifications (spec §18.4).

    Multi-workspace: the bot token is fetched per-invocation from the workspace's
    SSM SecureString (never a module global — threat T-8/T-36). Non-fatal on
    failure — callers treat a failed post as best-effort. Slack's Web API returns
    HTTP 200 even on a logical error (``{"ok": false}``), so we check ``ok``."""
    if not team_id or not channel:
        logger.error("post_slack_message missing team_id or channel")
        return False, None
    token = _get_secret(slack_bot_token_param(team_id))
    if not token:
        return False, None
    payload = {"channel": channel, "text": body}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        response = requests.post(
            f"{SLACK_API}/chat.postMessage",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error(
                "Slack chat.postMessage rejected for %s/%s: %s",
                team_id,
                channel,
                data.get("error", "unknown"),
            )
            return False, None
        return True, data.get("ts")
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to post Slack message to %s/%s: %s", team_id, channel, exc)
        return False, None


def slack_user_profile(team_id: str, user_id: str) -> dict:
    """Fetch a Slack user's profile via ``users.info`` — the workspace's
    AUTHENTICATED directory, so the display name + email it returns are verified
    (identity.py §16.5 / T-42: only such a source may seed the golden-join email).

    Returns ``{"display_name": ..., "email": ...}`` with whatever was resolvable;
    empty strings on any miss. Best-effort like the other Slack calls: a failed
    lookup must not block dispatch (the caller degrades to the raw handle). Slack
    returns HTTP 200 even on a logical error (``{"ok": false}``), so we check
    ``ok``. Requires the ``users:read`` + ``users:read.email`` scopes the app
    manifest already requests."""
    if not team_id or not user_id:
        return {"display_name": "", "email": ""}
    token = _get_secret(slack_bot_token_param(team_id))
    if not token:
        return {"display_name": "", "email": ""}
    try:
        response = requests.get(
            f"{SLACK_API}/users.info",
            params={"user": user_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error(
                "Slack users.info rejected for %s/%s: %s",
                team_id,
                user_id,
                data.get("error", "unknown"),
            )
            return {"display_name": "", "email": ""}
        profile = (data.get("user") or {}).get("profile") or {}
        # Prefer the user's chosen display name, then their real name; Slack
        # leaves display_name blank when the user never set one.
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or (data.get("user") or {}).get("real_name")
            or ""
        ).strip()
        return {"display_name": display_name, "email": (profile.get("email") or "").strip()}
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to fetch Slack profile for %s/%s: %s", team_id, user_id, exc)
        return {"display_name": "", "email": ""}


def _github_token(repo: str) -> Optional[str]:
    """The GitHub bearer token for posting to ``repo``: a per-owner GitHub App
    installation token (bounded to that owner's installed repos — threat-model
    T-11). Returns None on failure (the caller treats a failed reply as
    non-fatal — the block decision already stands)."""
    import github_app

    try:
        return github_app.installation_token_for_repo(repo)
    except github_app.GitHubAppError as exc:
        logger.error("Failed to mint GitHub App token for %s: %s", repo, exc)
        return None


def _get_secret(param_name: str) -> Optional[str]:
    try:
        resp = _ssm.get_parameter(Name=param_name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to fetch SSM parameter %s: %s", param_name, exc)
        return None
