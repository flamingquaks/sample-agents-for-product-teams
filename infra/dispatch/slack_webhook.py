"""Slack Webhook Receiver Lambda.

The Slack trigger source, at parity with the GitHub App + Asana receivers. Sits
behind API Gateway (public HTTPS) on two routes:

  - POST /slack/events   — the Events API: ``url_verification`` handshake +
    ``app_mention`` events ("@fleetbot @workitems break this up").
  - POST /slack/commands — slash commands: ``/fleet <@agent> …`` (mention
    dispatch) and ``/onboard-channel [agent …]`` (a CHANNEL ONBOARDING REQUEST
    that an admin approves in the Connectors panel — access is never self-served).

Multi-workspace: every delivery carries a ``team_id``; we resolve it to an
onboarded, enabled ``slack_workspace`` row and verify the signature against THAT
workspace's signing secret (each workspace has its own). Unknown/disabled
workspace ⇒ no dispatch.

Security model mirrors the other receivers:
  - Every request is authenticated by its Slack ``v0`` signature over
    ``v0:{timestamp}:{raw_body}`` (mentions.verify_slack_signature), with a
    ±5-min replay window. Missing/empty secret hard-fails closed.
  - Secrets are fetched per-invocation, never a module global (T-8/T-36).
  - ``event_id`` de-duplication: Slack retries deliveries, so we drop a repeat.
  - Bot-loop guard: events authored by a bot / our own bot user are ignored, so
    the agent's own reply never re-triggers it.

Returns 200 within Slack's ~3s budget: verify → dedup → async-invoke the router
(or write a channel request) → return. The user-visible "on it" ack is posted by
the router, not here, so a slow chat.postMessage can't blow the budget.
"""

import base64
import json
import logging
import os
import time
from urllib.parse import parse_qs

import boto3

import mentions
import reply
import trigger_grants

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
REGISTRY_PARAM = os.environ.get("REGISTRY_PARAM", "/dispatch/agents")
STAGE = os.environ.get("STAGE", "dev")
# Slash command that files a channel-onboarding REQUEST (leading slash stripped
# by Slack; we match on the bare name).
ONBOARD_COMMAND = os.environ.get("SLACK_ONBOARD_COMMAND", "onboard-channel")
# How long to remember an event_id for de-duplication.
_DEDUP_TTL_SECONDS = 24 * 60 * 60

_ssm = boto3.client("ssm")
_lambda = boto3.client("lambda")
_ddb = boto3.resource("dynamodb")

# Registry-backed @mention resolution (shared with the other receivers): the
# live registry decides which agents are reachable, so a UI-onboarded agent
# resolves here with no code change.
_registry = mentions.RegistryCache(REGISTRY_PARAM, lambda: _ssm)


