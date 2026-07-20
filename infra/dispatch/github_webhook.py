"""GitHub App Webhook Receiver Lambda.

Receives webhook deliveries from the fleet's GitHub App and forwards recognized
@agent mentions to the Dispatch Router — the server-side replacement for the old
`.github/workflows/agent-dispatch.yml`, which required a GitHub Actions workflow
(and an OIDC deploy role) in every onboarded repo. With the GitHub App, one
webhook endpoint receives events for every repo the App is installed on, so
onboarding a repo needs no per-repo workflow and no repo-side AWS credential.

Sits behind API Gateway (public HTTPS). Security model mirrors the Asana receiver
(asana_webhook.py):
  - Every delivery is authenticated by its HMAC-SHA256 signature
    (``X-Hub-Signature-256: sha256=<hex>``) against the App's webhook secret,
    stored as an SSM SecureString. Missing/empty secret hard-fails closed.
  - The webhook secret is fetched per-invocation and held only in local scope,
    never a module global (threat T-8).
  - Full issue/PR context is fetched server-side with a per-repo, least-privilege
    GitHub App installation token (github_app.installation_token_for_repo) —
    replacing the workflow's ``gh api`` calls that ran with the Actions token.

Handled events: ``issue_comment`` (created) and ``pull_request_review_comment``
(created). A comment must @mention a known agent; otherwise it's a no-op 200.
"""

import hashlib
import hmac
import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
# The App webhook secret — an SSM SecureString, populated when the App is
# registered (github_client.exchange_manifest_code writes it during the admin
# manifest flow). Fetched per invocation, never cached in a module global.
GITHUB_WEBHOOK_SECRET_PARAM = os.environ.get(
    "GITHUB_WEBHOOK_SECRET_PARAM", "/sdlc-agents/github-webhook-secret"
)

_ssm = boto3.client("ssm")
_lambda = boto3.client("lambda")

# @agent mention → canonical agent id. The router does the authoritative
# resolution against the live registry; this only decides WHICH agent a comment
# is addressed to (and short-circuits comments that mention no agent). Aliases
# mirror asana_webhook.ALIAS_MAP so both sources resolve the same names.
MENTION_PATTERN = re.compile(
    r"@(workitems|uat|researcher|docwriter|feedback|merge|triage|adr|pm|status|plan|qa|test|ba|research|analyze|docs|doc|writer)\b",
    re.IGNORECASE,
)
ALIAS_MAP = {
    "pm": "workitems",
    "status": "workitems",
    "plan": "workitems",
    "qa": "uat",
    "test": "uat",
    "ba": "researcher",
    "research": "researcher",
    "analyze": "researcher",
    "docs": "docwriter",
    "doc": "docwriter",
    "writer": "docwriter",
}


def _get_secret() -> str | None:
    try:
        resp = _ssm.get_parameter(Name=GITHUB_WEBHOOK_SECRET_PARAM, WithDecryption=True)
    except _ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"] or None


def _verify_signature(secret: str, raw_body: str, signature_header: str) -> bool:
    """Constant-time check of the ``sha256=<hex>`` GitHub signature. GitHub signs
    the EXACT raw request body, so the caller must pass the unparsed body."""
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def _resolve_agent(body: str) -> str | None:
    match = MENTION_PATTERN.search(body or "")
    if not match:
        return None
    name = match.group(1).lower()
    return ALIAS_MAP.get(name, name)


def _dispatch(agent_id: str, instruction: str, sender: str, context: dict, trigger_type: str):
    payload = {
        "source": "github",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s: github/%s", agent_id, trigger_type)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async — don't block the webhook response
        Payload=json.dumps(payload).encode(),
    )


