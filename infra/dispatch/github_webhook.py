"""GitHub PM Webhook Receiver Lambda.

Handles incoming GitHub webhook events for PM-style triggers and forwards
recognized agent work to the Dispatch Router. Sits behind API Gateway with a
public HTTPS endpoint, paralleling the Asana webhook receiver.

Trigger types:
1. Issue assignment — an issue is assigned to a configured bot login
   (e.g. workitems-bot). Resolves to a pre-resolved agent_id.
2. Projects V2 item edited — a board item's STATUS (single-select) field
   changed. This is the PM "card moved between columns" signal. Defaults to
   the workitems (PM) agent.
3. Comment mention — a new issue comment contains an @agent mention. The
   agent is NOT pre-resolved here; the Dispatch Router parses the @mention
   against the live registry.

Also handles GitHub's `ping` event (sent on webhook registration).

Signature verification:
    GitHub signs each delivery with the `X-Hub-Signature-256` header, which is
    "sha256=" + HMAC-SHA256(secret, raw_request_body). We verify with
    hmac.compare_digest and reject mismatches with 401.
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

# --- Configuration -----------------------------------------------------------

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")

# Secrets are fetched at invocation time, not at cold start. Retaining the
# webhook secret in a module-level global across invocations widens the
# exposure window of a memory-disclosure or verbose-log incident (threat T-8).
# Mirrors asana_webhook.py / slack_events.py secret hygiene.
_ssm = boto3.client("ssm")

GITHUB_WEBHOOK_SECRET_PARAM = os.environ.get(
    "GITHUB_WEBHOOK_SECRET_PARAM", "/sdlc-agents/github-webhook-secret"
)

lambda_client = boto3.client("lambda")


def _get_ssm_param(name: str) -> str:
    resp = _ssm.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


# --- Agent Resolution --------------------------------------------------------

# Map a GitHub bot login -> agent_id. Logins come from environment variables,
# exactly like asana_webhook.py's BOT_USERS map keyed on bot GIDs. A login is a
# stable, non-editable identifier on GitHub, so matching the assignee against
# this allowlist is a safe authorization signal (cf. asana threat T-4).
BOT_LOGINS = {
    os.environ.get("WORKITEMS_GH_BOT_LOGIN", ""): "workitems",
    os.environ.get("UAT_GH_BOT_LOGIN", ""): "uat",
    os.environ.get("RESEARCHER_GH_BOT_LOGIN", ""): "researcher",
    os.environ.get("DOCWRITER_GH_BOT_LOGIN", ""): "docwriter",
}

# Default agent for Projects V2 board moves — the PM agent owns the board.
PROJECT_ITEM_AGENT = os.environ.get("PROJECT_ITEM_AGENT", "workitems")

# Recognized @agent mentions inside issue comments. Resolution itself is
# delegated to the Router (which loads the live registry); this pattern is only
# a cheap pre-filter so we don't dispatch on every comment.
MENTION_PATTERN = re.compile(r"@(\w+)", re.IGNORECASE)


# --- Dispatch ----------------------------------------------------------------


def dispatch(
    trigger_type: str,
    instruction: str,
    sender: str,
    context: dict,
    agent_id: str | None = None,
):
    """Forward a normalized GitHub event to the Dispatch Router Lambda.

    When ``agent_id`` is provided the Router uses it directly; otherwise the
    key is omitted from the payload and the Router parses the @mention from
    ``body`` against the live registry.
    """
    payload = {
        "source": "github",
        "trigger_type": trigger_type,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    if agent_id:
        payload["agent_id"] = agent_id

    logger.info("Dispatching github %s (agent=%s) from %s", trigger_type, agent_id or "<router>", sender)

    lambda_client.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async — return to GitHub quickly
        Payload=json.dumps(payload).encode(),
    )


# --- Event Processors --------------------------------------------------------


def process_issue_assigned(payload: dict):
    """Handle `issues` / action=assigned — issue assigned to a bot login."""
    issue = payload.get("issue", {}) or {}
    # The assignee that triggered THIS event (GitHub also sends the full
    # assignees array). The webhook's top-level `assignee` is the one just added.
    assignee = payload.get("assignee", {}) or {}
    assignee_login = assignee.get("login", "")

    agent_id = BOT_LOGINS.get(assignee_login)
    if not agent_id:
        return  # Not assigned to a known bot

    repo = (payload.get("repository", {}) or {}).get("full_name", "")
    issue_number = issue.get("number", "")
    # sender is the user who performed the assignment (immutable login).
    sender = (payload.get("sender", {}) or {}).get("login", "")

    dispatch(
        agent_id=agent_id,
        trigger_type="assignment",
        instruction=(
            f"You have been assigned to GitHub issue #{issue_number} in {repo}. "
            "Read it and take appropriate action."
        ),
        sender=sender,
        context={
            "repo": repo,
            "issue_number": issue_number,
            "issue_title": issue.get("title", ""),
            "issue_body": issue.get("body", "") or "",
            "trigger_type": "assignment",
        },
    )


def process_projects_v2_item(payload: dict):
    """Handle `projects_v2_item` / action=edited — only on a STATUS change.

    Projects V2 board moves between columns surface as a single-select (status)
    field-value change on the project item. We must filter to status changes
    only; otherwise every reorder, date edit, or text-field tweak would trigger
    a dispatch storm.

    The `changes` payload for a field-value edit looks roughly like:

        "changes": {
            "field_value": {
                "field_node_id": "...",
                "field_type": "single_select",
                "field_name": "Status",
                "from": {"id": "...", "name": "Todo"},
                "to":   {"id": "...", "name": "In Progress"}
            }
        }

    GitHub does not always populate `field_name`, so we treat the change as a
    status change when EITHER the field_type is a single-select OR the field
    name looks like a status field. This errs toward dispatching on genuine
    column moves while still filtering out plain text/number/date edits.
    """
    if not _is_status_change(payload.get("changes", {}) or {}):
        logger.info("projects_v2_item edited but not a status change; ignoring")
        return

    item = payload.get("projects_v2_item", {}) or {}
    # Org/user that owns the project. We deliberately do NOT fall back to the
    # org login for `repo`: a Projects V2 board is org/user-owned and can span
    # repos, so the webhook usually omits `repository`. Aliasing org -> repo
    # would produce a bare "acme" (not "acme/web") and break the reply path's
    # /repos/{owner}/{repo}/issues URL. Leave repo empty when GitHub omits it;
    # the agent resolves the issue via the board item's content node instead.
    repo = (payload.get("repository", {}) or {}).get("full_name", "")

    project_number = item.get("project_number", "") or (
        payload.get("projects_v2", {}) or {}
    ).get("number", "")
    item_id = item.get("node_id") or item.get("id", "")

    # GitHub's projects_v2_item webhook carries content_type + content_node_id
    # (a GraphQL node id string), NOT a nested content.number. We pass the node
    # id through so the agent can resolve the issue number via MCP/GraphQL when
    # it needs to comment on the issue. issue_number is only populated on the
    # rare payload variant that does include an explicit number.
    content_node_id = item.get("content_node_id", "") if item.get("content_type") == "Issue" else ""
    content = item.get("content")
    issue_number = ""
    if item.get("content_type") == "Issue" and isinstance(content, dict):
        issue_number = content.get("number", "")

    sender = (payload.get("sender", {}) or {}).get("login", "")
    status_change = _describe_status_change(payload.get("changes", {}) or {})

    dispatch(
        agent_id=PROJECT_ITEM_AGENT,
        trigger_type="project_item",
        instruction=(
            f"A Projects V2 board item changed status ({status_change}) on project "
            f"#{project_number}. Review the item and take appropriate PM action."
        ),
        sender=sender,
        context={
            "repo": repo,
            "project_number": project_number,
            "item_id": item_id,
            "issue_number": issue_number,
            "content_node_id": content_node_id,
            "status_change": status_change,
            # The Phase 1 agent keys its project-item branch on
            # source_context.get("trigger_type") == "project_item".
            "trigger_type": "project_item",
        },
    )


def process_issue_comment(payload: dict):
    """Handle `issue_comment` / action=created — @agent mention pass-through.

    Agent resolution is delegated to the Router (it loads the live registry),
    so we do NOT pre-resolve agent_id. We only pre-filter on an @mention so we
    don't dispatch on every comment.
    """
    comment = payload.get("comment", {}) or {}
    body = comment.get("body", "") or ""
    if not MENTION_PATTERN.search(body):
        return  # no @mention — nothing for the fleet to do

    issue = payload.get("issue", {}) or {}
    repo = (payload.get("repository", {}) or {}).get("full_name", "")
    issue_number = issue.get("number", "")
    sender = (comment.get("user", {}) or {}).get("login", "") or (
        payload.get("sender", {}) or {}
    ).get("login", "")

    dispatch(
        # No agent_id — let the Router parse @mention from body.
        trigger_type="comment_mention",
        instruction=body,
        sender=sender,
        context={
            "repo": repo,
            "issue_number": issue_number,
            "issue_title": issue.get("title", ""),
            "issue_body": issue.get("body", "") or "",
        },
    )


# --- Projects V2 status-change detection -------------------------------------


def _is_status_change(changes: dict) -> bool:
    """Return True when the `changes` payload describes a status field move.

    A Projects V2 status column move arrives as a `field_value` change on the
    single-select field named "Status". We key PRIMARILY on the field name so
    that edits to OTHER single-select fields (Priority, Size, Team, ...) do not
    each trigger a full agent run — a board with several single-selects would
    otherwise cause dispatch storms (and burn the receiver's reserved
    concurrency). We fall back to the single_select type only when the field
    name is genuinely absent from the payload.
    Everything else (text/number/date/iteration edits, reorders) is ignored.
    """
    field_value = changes.get("field_value")
    if not isinstance(field_value, dict):
        return False
    field_type = (field_value.get("field_type") or "").lower()
    field_name = (field_value.get("field_name") or "").lower()
    if field_name:
        return field_name == "status"
    # No field name in the payload — fall back to the single-select heuristic.
    return field_type == "single_select"


def _describe_status_change(changes: dict) -> str:
    """Build a human-readable "old → new" string from a field_value change."""
    field_value = changes.get("field_value") or {}
    old = field_value.get("from") or {}
    new = field_value.get("to") or {}
    old_name = old.get("name") if isinstance(old, dict) else old
    new_name = new.get("name") if isinstance(new, dict) else new
    if old_name and new_name:
        return f"{old_name} → {new_name}"
    if new_name:
        return f"→ {new_name}"
    return "status changed"


# --- Lambda Handler ----------------------------------------------------------


def handler(event, context):
    """GitHub Webhook Receiver Lambda entry point.

    Fronted by API Gateway. Verifies the X-Hub-Signature-256 HMAC, then routes
    on the X-GitHub-Event header. Dispatch is async (InvocationType="Event") so
    we return 200 to GitHub well within its delivery timeout.
    """
    headers = event.get("headers", {}) or {}
    # Normalize header keys to lowercase
    headers = {k.lower(): v for k, v in headers.items()}
    body = event.get("body", "") or ""
    # API Gateway may base64-encode the delivered body. GitHub signs the raw
    # bytes, so decode to the exact bytes GitHub signed before computing the
    # HMAC; verifying over the base64 wrapper would reject every such delivery.
    raw_body_bytes = None
    if event.get("isBase64Encoded"):
        import base64
        try:
            raw_body_bytes = base64.b64decode(body)
            body = raw_body_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {"statusCode": 400, "body": "invalid body encoding"}

    # --- Verify webhook signature ---
    # Hard-fail on missing/unavailable secret. SecureString rejects empty
    # values in practice; contracting on it here keeps a misconfigured stage
    # from silently accepting unsigned events.
    try:
        webhook_secret = _get_ssm_param(GITHUB_WEBHOOK_SECRET_PARAM)
    except _ssm.exceptions.ParameterNotFound:
        logger.error(
            "Webhook secret parameter %s not found. Run "
            "scripts/bootstrap_github_webhook.py to register the webhook.",
            GITHUB_WEBHOOK_SECRET_PARAM,
        )
        return {"statusCode": 503, "body": "webhook not registered"}
    except Exception as exc:  # noqa: BLE001 — any SSM failure means we can't verify
        logger.error("Failed to fetch webhook secret: %s", exc)
        return {"statusCode": 503, "body": "webhook secret unavailable"}
    if not webhook_secret:
        logger.error(
            "Webhook secret %s is empty; refusing to process events.",
            GITHUB_WEBHOOK_SECRET_PARAM,
        )
        return {"statusCode": 503, "body": "webhook not registered"}

    signature = headers.get("x-hub-signature-256", "")
    # HMAC over the exact bytes GitHub signed: the decoded bytes when the body
    # arrived base64-encoded, otherwise the UTF-8 encoding of the string body.
    body_bytes = raw_body_bytes if raw_body_bytes is not None else body.encode("utf-8")
    expected = "sha256=" + hmac.new(
        webhook_secret.encode(), body_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        logger.warning("Invalid GitHub webhook signature")
        return {"statusCode": 401, "body": "invalid signature"}

    # --- Parse payload ---
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    github_event = headers.get("x-github-event", "")
    action = payload.get("action", "")

    # GitHub's registration ping — acknowledge so the hook shows as healthy.
    if github_event == "ping":
        logger.info("GitHub webhook ping received")
        return {"statusCode": 200, "body": "pong"}

    # Ignore events authored by bots to prevent feedback loops. Assignments are
    # exempt: a human or automation assigning a bot login is the whole point,
    # and over-filtering here would drop legitimate assignment triggers.
    sender_type = (payload.get("sender", {}) or {}).get("type", "")
    if sender_type == "Bot" and not (github_event == "issues" and action == "assigned"):
        logger.info("Ignoring event from bot sender (event=%s action=%s)", github_event, action)
        return {"statusCode": 200, "body": "ok"}

    try:
        if github_event == "issues" and action == "assigned":
            process_issue_assigned(payload)
        elif github_event == "projects_v2_item" and action == "edited":
            process_projects_v2_item(payload)
        elif github_event == "issue_comment" and action == "created":
            process_issue_comment(payload)
        else:
            logger.info("Unhandled github event: %s/%s", github_event, action)
    except Exception as exc:  # noqa: BLE001 — log and surface a 500 to GitHub
        logger.exception("Error processing GitHub event %s/%s: %s", github_event, action, exc)
        return {"statusCode": 500, "body": "internal error"}

    return {"statusCode": 200, "body": "ok"}