def _assignments_table():
    return _ddb.Table(os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments"))


def _signing_secret(team_id: str) -> str | None:
    param = f"/sdlc-agents/{STAGE}/slack/{team_id}/signing-secret"
    try:
        resp = _ssm.get_parameter(Name=param, WithDecryption=True)
    except _ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"] or None


def _seen_event(event_id: str) -> bool:
    """Record ``event_id``; return True if it was ALREADY seen (a Slack retry).

    Conditional PutItem on the assignments table with a short TTL — idempotent
    dedup without a second table. A brand-new id writes and returns False; a
    repeat hits the condition and returns True."""
    if not event_id:
        return False
    now = int(time.time())
    try:
        _assignments_table().put_item(
            Item={
                "assignment_id": f"slack-event#{event_id}",
                "kind": "slack_event_dedup",
                "ttl": now + _DEDUP_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(assignment_id)",
        )
        return False
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return True
    except Exception:  # noqa: BLE001
        # A dedup-store hiccup must not drop a real delivery; fail OPEN on dedup
        # only (worst case a duplicate dispatch, which the router's assignment id
        # + concurrency guard already tolerate).
        logger.exception("event dedup check failed for %s; treating as new", event_id)
        return False


def _dispatch(agent_id: str, instruction: str, sender: str, context: dict, trigger_type: str):
    payload = {
        "source": "slack",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s: slack/%s", agent_id, trigger_type)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async — don't block the webhook response
        Payload=json.dumps(payload).encode(),
    )


def _strip_bot_mention(text: str) -> str:
    """Drop a leading ``<@U0BOT>`` Slack mention so the remaining text can be
    resolved against the agent registry exactly like the other sources."""
    import re

    return re.sub(r"^\s*<@[\w]+>\s*", "", text or "").strip()


def _principal(team_id: str, user_id: str) -> str:
    """The workspace-scoped, immutable sender principal (T-4)."""
    return f"slack:{team_id}:{user_id}"


def _principal_groups(team_id: str, channel_id: str) -> list[str]:
    """Implicit groups the sender belongs to for authz. The channel itself is a
    group (``channel:<team>:<channel>``) so a channel-scoped grant — the one an
    admin creates when approving a channel-onboarding request — permits anyone
    triggering FROM that channel, without enumerating users. (Real Slack
    usergroups can be added here as a fast-follow.)"""
    groups = []
    if channel_id:
        groups.append(f"channel:{team_id}:{channel_id}")
    return groups


# --- channel onboarding request ---------------------------------------------


def _record_channel_request(
    team_id: str, channel_id: str, channel_name: str, user_id: str, text: str
) -> str:
    """Persist a pending channel-onboarding request from a slash command. The
    remaining command text is parsed as an optional space/comma-separated list of
    requested agent ids (scope); empty ⇒ any agent. Returns a user-facing message
    to show in the (ephemeral) slash-command response."""
    raw = (text or "").replace(",", " ").split()
    requested_agents = [a.lstrip("@").strip().lower() for a in raw if a.strip()]
    try:
        trigger_grants.put_channel_request(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            requested_by=_principal(team_id, user_id),
            requested_agents=requested_agents,
        )
    except ValueError as exc:
        logger.warning("channel request rejected: %s", exc)
        return f"Couldn't file that request: {exc}"
    scope = ", ".join(requested_agents) if requested_agents else "all agents"
    return (
        f"📨 Request filed to onboard this channel for *{scope}*. "
        "An admin will review it in the fleet dashboard."
    )


# --- event processing --------------------------------------------------------


def _process_app_mention(event_data: dict, team_id: str) -> None:
    """Handle an ``app_mention`` event: resolve the @agent and dispatch."""
    if event_data.get("bot_id") or event_data.get("subtype") == "bot_message":
        return  # bot-loop guard
    text = _strip_bot_mention(event_data.get("text", ""))
    resolved = _registry.resolve_mention(text)
    if not resolved:
        return
    agent_id, instruction = resolved
    if not instruction:
        instruction = "You were mentioned in Slack. Review the thread and take appropriate action."
    user_id = event_data.get("user", "")
    channel_id = event_data.get("channel", "")
    context = {
        "workspace": team_id,
        "channel_id": channel_id,
        "thread_ts": event_data.get("thread_ts") or event_data.get("ts"),
        "message_ts": event_data.get("ts"),
        "principal_groups": _principal_groups(team_id, channel_id),
    }
    _dispatch(agent_id, instruction, _principal(team_id, user_id), context, "comment_mention")


def _ephemeral(text: str) -> dict:
    """A Slack ephemeral (visible only to the invoking user) response body."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "text": text}),
    }


def _ack(text: str = "") -> dict:
    return {"statusCode": 200, "body": text}


def _handle_slash_command(form: dict, team_id: str) -> dict:
    """Route a slash command. ``/onboard-channel`` files a request; any other
    configured command carries an @mention we dispatch. Returns the HTTP response
    (ephemeral so only the invoking user sees it)."""
    command = (form.get("command", [""])[0] or "").lstrip("/")
    text = form.get("text", [""])[0] or ""
    user_id = form.get("user_id", [""])[0] or ""
    channel_id = form.get("channel_id", [""])[0] or ""
    channel_name = form.get("channel_name", [""])[0] or ""

    if command == ONBOARD_COMMAND:
        msg = _record_channel_request(team_id, channel_id, channel_name, user_id, text)
        return _ephemeral(msg)

    # A mention-style command: resolve the agent from the text.
    resolved = _registry.resolve_mention(text if text.startswith("@") else f"@{text}")
    if not resolved:
        return _ephemeral(
            f"No known agent in `/{command} {text}`. Mention an agent, e.g. "
            f"`/{command} @workitems break this up`."
        )
    agent_id, instruction = resolved
    context = {
        "workspace": team_id,
        "channel_id": channel_id,
        "thread_ts": None,
        "message_ts": None,
        "principal_groups": _principal_groups(team_id, channel_id),
    }
    _dispatch(
        agent_id,
        instruction or f"Slash command /{command} from Slack.",
        _principal(team_id, user_id),
        context,
        "slash_command",
    )
    return _ephemeral(f"🏁 Dispatching to `{agent_id}`…")


# --- Lambda handler ----------------------------------------------------------


def _team_id_from_event(payload: dict) -> str:
    """The workspace id from an Events-API envelope."""
    return payload.get("team_id") or (payload.get("team") or {}).get("id") or ""


def handler(event, context=None):
    """API Gateway entry point for both /slack/events and /slack/commands."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("could not base64-decode Slack body")
            return {"statusCode": 400, "body": "invalid body encoding"}

    resource = event.get("resource", "") or event.get("path", "")
    is_command = resource.endswith("/commands") or "command=" in raw_body

    # Parse enough to find the team_id BEFORE verifying (we need the per-workspace
    # secret). Slash commands are form-encoded; events are JSON.
    form = None
    payload = None
    if is_command:
        form = parse_qs(raw_body)
        team_id = (form.get("team_id", [""])[0]) or ""
    else:
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return {"statusCode": 400, "body": "invalid JSON"}
        # url_verification carries no team scope — but it's signed, so verify with
        # the challenge's team if present, else fall through to signature failure.
        team_id = _team_id_from_event(payload)

    # --- authenticate against the workspace's signing secret ---
    secret = _signing_secret(team_id) if team_id else None
    if not secret:
        # url_verification during first setup may arrive before the workspace row
        # exists; without a secret we cannot trust it, so refuse (the operator
        # stores the secret via bootstrap_slack.py before pointing Slack here).
        logger.error("no signing secret for team %r — refusing Slack delivery", team_id)
        return {"statusCode": 401, "body": "workspace not configured"}
    if not mentions.verify_slack_signature(
        secret,
        headers.get("x-slack-request-timestamp", ""),
        raw_body,
        headers.get("x-slack-signature", ""),
    ):
        logger.warning("invalid Slack signature for team %s", team_id)
        return {"statusCode": 401, "body": "invalid signature"}

    # --- workspace must be onboarded + enabled ---
    if not trigger_grants.is_workspace_enabled(team_id):
        logger.info("Slack delivery from non-onboarded/disabled workspace %s; ignoring", team_id)
        return _ack("ignored")

    # --- slash commands ---
    if is_command:
        try:
            return _handle_slash_command(form, team_id)
        except Exception:  # noqa: BLE001
            logger.exception("error handling Slack slash command")
            return _ephemeral("Something went wrong handling that command.")

    # --- events API ---
    ptype = payload.get("type")
    if ptype == "url_verification":
        return {"statusCode": 200, "body": payload.get("challenge", "")}
    if ptype != "event_callback":
        return _ack("ignored")

    # De-dup Slack retries on the delivery's event_id.
    if _seen_event(payload.get("event_id", "")):
        return _ack("duplicate")

    event_data = payload.get("event", {}) or {}
    try:
        if event_data.get("type") == "app_mention":
            _process_app_mention(event_data, team_id)
    except Exception:  # noqa: BLE001
        logger.exception("error processing Slack event")
        return {"statusCode": 500, "body": "processing error"}
    return _ack("ok")