def _issue_context(repo: str, issue_number: int) -> dict:
    """Fetch full issue/PR context server-side with a per-repo App token — the
    context the router + agent need, matching what agent-dispatch.yml assembled
    via ``gh api``. Best-effort: if enrichment fails, dispatch still proceeds with
    the base context so a transient GitHub error doesn't drop the mention."""
    ctx = {"repo": repo, "issue_number": str(issue_number)}
    try:
        import github_app
        import requests

        token = github_app.installation_token_for_repo(repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        base = f"{github_app.GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
        issue = requests.get(base, headers=headers, timeout=10).json()
        comments = requests.get(f"{base}/comments", headers=headers, timeout=10).json()
        ctx.update(
            {
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body") or "",
                "issue_labels": ", ".join(l.get("name", "") for l in issue.get("labels", [])),
                "issue_assignees": ", ".join(a.get("login", "") for a in issue.get("assignees", [])),
                "issue_state": issue.get("state", ""),
                "is_pr": "true" if issue.get("pull_request") else "false",
                "issue_comments": "\n".join(
                    f"[{c.get('user', {}).get('login', '?')} at {c.get('created_at', '')}]:\n{c.get('body', '')}\n"
                    for c in (comments if isinstance(comments, list) else [])
                ),
            }
        )
    except Exception:  # noqa: BLE001
        # Never let enrichment failure drop the dispatch; log without echoing
        # any token/PII, and proceed with the base context.
        logger.exception("issue context enrichment failed for %s#%s", repo, issue_number)
    return ctx


def _process_comment(payload: dict, trigger_type: str):
    """Handle an issue_comment / pull_request_review_comment 'created' event."""
    if payload.get("action") != "created":
        return
    comment = payload.get("comment", {})
    body = comment.get("body", "")
    agent_id = _resolve_agent(body)
    if not agent_id:
        return  # no known agent mentioned

    # sender = the commenter's GitHub LOGIN (stable, used by the router's
    # authorization allowlist). GitHub logins are unique + not self-editable to
    # an arbitrary existing login, so they're a safe principal (parallels the
    # Asana receiver keying on the immutable user gid).
    sender = comment.get("user", {}).get("login", "")
    repo = payload.get("repository", {}).get("full_name", "")
    issue = payload.get("issue") or payload.get("pull_request") or {}
    issue_number = issue.get("number")
    if not repo or issue_number is None:
        logger.warning("comment event missing repo/issue number; skipping")
        return

    instruction = body.strip()
    context = _issue_context(repo, issue_number)
    _dispatch(agent_id, instruction, sender, context, trigger_type)


# Event type (X-GitHub-Event header) → processor trigger_type.
_EVENT_TRIGGER = {
    "issue_comment": "comment_mention",
    "pull_request_review_comment": "pr_comment",
}


def handler(event, context=None):
    """API Gateway entry point for GitHub App webhook deliveries."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""

    # --- authenticate the delivery ---
    secret = _get_secret()
    if not secret:
        logger.error(
            "GitHub webhook secret %s not set — refusing events. Register the "
            "GitHub App in the dashboard admin UI first.",
            GITHUB_WEBHOOK_SECRET_PARAM,
        )
        return {"statusCode": 503, "body": "webhook not configured"}
    signature = headers.get("x-hub-signature-256", "")
    if not _verify_signature(secret, raw_body, signature):
        logger.warning("Invalid GitHub webhook signature")
        return {"statusCode": 401, "body": "invalid signature"}

    event_type = headers.get("x-github-event", "")
    if event_type == "ping":
        # GitHub sends a ping on webhook creation — acknowledge it.
        return {"statusCode": 200, "body": "pong"}

    trigger_type = _EVENT_TRIGGER.get(event_type)
    if not trigger_type:
        # A subscribed event we don't route (push, etc.) — ack without action.
        return {"statusCode": 200, "body": "ignored"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    try:
        _process_comment(payload, trigger_type)
    except Exception:  # noqa: BLE001
        logger.exception("error processing GitHub %s event", event_type)
        return {"statusCode": 500, "body": "processing error"}
    return {"statusCode": 200, "body": "ok"}
