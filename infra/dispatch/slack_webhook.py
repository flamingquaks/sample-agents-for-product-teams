"""Slack Webhook Receiver Lambda.

The Slack trigger source, at parity with the GitHub App + Asana receivers. Sits
behind API Gateway (public HTTPS) on two routes:

  - POST /slack/events   — the Events API: ``url_verification`` handshake +
    ``app_mention`` events ("@fleetbot @workitems break this up").
  - POST /slack/commands — slash commands: ``/fleet <@agent> …`` (mention
    dispatch) and ``/sdlc-onboard-channel [agent …]`` (a CHANNEL ONBOARDING
    REQUEST an admin approves in the Connectors panel — never self-served).

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
import slack_notify
import trigger_grants

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
REGISTRY_PARAM = os.environ.get("REGISTRY_PARAM", "/dispatch/agents")
STAGE = os.environ.get("STAGE", "dev")
# Slash command that files a channel-onboarding REQUEST (leading slash stripped
# by Slack; we match on the bare name).
ONBOARD_COMMAND = os.environ.get("SLACK_ONBOARD_COMMAND", "sdlc-onboard-channel")
# Slash command that opens the interactive notification-config modal (spec §18).
NOTIFY_COMMAND = os.environ.get("SLACK_NOTIFY_COMMAND", "sdlc-notify")
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


def _signing_secret() -> str | None:
    """The Slack app's signing secret. This is APP-LEVEL (one per Slack app),
    NOT per-workspace — only bot tokens are per-installation. It also must be
    resolvable without a team id, because the ``url_verification`` handshake
    carries no team scope. Stored at /sdlc-agents/<stage>/slack/signing-secret
    by bootstrap_slack.py."""
    param = f"/sdlc-agents/{STAGE}/slack/signing-secret"
    try:
        resp = _ssm.get_parameter(Name=param, WithDecryption=True)
    except _ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"] or None


def _dedup_key(event_id: str) -> str:
    return f"slack-event#{event_id}"


def _already_seen(event_id: str) -> bool:
    """True if ``event_id`` was already recorded as processed (a Slack retry of a
    delivery we handled). Read-only — the id is recorded by ``_mark_seen`` AFTER
    successful processing, so a delivery that failed mid-process is NOT marked and
    Slack's retry is allowed through. A read error fails OPEN (treat as new): a
    duplicate dispatch is tolerated by the router's assignment id + concurrency
    guard, but dropping a real mention is not."""
    if not event_id:
        return False
    try:
        resp = _assignments_table().get_item(Key={"assignment_id": _dedup_key(event_id)})
        return "Item" in resp
    except Exception:  # noqa: BLE001
        logger.exception("event dedup read failed for %s; treating as new", event_id)
        return False


def _mark_seen(event_id: str) -> None:
    """Record ``event_id`` as processed (short TTL). Best-effort — a write failure
    only risks a duplicate dispatch on a Slack retry, never a dropped delivery."""
    if not event_id:
        return
    try:
        _assignments_table().put_item(
            Item={
                "assignment_id": _dedup_key(event_id),
                "kind": "slack_event_dedup",
                "ttl": int(time.time()) + _DEDUP_TTL_SECONDS,
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("event dedup write failed for %s", event_id)


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
    """Route a slash command. ``/sdlc-onboard-channel`` files a request; any other
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

    if command == NOTIFY_COMMAND:
        # Open the interactive notification-config modal. The slash-command
        # payload carries a trigger_id (valid ~3s); views.open must use it
        # promptly, so we open here and return an empty 200 (Slack shows the
        # modal, no ephemeral text needed).
        trigger_id = form.get("trigger_id", [""])[0] or ""
        view = slack_notify.build_notify_modal(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            repos=slack_notify.onboarded_repos(),
        )
        if not slack_notify.open_modal(team_id=team_id, trigger_id=trigger_id, view=view):
            return _ephemeral("Couldn't open the notification settings — please try again.")
        return _ack()

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


# --- interactivity (Block Kit / modal submits) -------------------------------


