"""Slack Webhook Receiver Lambda.

Handles inbound Slack Events API (POST /slack/events) and Slack slash
commands (POST /slack/commands), verifies signatures, resolves the target
agent, and forwards a normalized dispatch payload to the Dispatch Router.

Two entry points share a single Lambda:
1. Events API — receives app_mention events (user @-mentions the bot)
2. Slash commands — receives /workitems, /docwriter, etc.

No handshake protocol (unlike Asana). Slack verifies ownership by having the
operator paste the endpoint URL into the app config and performing a
url_verification echo challenge. That challenge arrives over the same POST
/slack/events endpoint and is verified with the same HMAC-SHA256 signature
before echoing the challenge back.

Signature verification uses HMAC-SHA256:
    v0:{X-Slack-Request-Timestamp}:{raw-body}
compared timing-safe against the X-Slack-Signature header value (v0=<hex>).
Requests older than 5 minutes are rejected regardless of signature validity
to close the replay window (threat-model analogue to Asana T-8/T-9).

Hard-fail on a missing or empty signing secret keeps a misconfigured stage
from silently accepting unsigned events — mirrors the hard-fail block in
asana_webhook.py.

Single-bot model: one Slack app for the entire fleet. The target agent is
resolved by finding the first @agent mention in the message body (app_mention
path) or by the slash command name (slash_command path). Pre-resolved
agent_id is passed directly to the Dispatch Router via the pre-resolved
agent_id path in router.py.
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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration -----------------------------------------------------------

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")

_ssm = boto3.client("ssm")

SLACK_BOT_TOKEN_PARAM = os.environ.get("SLACK_BOT_TOKEN_PARAM", "/sdlc-agents/slack-bot-token")
SLACK_SIGNING_SECRET_PARAM = os.environ.get("SLACK_SIGNING_SECRET_PARAM", "/sdlc-agents/slack-signing-secret")

lambda_client = boto3.client("lambda")

REPLAY_WINDOW_SECONDS = 300

# Per-container signing-secret cache with a 5-minute TTL. The cache avoids
# an SSM fetch on every request in the steady state (hot-path latency).
# The TTL is rotation tolerance only — a rotated secret is picked up within
# 5 minutes. The hard-fail on an empty secret (see _verify_slack_signature)
# remains unchanged: a cached empty string will still raise ValueError.
_SIGNING_SECRET_TTL_SECONDS = 300
_cached_signing_secret: str | None = None
_cached_signing_secret_at: float = 0.0


def _get_signing_secret() -> str:
    global _cached_signing_secret, _cached_signing_secret_at
    now = time.time()
    if _cached_signing_secret is None or now - _cached_signing_secret_at > _SIGNING_SECRET_TTL_SECONDS:
        _cached_signing_secret = _get_ssm_param(SLACK_SIGNING_SECRET_PARAM)
        _cached_signing_secret_at = now
    return _cached_signing_secret

# --- Agent Resolution --------------------------------------------------------

# Canonical agent names plus aliases, matching the registry's alias map.
# The router's resolve_agent() does the authoritative lookup; this is the
# initial match to catch the first @name token before dispatching.
MENTION_PATTERN = re.compile(
    r"@(workitems|pm|status|plan|docwriter|docs|doc|writer)\b",
    re.IGNORECASE,
)

ALIAS_MAP = {
    "pm": "workitems",
    "status": "workitems",
    "plan": "workitems",
    "docs": "docwriter",
    "doc": "docwriter",
    "writer": "docwriter",
}

# Slash commands map directly by stripping the leading "/".
SLASH_COMMAND_AGENTS = {"workitems", "docwriter"}


def _resolve_mention(text: str) -> tuple[str | None, str]:
    """Find the first recognized @agent mention; return (agent_id, instruction).

    The instruction is everything after the mention token. Call this after
    stripping the <@BOT_ID> prefix that Slack prepends to app_mention text.

    Returns (None, "") if no recognized mention is found.
    """
    match = MENTION_PATTERN.search(text)
    if not match:
        return None, ""
    raw = match.group(1).lower()
    agent_id = ALIAS_MAP.get(raw, raw)
    instruction = text[match.end():].strip()
    return agent_id, instruction


def _strip_bot_mention(text: str) -> str:
    """Remove the leading <@UXXXXXXXXXX> token Slack prepends to app_mention text."""
    return re.sub(r"^<@[A-Z0-9]+>\s*", "", text, count=1)


# --- Secrets / SSM -----------------------------------------------------------


def _get_ssm_param(name: str) -> str:
    resp = _ssm.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


# --- Signature Verification --------------------------------------------------


def _verify_slack_signature(raw_body: str, timestamp: str, signature: str, invocation_state: dict) -> bool:
    """Verify Slack's HMAC-SHA256 request signature.

    Returns True if the signature is valid and the request is within the
    5-minute replay window; False otherwise.

    The signing secret is fetched once per Lambda invocation and stored only
    on `invocation_state`, which goes out of scope when the handler returns
    (threat T-8 analogue — no module-level secret retention).

    Hard-fails (propagates exception) on a missing SSM parameter so a
    misconfigured stage never silently processes unsigned events.
    """
    # Reject requests outside the 5-minute replay window before doing any
    # cryptographic work — avoids wasted SSM fetches for obviously stale
    # replays and closes the replay window regardless of secret validity.
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        logger.warning("Slack request has non-numeric timestamp: %r", timestamp)
        return False

    now = int(time.time())
    if abs(now - ts_int) > REPLAY_WINDOW_SECONDS:
        logger.warning(
            "Slack request timestamp %s is outside the %s-second window (now=%s)",
            timestamp,
            REPLAY_WINDOW_SECONDS,
            now,
        )
        return False

    signing_secret = invocation_state.get("signing_secret")
    if signing_secret is None:
        signing_secret = _get_signing_secret()
        invocation_state["signing_secret"] = signing_secret

    if not signing_secret:
        logger.error(
            "Signing secret parameter %s is empty; refusing to process events.",
            SLACK_SIGNING_SECRET_PARAM,
        )
        raise ValueError("signing secret is empty")

    if not signature.startswith("v0="):
        logger.warning("Slack signature missing required v0= prefix: %r", signature[:10])
        return False

    sig_basestring = f"v0:{timestamp}:{raw_body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


# --- Dispatch ----------------------------------------------------------------


def dispatch(agent_id: str, trigger_type: str, instruction: str, context: dict, sender: str) -> None:
    """Forward a resolved event to the Dispatch Router Lambda (fire-and-forget)."""
    payload = {
        "source": "slack",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s (%s) from Slack", agent_id, trigger_type)
    lambda_client.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async — don't block the webhook response
        Payload=json.dumps(payload).encode(),
    )


# --- Event Processors --------------------------------------------------------


def _process_app_mention(event: dict) -> dict:
    """Handle a Slack app_mention event.

    Strips the leading <@BOT_ID> prefix Slack prepends to mention text,
    resolves the target agent from the first @agent mention, and dispatches
    asynchronously.

    sender = event.user, the Slack user ID (U-prefixed). User IDs are
    immutable and cannot be changed by the user — matching on them in the
    authorization allowlist is safe. Display names and real names are
    self-editable and non-unique; using them here would be an identity bypass
    (threat T-4 analogue for Slack, same principle as Asana GID vs. display
    name).
    """
    raw_text = event.get("text", "")
    # Strip the leading <@BOT_ID> prefix Slack inserts for app_mention events
    text = _strip_bot_mention(raw_text)

    agent_id, instruction = _resolve_mention(text)
    if not agent_id:
        logger.info("app_mention contained no recognized @agent mention — ignoring")
        return {"statusCode": 200, "body": "ok"}

    channel_id = event.get("channel", "")
    # thread_ts: if the mention is already in a thread, continue that thread;
    # otherwise anchor a new thread to this message's ts.
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    sender = event.get("user", "")  # immutable Slack user ID — see T-4 comment above
    team_id = event.get("team", "")

    if not instruction:
        instruction = "You were mentioned in a Slack message. Review the context and determine what action to take."

    context = {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "team_id": team_id,
        "user_id": sender,
        "message_text": raw_text,
    }

    dispatch(
        agent_id=agent_id,
        trigger_type="mention",
        instruction=instruction,
        context=context,
        sender=sender,
    )
    return {"statusCode": 200, "body": "ok"}


def _process_slash_command(form_data: dict) -> dict:
    """Handle a Slack slash command (e.g. /workitems <text>).

    Slack times out slash commands after ~3 seconds. We respond immediately
    with an ephemeral ack (visible only to the invoking user) and fire an
    async Lambda invoke to the Dispatch Router — the actual agent work is
    entirely non-blocking from Slack's perspective.

    agent_id is derived from the command name: "/workitems" → "workitems".
    """
    command = form_data.get("command", [""])[0].lstrip("/").lower()
    text = form_data.get("text", [""])[0].strip()
    channel_id = form_data.get("channel_id", [""])[0]
    user_id = form_data.get("user_id", [""])[0]  # immutable Slack user ID (T-4 analogue)
    team_id = form_data.get("team_id", [""])[0]
    # thread_ts is present when the slash command is used inside a thread
    thread_ts = form_data.get("thread_ts", [""])[0]

    if command not in SLASH_COMMAND_AGENTS:
        logger.info("Unknown slash command: /%s", command)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "response_type": "ephemeral",
                "text": (
                    f"Unknown command `/{command}`. "
                    f"Available: {', '.join(f'`/{a}`' for a in sorted(SLASH_COMMAND_AGENTS))}"
                ),
            }),
        }

    agent_id = command
    instruction = text or f"Slash command `/{command}` invoked with no arguments. Use your judgment."

    context = {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "team_id": team_id,
        "user_id": user_id,
        "message_text": text,
    }

    # Build ack before dispatching so the response is always correct even if
    # the invoke call raises.
    ack = {
        "response_type": "ephemeral",
        "text": f"On it! Dispatching `@{agent_id}` — I'll post results in this channel.",
    }

    try:
        dispatch(
            agent_id=agent_id,
            trigger_type="slash_command",
            instruction=instruction,
            context=context,
            sender=user_id,
        )
    except Exception as exc:
        logger.exception("Failed to dispatch slash command /%s", command)
        ack["text"] = f"Failed to dispatch `/{command}`: {exc}. Please try again."

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(ack),
    }


# --- Lambda Handler ----------------------------------------------------------


def handler(event, context):
    """Slack Webhook Receiver Lambda entry point.

    Fronted by API Gateway. Handles two routes:
    - POST /slack/events   — Events API (app_mention) + URL verification
    - POST /slack/commands — Slash commands (/workitems, /docwriter, etc.)

    Secrets and derived state live on `invocation_state`, not module globals.
    The dict goes out of scope when the handler returns (threat T-8 analogue).
    """
    invocation_state: dict = {}

    headers = event.get("headers", {}) or {}
    headers = {k.lower(): v for k, v in headers.items()}
    raw_body = event.get("body", "") or ""
    path = event.get("path", event.get("rawPath", ""))

    # --- Signature verification ----------------------------------------------
    # Runs first on every request — including url_verification challenges.
    # Slack signs challenge requests with the same HMAC-SHA256 scheme, so
    # verifying before echoing the challenge is both safe and required.
    # Hard-fail on a missing or empty signing secret (503) so a misconfigured
    # stage never silently processes unsigned events (see the hard-fail block
    # in asana_webhook.py).
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    if not timestamp or not signature:
        logger.warning("Missing Slack signature headers")
        return {"statusCode": 401, "body": "missing signature headers"}

    try:
        valid = _verify_slack_signature(raw_body, timestamp, signature, invocation_state)
    except Exception as exc:
        # Hard-fail on a misconfigured (missing/empty) signing secret so a
        # stage with no secret can never silently process events.
        logger.error("Signature verification error: %s", exc)
        return {"statusCode": 503, "body": "signing secret not configured"}

    if not valid:
        logger.warning("Invalid Slack signature")
        return {"statusCode": 401, "body": "invalid signature"}

    # --- Route by path -------------------------------------------------------
    if path.rstrip("/").endswith("/slack/commands"):
        try:
            form_data = parse_qs(raw_body)
        except Exception:
            return {"statusCode": 400, "body": "invalid form body"}
        return _process_slash_command(form_data)

    # Default path: /slack/events (Events API)
    try:
        body_json = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except (json.JSONDecodeError, TypeError):
        body_json = {}

    if not isinstance(body_json, dict):
        return {"statusCode": 400, "body": "invalid JSON"}

    # --- URL verification (Slack challenge) ----------------------------------
    # Slack sends this when the operator saves the endpoint URL in the app
    # config. The challenge is signed with the same HMAC-SHA256 scheme, so
    # signature verification above already validated the request.
    if body_json.get("type") == "url_verification":
        challenge = body_json.get("challenge", "")
        logger.info("Slack URL verification challenge received")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": challenge}),
        }

    event_type = body_json.get("type")

    if event_type == "event_callback":
        inner = body_json.get("event", {})
        inner_type = inner.get("type", "")
        if inner_type == "app_mention":
            return _process_app_mention(inner)
        logger.info("Unhandled Slack event type: %s", inner_type)
        return {"statusCode": 200, "body": "ok"}

    logger.info("Unrecognized Slack payload type: %s", event_type)
    return {"statusCode": 200, "body": "ok"}
