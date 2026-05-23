"""Post "blocked by safety filter" replies back to the originating thread.

When the Dispatch Router's guardrail check blocks a request, we post a
short note back to the GitHub issue, Asana task, or Slack channel/thread
that originated the mention so the sender sees what happened (no silent
failures).

All helpers return a bool instead of raising: the block decision has
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

GITHUB_PAT_PARAM_ENV = "GITHUB_PAT_PARAM"
ASANA_PAT_PARAM_ENV = "ASANA_PAT_PARAM"
SLACK_BOT_TOKEN_PARAM_ENV = "SLACK_BOT_TOKEN_PARAM"

_ssm = boto3.client("ssm")


def post_github_comment(repo: str, issue_number: str | int, body: str) -> bool:
    """Post a comment to a GitHub issue or PR. Returns True on success."""
    if not repo or not issue_number:
        logger.error("post_github_comment missing repo or issue_number")
        return False

    token = _get_secret(os.environ.get(GITHUB_PAT_PARAM_ENV, "/sdlc-agents/github-pat"))
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


def post_slack_message(channel: str, body: str, thread_ts: str | None = None) -> bool:
    """Post a message to a Slack channel or thread. Returns True on success.

    Reads the bot token from the SSM parameter named by the
    SLACK_BOT_TOKEN_PARAM env var (default /sdlc-agents/slack-bot-token).
    Calls chat.postMessage with a 10-second timeout. Never raises.
    """
    if not channel:
        logger.error("post_slack_message missing channel")
        return False

    token = _get_secret(os.environ.get(SLACK_BOT_TOKEN_PARAM_ENV, "/sdlc-agents/slack-bot-token"))
    if not token:
        return False

    payload: dict = {"channel": channel, "text": body}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    try:
        response = requests.post(
            f"{SLACK_API}/chat.postMessage",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error("Slack chat.postMessage returned error: %s", data.get("error"))
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Failed to post Slack message to channel %s: %s", channel, exc)
        return False


def _get_secret(param_name: str) -> Optional[str]:
    try:
        resp = _ssm.get_parameter(Name=param_name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to fetch SSM parameter %s: %s", param_name, exc)
        return None