def _handle_interaction(form: dict) -> dict:
    """Route a Slack interactivity payload. Slack sends a single ``payload`` form
    field holding url-encoded JSON. We handle the /sdlc-notify modal submit
    (``view_submission`` with our callback_id) and save the subscription; a
    view_submission must return 200 with an empty body to close the modal."""
    raw = form.get("payload", [""])[0] or ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return _ack()
    if payload.get("type") != "view_submission":
        return _ack()  # button clicks etc. — no-op for now
    view = payload.get("view", {}) or {}
    if view.get("callback_id") != slack_notify.NOTIFY_VIEW_CALLBACK:
        return _ack()
    # Re-check the workspace is still onboarded before persisting.
    team_id = (payload.get("team") or {}).get("id") or ""
    if not trigger_grants.is_workspace_enabled(team_id):
        return {"statusCode": 200, "body": json.dumps({
            "response_action": "errors",
            "errors": {"repos": "This workspace isn't onboarded for the fleet."},
        })}
    config = slack_notify.parse_view_submission(view)
    slack_notify.save_subscription(config)
    return _ack()  # empty 200 closes the modal


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

    # Route by the API-Gateway resource/path ONLY. A content sniff like
    # "command=" in raw_body would misclassify a JSON app_mention whose text
    # happens to contain that substring, parse_qs it, and drop the mention.
    resource = event.get("resource", "") or event.get("path", "")
    is_command = resource.endswith("/commands")
    is_interaction = resource.endswith("/interactions")

    # --- authenticate FIRST, against the app-level signing secret ---
    # The signing secret is per-APP, not per-workspace (only bot tokens are
    # per-installation), and the ``url_verification`` handshake carries no team
    # scope — so verification must NOT depend on a team id. Verify over the exact
    # raw body, then parse.
    secret = _signing_secret()
    if not secret:
        logger.error("Slack signing secret not configured — refusing delivery")
        return {"statusCode": 503, "body": "slack not configured"}
    if not mentions.verify_slack_signature(
        secret,
        headers.get("x-slack-request-timestamp", ""),
        raw_body,
        headers.get("x-slack-signature", ""),
    ):
        logger.warning("invalid Slack signature")
        return {"statusCode": 401, "body": "invalid signature"}

    # --- slash commands (form-encoded) ---
    if is_command:
        form = parse_qs(raw_body)
        team_id = (form.get("team_id", [""])[0]) or ""
        if not trigger_grants.is_workspace_enabled(team_id):
            logger.info("slash command from non-onboarded/disabled workspace %s", team_id)
            return _ephemeral("This workspace isn't onboarded for the fleet yet.")
        try:
            return _handle_slash_command(form, team_id)
        except Exception:  # noqa: BLE001
            logger.exception("error handling Slack slash command")
            return _ephemeral("Something went wrong handling that command.")

    # --- interactivity (Block Kit actions + modal submits, form-encoded) ---
    # Interactions POST a `payload=<url-encoded-json>` form field. The only
    # interaction we handle today is the /sdlc-notify modal submit (view_submission
    # with our callback_id); anything else is acknowledged as a no-op.
    if is_interaction:
        try:
            return _handle_interaction(parse_qs(raw_body))
        except Exception:  # noqa: BLE001
            logger.exception("error handling Slack interaction")
            # A view_submission expects a 200 (empty body closes the modal).
            return _ack()

    # --- events API (JSON) ---
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}
    ptype = payload.get("type")
    # The verification handshake is signed but carries no team scope — answer it
    # as soon as the signature is verified (before any workspace gate).
    if ptype == "url_verification":
        return {"statusCode": 200, "body": payload.get("challenge", "")}
    if ptype != "event_callback":
        return _ack("ignored")

    # Now that it's a real event, the workspace must be onboarded + enabled.
    team_id = _team_id_from_event(payload)
    if not trigger_grants.is_workspace_enabled(team_id):
        logger.info("Slack event from non-onboarded/disabled workspace %s; ignoring", team_id)
        return _ack("ignored")

    # De-dup Slack retries on the delivery's event_id. Check-only here (no write
    # yet): a duplicate short-circuits, but we must NOT record the id until the
    # event actually processed — otherwise a transient dispatch failure (which
    # returns 500 and asks Slack to retry) would be swallowed by its own marker
    # on the retry and the mention silently lost.
    event_id = payload.get("event_id", "")
    if _already_seen(event_id):
        return _ack("duplicate")

    event_data = payload.get("event", {}) or {}
    try:
        if event_data.get("type") == "app_mention":
            _process_app_mention(event_data, team_id)
    except Exception:  # noqa: BLE001
        logger.exception("error processing Slack event")
        # Do NOT mark seen — let Slack retry the delivery.
        return {"statusCode": 500, "body": "processing error"}
    # Processed cleanly — now record the id so a Slack retry is a no-op.
    _mark_seen(event_id)
    return _ack("ok")
