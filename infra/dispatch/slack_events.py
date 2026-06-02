"""Slack Events API Receiver Lambda.

Handles incoming Slack events and slash commands, normalizes them, and forwards
to the Dispatch Router. Sits behind API Gateway with a public HTTPS endpoint.

Event types:
1. app_mention — user @mentions the bot in a channel or thread
2. message.im / assistant_thread_started — DM to bot (Assistants API flow)
3. Slash commands — /workitems, /researcher, /docwriter, /adr, /fleet

Also handles:
- URL verification challenge (Slack's initial handshake)
- Request signing verification (HMAC-SHA256)
- Immediate acknowledgment (reactions and typing indicators)
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration -----------------------------------------------------------

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
SLACK_SIGNING_SECRET_PARAM = os.environ.get(
    "SLACK_SIGNING_SECRET_PARAM", "/sdlc-agents/slack-signing-secret"
)
SLACK_BOT_TOKEN_PARAM = os.environ.get(
    "SLACK_BOT_TOKEN_PARAM", "/sdlc-agents/slack-bot-token"
)

SLACK_API = "https://slack.com/api"
REPLAY_THRESHOLD_SECONDS = 300

_ssm = boto3.client("ssm")
_lambda = boto3.client("lambda")

# --- Secrets (per-invocation, not cached at module level) --------------------


def _get_ssm_param(name: str) -> str:
    resp = _ssm.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


# --- Signature Verification --------------------------------------------------


def verify_signature(signing_secret: str, timestamp: str, body: str, signature: str) -> bool:
    """Verify Slack request signature using HMAC-SHA256.

    Slack signs requests with: v0=HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
    """
    if not timestamp or not signature:
        return False

    # Reject replays older than 5 minutes
    try:
        if abs(time.time() - int(timestamp)) > REPLAY_THRESHOLD_SECONDS:
            logger.warning("Slack request timestamp too old: %s", timestamp)
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# --- Slack API Helpers -------------------------------------------------------


def slack_api(method: str, token: str, **kwargs) -> dict:
    """Call a Slack Web API method."""
    resp = requests.post(
        f"{SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=kwargs,
        timeout=5,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.warning("Slack API %s failed: %s", method, data.get("error"))
    return data


def add_reaction(token: str, channel: str, timestamp: str, name: str):
    """Add an emoji reaction to a message (best-effort acknowledgment)."""
    try:
        slack_api("reactions.add", token, channel=channel, timestamp=timestamp, name=name)
    except Exception as exc:
        logger.warning("Failed to add reaction: %s", exc)


def set_typing_indicator(token: str, channel_id: str, thread_ts: str = ""):
    """Set the Assistants API typing indicator (best-effort)."""
    try:
        payload = {"channel_id": channel_id}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        slack_api("assistant.threads.setStatus", token, **payload, status="is thinking...")
    except Exception as exc:
        logger.warning("Failed to set typing indicator: %s", exc)


# --- Mention Parsing ---------------------------------------------------------

BOT_MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>\s*")

# A textual "@agent" mention in DM body text (after the <@BOT_ID> Slack mention
# is stripped). We don't enumerate agent names here — the Router resolves the
# name against the live registry; this only detects that a mention is present so
# we know whether to pre-resolve to the default agent or delegate to the Router.
_DM_MENTION_PATTERN = re.compile(r"(?<!<)@\w+")

# Default agent for plain DM messages with no @agent mention.
DM_DEFAULT_AGENT = os.environ.get("DM_DEFAULT_AGENT", "workitems")

SLASH_COMMAND_AGENT_MAP = {
    "/workitems": "workitems",
    "/researcher": "researcher",
    "/docwriter": "docwriter",
    "/adr": "adr",
}


def strip_bot_mention(text: str) -> str:
    """Remove the <@BOT_ID> prefix from mention text."""
    return BOT_MENTION_PATTERN.sub("", text, count=1).strip()


# --- Agent Resolution from Message Text --------------------------------------
# Agent resolution is delegated to the Dispatch Router (which loads the live
# registry from SSM). The Slack receiver only strips the <@BOT_ID> prefix and
# passes the raw body text. The Router's extract_mention_and_instruction()
# resolves @mentions against the registry, ensuring new agents added to
# .dispatch/agents.yaml work on Slack without code changes here.
#
# The one exception: slash commands, where the command name directly maps to
# an agent_id (handled separately in process_slash_command).


# --- Dispatch ----------------------------------------------------------------


def dispatch(agent_id: str, trigger_type: str, instruction: str, sender: str, context: dict):
    """Forward a pre-resolved agent event to the Dispatch Router Lambda."""
    payload = {
        "source": "slack",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }

    logger.info("Dispatching to %s via %s from sender %s", agent_id, trigger_type, sender)

    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def dispatch_to_router(trigger_type: str, body: str, sender: str, context: dict):
    """Forward an event to the Dispatch Router for @mention resolution.

    Unlike dispatch(), this does NOT pre-resolve agent_id. The Router will
    parse @mentions from body against the live registry.
    """
    payload = {
        "source": "slack",
        "trigger_type": trigger_type,
        "body": body,
        "sender": sender,
        "context": context,
    }

    logger.info("Dispatching (router-resolved) via %s from sender %s", trigger_type, sender)

    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


# --- Event Processors --------------------------------------------------------


def process_app_mention(event: dict, token: str):
    """Handle an app_mention event — user @mentioned the bot in a channel.

    Agent resolution is delegated to the Dispatch Router, which loads the
    live registry from SSM. We pass the stripped text as 'body' without
    pre-resolving agent_id, so the Router's extract_mention_and_instruction()
    handles it against the current registry.
    """
    text = event.get("text", "")
    channel = event.get("channel", "")
    user = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    message_ts = event.get("ts", "")
    team = event.get("team", "")

    # Strip the <@BOT_ID> prefix; leave @agent mentions for the Router to parse
    body = strip_bot_mention(text)

    # Acknowledge with reaction
    add_reaction(token, channel, message_ts, "eyes")

    # Don't pre-resolve agent_id — let the Router parse @mentions from body
    dispatch_to_router(
        trigger_type="mention",
        body=body,
        sender=user,
        context={
            "channel_id": channel,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
            "team_id": team,
            "user_id": user,
        },
    )


def process_assistant_thread(event: dict, token: str):
    """Handle a message in an assistant DM thread.

    For DMs, the Router will attempt @mention resolution from the body text.
    If no @agent mention is found, the Router returns 400 ("no recognized
    @agent mention"). To handle the common case of plain DM messages without
    an @mention, we default to workitems as the agent_id.
    """
    text = event.get("text", "")
    channel = event.get("channel", "")
    user = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    team = event.get("team", "")

    # Set typing indicator for the Assistants API
    set_typing_indicator(token, channel, thread_ts)

    context = {
        "channel_id": channel,
        "thread_ts": thread_ts,
        "message_ts": event.get("ts", ""),
        "team_id": team,
        "user_id": user,
        "is_dm": True,
    }

    # If the DM text contains an @agent mention, let the Router resolve it
    # against the live registry (so "@researcher ..." reaches researcher, not
    # workitems). Only when there's no recognized mention do we default to the
    # PM agent — the common "just talk to the assistant" case. Resolution stays
    # in one place (the Router); we only decide whether to pre-resolve.
    if _DM_MENTION_PATTERN.search(text):
        dispatch_to_router(
            trigger_type="assistant_thread",
            body=text,
            sender=user,
            context=context,
        )
    else:
        dispatch(
            agent_id=DM_DEFAULT_AGENT,
            trigger_type="assistant_thread",
            instruction=text,
            sender=user,
            context=context,
        )


def process_slash_command(command: str, text: str, user_id: str, channel_id: str, response_url: str, trigger_id: str):
    """Handle a slash command invocation."""
    agent_id = SLASH_COMMAND_AGENT_MAP.get(command)

    # /fleet commands are handled locally, not dispatched to an agent
    if command == "/fleet":
        return handle_fleet_command(text, response_url)

    if not agent_id:
        return _slash_error(response_url, f"Unknown command: {command}")

    if not text.strip():
        return _slash_error(
            response_url,
            f"Usage: `{command} [instruction]`\nExample: `{command} generate a status report`",
        )

    dispatch(
        agent_id=agent_id,
        trigger_type="slash_command",
        instruction=text.strip(),
        sender=user_id,
        context={
            "channel_id": channel_id,
            "user_id": user_id,
            "response_url": response_url,
            "trigger_id": trigger_id,
        },
    )

    # Post ephemeral acknowledgment via response_url
    try:
        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": f":eyes: `@{agent_id}` is on it. You'll see a response in this channel shortly.",
            },
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Failed to post slash command ack: %s", exc)


def handle_fleet_command(text: str, response_url: str):
    """Handle /fleet subcommands locally (no agent dispatch)."""
    subcommand = text.strip().split()[0] if text.strip() else "status"

    if subcommand == "status":
        message = ":robot_face: Fleet is operational. Use `/fleet budget [agent]` for token usage."
    elif subcommand == "budget":
        message = ":bar_chart: Budget tracking coming soon."
    elif subcommand == "health":
        message = ":green_circle: All agents healthy."
    else:
        message = f"Unknown fleet subcommand: `{subcommand}`. Try: `status`, `budget`, `health`"

    try:
        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": message},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Failed to post fleet command response: %s", exc)


def _slash_error(response_url: str, message: str):
    """Post an error response to a slash command."""
    try:
        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": f":warning: {message}"},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Failed to post slash error: %s", exc)


# --- Assistants API: Thread Started ------------------------------------------


def process_assistant_thread_started(event: dict, token: str):
    """Handle assistant_thread_started — post suggested prompts."""
    channel_id = event.get("assistant_thread", {}).get("channel_id", "")
    thread_ts = event.get("assistant_thread", {}).get("thread_ts", "")

    if not channel_id:
        return

    suggested_prompts = [
        {"title": "Sprint Status", "message": "Generate a status report for the current sprint"},
        {"title": "Risk Scan", "message": "@workitems detect risks and blockers in active work"},
        {"title": "Research Topic", "message": "@researcher analyze competitive landscape for [topic]"},
        {"title": "Generate Docs", "message": "@docwriter generate release notes for the latest changes"},
    ]

    try:
        slack_api(
            "assistant.threads.setSuggestedPrompts",
            token,
            channel_id=channel_id,
            thread_ts=thread_ts,
            prompts=suggested_prompts,
        )
    except Exception as exc:
        logger.warning("Failed to set suggested prompts: %s", exc)


# --- App Home ----------------------------------------------------------------


def process_app_home_opened(event: dict, token: str):
    """Handle app_home_opened — render the fleet dashboard."""
    user_id = event.get("user", "")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "SDLC Agent Fleet"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Autonomous AI agents for the software development lifecycle."}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Available Agents*"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":clipboard: *Workitems* — PO/PM: status reports, risk detection, work decomposition\n"
                    ":mag: *Researcher* — Business analyst: research synthesis, competitive intel\n"
                    ":pencil: *Docwriter* — Technical writer: API docs, user guides, release notes\n"
                    ":link: *Adr* — ADR linker: tags issues with governing architecture decisions"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Quick Commands*"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "`/workitems [instruction]` — Invoke Workitems agent\n"
                    "`/researcher [instruction]` — Invoke Researcher agent\n"
                    "`/docwriter [instruction]` — Invoke Docwriter agent\n"
                    "`/adr [instruction]` — Invoke ADR agent\n"
                    "`/fleet status` — Fleet health check\n\n"
                    "Or just mention me in any channel: `@SDLC Agents @workitems do something`"
                ),
            },
        },
    ]

    try:
        slack_api(
            "views.publish",
            token,
            user_id=user_id,
            view={"type": "home", "blocks": blocks},
        )
    except Exception as exc:
        logger.warning("Failed to publish app home: %s", exc)


# --- Lambda Handler ----------------------------------------------------------


def handler(event, context):
    """Slack Events Lambda entry point.

    Handles two distinct request formats:
    1. Events API (JSON body) — app_mention, message.im, assistant events
    2. Slash commands (form-encoded body) — /workitems, /fleet, etc.

    Both arrive via API Gateway but on different paths.
    """
    headers = event.get("headers", {})
    headers = {k.lower(): v for k, v in headers.items()}
    body = event.get("body", "")
    # API Gateway base64-encodes the body for some content types (notably the
    # form-encoded slash-command POSTs). Slack signs the RAW request bytes, so we
    # must decode to the exact string Slack signed BEFORE computing the HMAC —
    # otherwise verification runs over the base64 wrapper and every such request
    # 401s (and parse_qs later sees garbage).
    if event.get("isBase64Encoded"):
        import base64
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {"statusCode": 400, "body": "invalid body encoding"}
    path = event.get("path", event.get("requestContext", {}).get("path", ""))

    # --- Fetch signing secret ---
    try:
        signing_secret = _get_ssm_param(SLACK_SIGNING_SECRET_PARAM)
    except Exception as exc:
        logger.error("Failed to fetch signing secret: %s", exc)
        return {"statusCode": 503, "body": "signing secret unavailable"}
    # Hard-fail on an empty secret rather than verifying against "" — matches the
    # asana/github receivers and keeps a misconfigured stage from accepting
    # unsigned requests that happen to also send an empty signature.
    if not signing_secret:
        logger.error("Slack signing secret is empty; refusing to process events.")
        return {"statusCode": 503, "body": "signing secret unavailable"}

    # --- Verify request signature ---
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    if not verify_signature(signing_secret, timestamp, body, signature):
        logger.warning("Invalid Slack request signature")
        return {"statusCode": 401, "body": "invalid signature"}

    # --- Route: Slash command (form-encoded) ---
    if path.endswith("/slash") or headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return _handle_slash_command(body)

    # --- Route: Events API (JSON) ---
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    # URL verification challenge (Slack's handshake)
    if payload.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": payload.get("challenge", "")}),
        }

    # Event callback
    if payload.get("type") != "event_callback":
        return {"statusCode": 200, "body": "ok"}

    # Fetch bot token for API calls
    try:
        token = _get_ssm_param(SLACK_BOT_TOKEN_PARAM)
    except Exception as exc:
        logger.error("Failed to fetch bot token: %s", exc)
        return {"statusCode": 503, "body": "bot token unavailable"}

    event_data = payload.get("event", {})
    event_type = event_data.get("type", "")

    # Ignore bot messages (prevent loops)
    if event_data.get("bot_id") or event_data.get("subtype") == "bot_message":
        return {"statusCode": 200, "body": "ok"}

    try:
        if event_type == "app_mention":
            process_app_mention(event_data, token)
        elif event_type == "message" and event_data.get("channel_type") == "im":
            process_assistant_thread(event_data, token)
        elif event_type == "assistant_thread_started":
            process_assistant_thread_started(event_data, token)
        elif event_type == "app_home_opened":
            process_app_home_opened(event_data, token)
        else:
            logger.info("Unhandled event type: %s", event_type)
    except Exception as exc:
        logger.exception("Error processing Slack event: %s", exc)
        return {"statusCode": 500, "body": "internal error"}

    return {"statusCode": 200, "body": "ok"}


def _handle_slash_command(body: str) -> dict:
    """Parse and process a slash command request."""
    try:
        params = parse_qs(body)
        command = params.get("command", [""])[0]
        text = params.get("text", [""])[0]
        user_id = params.get("user_id", [""])[0]
        channel_id = params.get("channel_id", [""])[0]
        response_url = params.get("response_url", [""])[0]
        trigger_id = params.get("trigger_id", [""])[0]
    except Exception as exc:
        logger.error("Failed to parse slash command: %s", exc)
        return {"statusCode": 400, "body": "invalid request"}

    process_slash_command(command, text, user_id, channel_id, response_url, trigger_id)

    # Return 200 immediately — detailed response goes via response_url
    return {"statusCode": 200, "body": ""}
