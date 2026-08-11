"""Slack Webhook Receiver Lambda.

The Slack trigger source, at parity with the GitHub App + Asana receivers. Sits
behind API Gateway (public HTTPS) on two routes:

  - POST /slack/events   — the Events API: ``url_verification`` handshake +
    ``app_mention`` events. This is the PRIMARY dispatch UX:
    ``@sdlc-agents <agent> <message>`` — the agent name resolves bare (no
    second @) or as ``@agent``, and the reply threads under the mention.
  - POST /slack/commands — slash commands: ``/sdlc-message-agent`` (guided
    modal, secondary path) and ``/sdlc-onboard-channel [agent …]`` (a CHANNEL
    ONBOARDING REQUEST an admin approves in the Connectors panel — never
    self-served).

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
import re
import time
from urllib.parse import parse_qs

import boto3
import mentions
import reply
import slack_modals
import slack_notify
import trigger_grants

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
REGISTRY_PARAM = os.environ.get("REGISTRY_PARAM", "/dispatch/agents")
STAGE = os.environ.get("STAGE", "dev")
# Slash command that opens the channel-onboarding request modal (leading slash
# stripped by Slack; we match on the bare name).
ONBOARD_COMMAND = os.environ.get("SLACK_ONBOARD_COMMAND", "sdlc-onboard-channel")
# Slash command that opens the interactive notification-config modal (spec §18).
NOTIFY_COMMAND = os.environ.get("SLACK_NOTIFY_COMMAND", "sdlc-notify")
# Slash command that opens the message-an-agent modal (spec §19) — replaces the
# "/fleet @agent …" form, whose @ collided with Slack's user tagging.
MESSAGE_COMMAND = os.environ.get("SLACK_MESSAGE_COMMAND", "sdlc-message-agent")
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
    except Exception:
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
    except Exception:
        logger.exception("event dedup write failed for %s", event_id)


def _dispatch(
    agent_id: str,
    instruction: str,
    sender: str,
    context: dict,
    trigger_type: str,
    parent_assignment_id: str = "",
):
    payload = {
        "source": "slack",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    if parent_assignment_id:
        # D8 (durable-repo-work spec): a reply on a completed thread starts a
        # NEW assignment linked to the prior one.
        payload["parent_assignment_id"] = parent_assignment_id
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


def _sender_identity_context(team_id: str, user_id: str) -> dict:
    """Context keys that name the human behind a dispatch, resolved from Slack's
    authenticated directory (``users.info``). The verified display name + email
    let the router's identity map store a friendly name and use email as the
    golden join id across sources (§16.5). ``requester_email_verified`` is set
    because the source IS the platform directory (T-42) — never a user-editable
    field. Best-effort: an empty profile just omits the keys, and the router
    falls back to the raw handle. Only fetched when we have a user to look up."""
    if not user_id:
        return {}
    profile = reply.slack_user_profile(team_id, user_id)
    ctx: dict = {}
    if profile.get("display_name"):
        ctx["sender_name"] = profile["display_name"]
    if profile.get("email"):
        ctx["requester_email"] = profile["email"]
        ctx["requester_email_verified"] = True
    return ctx


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


def _resolve_agent_from_text(text: str) -> tuple[str, str] | None:
    """Resolve the agent from text after the bot mention is stripped.

    Tries (in order):
      1. Standard ``@agent rest of message`` (existing path).
      2. Bare first word matching an agent id or alias — so users can type
         ``@sdlc-agents researcher do X`` without a second ``@``.

    Returns ``(agent_id, instruction)`` or None."""
    resolved = _registry.resolve_mention(text)
    if resolved:
        return resolved
    parts = (text or "").split(None, 1)
    if not parts:
        return None
    first_word = parts[0].lower().lstrip("@")
    registry = _registry.load() or {}
    agents = registry.get("agents", {})
    agent_id = None
    if first_word in agents:
        agent_id = first_word
    else:
        agent_id = next(
            (aid for aid, cfg in agents.items() if first_word in (cfg.get("aliases") or [])),
            None,
        )
    if agent_id:
        instruction = parts[1].strip() if len(parts) > 1 else ""
        return agent_id, instruction
    return None


def _agent_has_github_access(agent_id: str) -> bool:
    """Whether ``agent_id`` can actually reach GitHub through the gateway.

    Attaching channel repo scope to an agent with NO GitHub tier (e.g.
    researcher, Asana-only by design) is worse than useless: the dispatch
    instruction promises repo access, the model calls the GitHub tools, and
    the interceptor rejects every call fail-closed (no dispatch origin) —
    killing the MCP session mid-run and failing the whole request. The
    broker's permission-tier table is the source of truth for which built-ins
    hold ANY GitHub permission; custom agents (not in the table) default to
    scoped-in since the vendor/broker give them a read tier."""
    from scm_broker import AGENT_GITHUB_PERMISSIONS

    tier = AGENT_GITHUB_PERMISSIONS.get(agent_id)
    if tier is None:
        return True  # custom agent — broker/vendor default it to a read tier
    return bool(tier)


def _mention_repo_scope(
    team_id: str, channel_id: str, instruction: str, agent_id: str = ""
) -> tuple[str, list[str]]:
    """The repo scope for a mention dispatch: ``(origin_repo, repos)``.

    Slack mentions carry no repo the way a GitHub mention does, so the agent
    would have NO codebase bridge — the gateway interceptor fails closed on a
    missing dispatch origin and every GitHub tool call is refused. The channel's
    APPROVED repo grants (the same set the /sdlc-message-agent modal offers as
    checkboxes) are the natural scope for a mention from that channel:

      - a repo explicitly named in the message (``owner/repo``) AND approved for
        the channel becomes the dispatch origin — the user said which codebase
        they mean, honor it;
      - otherwise the channel's approved repos are attached wholesale, first as
        origin (deterministic: grant order), siblings reachable via co-repo
        grouping.

    An unapproved repo named in the message is deliberately NOT honored — the
    channel grant is the authorization boundary, mentioning a repo must not
    widen it. No approved repos — or an agent with no GitHub access at all
    (``_agent_has_github_access``) — ⇒ ("", []) and the agent runs repo-less
    (Slack-thread research, Asana work), same as before."""
    if agent_id and not _agent_has_github_access(agent_id):
        return "", []
    approved = trigger_grants.channel_repos(team_id, channel_id)
    if not approved:
        return "", []
    named = re.findall(r"\b([\w.-]+/[\w.-]+)\b", instruction or "")
    approved_fold = {r.casefold(): r for r in approved}
    for candidate in named:
        hit = approved_fold.get(candidate.casefold())
        if hit:
            return hit, list(approved)
    return approved[0], list(approved)


def _thread_binding(team_id: str, channel_id: str, thread_ts: str) -> dict | None:
    """The thread_binding# row for this thread, or None. Written by the router
    on every Slack dispatch (durable-repo-work spec) — binds the thread to its
    assignment + agent so a reply can resume without naming the agent."""
    if not thread_ts:
        return None
    try:
        resp = _assignments_table().get_item(
            Key={"assignment_id": f"thread_binding#{team_id}#{channel_id}#{thread_ts}"}
        )
        return resp.get("Item") or None
    except Exception:
        logger.exception("thread binding read failed")
        return None


def _bound_assignment_status(assignment_id: str) -> str:
    try:
        resp = _assignments_table().get_item(
            Key={"assignment_id": assignment_id},
            ProjectionExpression="#s",
            ExpressionAttributeNames={"#s": "status"},
        )
        return str((resp.get("Item") or {}).get("status", "") or "")
    except Exception:
        logger.exception("bound assignment read failed for %s", assignment_id)
        return ""


def _dispatch_resume(assignment_id: str, reply_text: str, sender: str, context: dict) -> None:
    """Send a resume event to the router (same assignment, the reply is the
    interrupt answer; the router holds the lock + guardrail)."""
    payload = {
        "resume_of": assignment_id,
        "body": reply_text,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching resume of %s", assignment_id)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def _process_app_mention(event_data: dict, team_id: str) -> None:
    """Handle an ``app_mention`` event: resolve the @agent and dispatch.

    Users type ``@sdlc-agents researcher do something`` — the Slack-encoded bot
    mention is stripped, leaving ``researcher do something``. We resolve
    ``researcher`` (or any alias) against the live registry, then dispatch with
    the thread_ts so the agent's reply threads under the user's message. The
    channel's approved repos are attached as the dispatch's codebase scope
    (see _mention_repo_scope).

    BOUND THREADS (durable-repo-work spec): a mention inside a thread the
    router bound to an assignment doesn't need to name an agent — the binding
    resolves it. A paused (awaiting_input) assignment RESUMES with the reply
    as the interrupt answer; a completed one starts a NEW assignment linked to
    the prior via parent_assignment_id (D8). Any other status falls through to
    normal mention handling."""
    if event_data.get("bot_id") or event_data.get("subtype") == "bot_message":
        return  # bot-loop guard
    text = _strip_bot_mention(event_data.get("text", ""))
    user_id_early = event_data.get("user", "")
    channel_id_early = event_data.get("channel", "")
    thread_ts = event_data.get("thread_ts") or ""
    binding = _thread_binding(team_id, channel_id_early, thread_ts) if thread_ts else None
    if binding and binding.get("bound_assignment_id"):
        bound_id = str(binding["bound_assignment_id"])
        status = _bound_assignment_status(bound_id)
        if status == "awaiting_input":
            context = {
                "workspace": team_id,
                "channel_id": channel_id_early,
                "thread_ts": thread_ts,
                "message_ts": event_data.get("ts"),
                # Channel-membership group so a channel-scoped grant authorizes
                # the resume reply exactly like the original dispatch.
                "principal_groups": _principal_groups(team_id, channel_id_early),
                **_sender_identity_context(team_id, user_id_early),
            }
            _dispatch_resume(bound_id, text, _principal(team_id, user_id_early), context)
            return
        if status == "completed":
            # D8: new linked assignment on the same thread — the binding names
            # the agent, so the reply needn't. Switching agents requires the
            # EXPLICIT @agent form: the bare-first-word resolution the top-level
            # mention path uses would hijack natural replies here, because
            # aliases are common English words ('docs are outdated…' would
            # silently reroute a workitems thread to docwriter with the first
            # word swallowed).
            resolved = _registry.resolve_mention(text) or (
                str(binding.get("agent_id", "")), text
            )
            agent_id, instruction = resolved
            if agent_id:
                instruction = instruction or text
                origin_repo, repos = _mention_repo_scope(
                    team_id, channel_id_early, instruction, agent_id=agent_id
                )
                context = {
                    "workspace": team_id,
                    "channel_id": channel_id_early,
                    "thread_ts": thread_ts,
                    "message_ts": event_data.get("ts"),
                    "repo": origin_repo,
                    "repos": repos,
                    "principal_groups": _principal_groups(team_id, channel_id_early),
                    **_sender_identity_context(team_id, user_id_early),
                }
                _dispatch(
                    agent_id, instruction, _principal(team_id, user_id_early),
                    context, "comment_mention", parent_assignment_id=bound_id,
                )
                return
        # any other status (dispatched/resuming/failed/…) → normal handling

    resolved = _resolve_agent_from_text(text)
    if not resolved:
        return
    agent_id, instruction = resolved
    if not instruction:
        instruction = "You were mentioned in Slack. Review the thread and take appropriate action."
    user_id = event_data.get("user", "")
    channel_id = event_data.get("channel", "")
    origin_repo, repos = _mention_repo_scope(team_id, channel_id, instruction, agent_id=agent_id)
    if repos:
        instruction += (
            "\n\nRepositories approved for this channel (work against these; "
            f"the first is your primary): {', '.join([origin_repo] + [r for r in repos if r != origin_repo])}"
        )
    context = {
        "workspace": team_id,
        "channel_id": channel_id,
        "thread_ts": event_data.get("thread_ts") or event_data.get("ts"),
        "message_ts": event_data.get("ts"),
        "repo": origin_repo,
        "repos": repos,
        "principal_groups": _principal_groups(team_id, channel_id),
        **_sender_identity_context(team_id, user_id),
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


def _registry_agent_ids() -> list[str]:
    """The routable agent ids from the live registry (for modal dropdowns)."""
    return sorted((_registry.load() or {}).get("agents", {}).keys())


def _handle_slash_command(form: dict, team_id: str) -> dict:
    """Route a slash command. Every command opens an interactive modal
    (``views.open`` on the ~3s trigger_id):

      - ``/sdlc-onboard-channel`` — agent + repo pickers → files a pending
        channel_request an admin approves in the dashboard.
      - ``/sdlc-message-agent``  — agent dropdown + the channel's APPROVED
        repos + a message → dispatches, then posts a visible in-channel
        confirmation. Replaces ``/fleet @agent …`` (@ collided with user tags).
      - ``/sdlc-notify``         — the notification-config modal (spec §18).

    Legacy ``/fleet @agent …`` text dispatch still works when invoked WITH text
    (back-compat during migration), but its empty invocation now points at the
    new command."""
    command = (form.get("command", [""])[0] or "").lstrip("/")
    text = form.get("text", [""])[0] or ""
    user_id = form.get("user_id", [""])[0] or ""
    channel_id = form.get("channel_id", [""])[0] or ""
    channel_name = form.get("channel_name", [""])[0] or ""
    trigger_id = form.get("trigger_id", [""])[0] or ""

    if command == ONBOARD_COMMAND:
        # Back-compat: explicit text args still file directly (scripts/docs).
        if text.strip():
            msg = _record_channel_request(team_id, channel_id, channel_name, user_id, text)
            return _ephemeral(msg)
        view = slack_modals.build_onboard_modal(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            agents=_registry_agent_ids(),
            repos=slack_notify.onboarded_repos(),
        )
        if not slack_notify.open_modal(team_id=team_id, trigger_id=trigger_id, view=view):
            return _ephemeral("Couldn't open the onboarding form — please try again.")
        return _ack()

    if command == MESSAGE_COMMAND:
        agents = _registry_agent_ids()
        if not agents:
            return _ephemeral("No agents are live yet — ask an admin to enable one.")
        view = slack_modals.build_message_modal(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            agents=agents,
            channel_repos=trigger_grants.channel_repos(team_id, channel_id),
        )
        if not slack_notify.open_modal(team_id=team_id, trigger_id=trigger_id, view=view):
            return _ephemeral("Couldn't open the message form — please try again.")
        return _ack()

    if command == NOTIFY_COMMAND:
        # ``/sdlc-notify me`` → the PER-USER DM prefs modal (§A9.2); bare
        # ``/sdlc-notify`` → the channel-subscription modal.
        if text.strip().lower() == "me":
            view = slack_notify.build_pref_modal(team_id=team_id, user_id=user_id)
            if not slack_notify.open_modal(team_id=team_id, trigger_id=trigger_id, view=view):
                return _ephemeral("Couldn't open your DM settings — please try again.")
            return _ack()
        # Open the interactive notification-config modal. The slash-command
        # payload carries a trigger_id (valid ~3s); views.open must use it
        # promptly, so we open here and return an empty 200 (Slack shows the
        # modal, no ephemeral text needed).
        view = slack_notify.build_notify_modal(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            repos=slack_notify.onboarded_repos(),
        )
        if not slack_notify.open_modal(team_id=team_id, trigger_id=trigger_id, view=view):
            return _ephemeral("Couldn't open the notification settings — please try again.")
        return _ack()

    # Legacy mention-style command (/fleet @agent …) — kept for back-compat.
    if not text.strip():
        return _ephemeral(
            "Mention the bot to message an agent: `@sdlc-agents <agent> <message>` "
            f"(threads the reply). Or use `/{MESSAGE_COMMAND}` for a guided form."
        )
    resolved = _registry.resolve_mention(text if text.startswith("@") else f"@{text}")
    if not resolved:
        return _ephemeral(
            f"No known agent in `/{command} {text}`. Try "
            "`@sdlc-agents <agent> <message>` or "
            f"`/{MESSAGE_COMMAND}` for a guided form."
        )
    agent_id, instruction = resolved
    context = {
        "workspace": team_id,
        "channel_id": channel_id,
        "thread_ts": None,
        "message_ts": None,
        "principal_groups": _principal_groups(team_id, channel_id),
        **_sender_identity_context(team_id, user_id),
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


def _view_errors(errors: dict) -> dict:
    """A view_submission response that keeps the modal open with field errors."""
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"response_action": "errors", "errors": errors})}


def _submit_onboard_request(payload: dict, view: dict) -> dict:
    """Persist the channel-onboarding request from the modal submit."""
    parsed = slack_modals.parse_onboard_submission(view)
    user_id = (payload.get("user") or {}).get("id") or ""
    try:
        trigger_grants.put_channel_request(
            team_id=parsed["team_id"],
            channel_id=parsed["channel_id"],
            channel_name=parsed["channel_name"],
            requested_by=_principal(parsed["team_id"], user_id),
            requested_agents=parsed["requested_agents"],
            requested_repos=parsed["requested_repos"],
        )
    except ValueError as exc:
        logger.warning("channel request rejected: %s", exc)
        return _view_errors({"agents": f"Couldn't file the request: {exc}"})
    # Visible confirmation so the channel knows a request is in flight.
    scope = ", ".join(parsed["requested_agents"]) or "any agent"
    repos = ", ".join(parsed["requested_repos"]) or "no repos yet"
    reply.post_slack_message(
        parsed["team_id"], parsed["channel_id"],
        f"📨 <@{user_id}> requested fleet onboarding for this channel "
        f"(agents: {scope} · repos: {repos}). An admin will review it.",
    )
    return _ack()  # close the modal


def _submit_message_agent(payload: dict, view: dict) -> dict:
    """Dispatch the message-agent modal submit, then post a VISIBLE in-channel
    confirmation (the whole point of the guided form — the channel sees work
    was kicked off, unlike the old ephemeral-only /fleet)."""
    parsed = slack_modals.parse_message_submission(view)
    team_id, channel_id = parsed["team_id"], parsed["channel_id"]
    user_id = (payload.get("user") or {}).get("id") or ""
    if not parsed["agent_id"]:
        return _view_errors({"agent": "Pick an agent."})
    if not parsed["message"]:
        return _view_errors({"message": "Enter a message for the agent."})

    # Re-verify the repo scope against the LIVE channel grant. The modal only
    # OFFERED approved repos, but the grant may have been revoked between open
    # and submit — enforce at the trust boundary, not just the UI.
    approved = set(trigger_grants.channel_repos(team_id, channel_id))
    stale = [r for r in parsed["repos"] if r not in approved]
    if stale:
        return _view_errors({
            "repos": f"No longer approved for this channel: {', '.join(stale)}. "
            "Re-open the form to refresh.",
        })

    repos = parsed["repos"]
    instruction = parsed["message"]
    if repos:
        instruction += "\n\nWork against these repositories (approved for this channel): " + ", ".join(repos)

    # Post the visible confirmation FIRST and capture its ts — it becomes the
    # THREAD ANCHOR for the whole run. The router's "on it" ack and the final
    # result all thread under this one message instead of piling up as separate
    # top-level channel posts. (A modal submit has no originating message to
    # thread under, so we make one.)
    scope = f" · repos: {', '.join(repos)}" if repos else ""
    preview = parsed["message"][:200] + ("…" if len(parsed["message"]) > 200 else "")
    _ok, anchor_ts = reply.post_slack_message_ts(
        team_id, channel_id,
        f"🤖 <@{user_id}> sent a message to *{parsed['agent_id']}*{scope}:\n> {preview}",
        agent_id=parsed["agent_id"],
    )
    context = {
        "workspace": team_id,
        "channel_id": channel_id,
        "thread_ts": anchor_ts,
        "message_ts": anchor_ts,
        # The FIRST selected repo becomes the dispatch origin — the anchor the
        # gateway's co-repo grouping enforces reach from (siblings grouped with
        # it are reachable; unrelated repos are not).
        "repo": repos[0] if repos else "",
        "repos": repos,
        "principal_groups": _principal_groups(team_id, channel_id),
        **_sender_identity_context(team_id, user_id),
    }
    _dispatch(
        parsed["agent_id"], instruction, _principal(team_id, user_id),
        context, "slash_command",
    )
    return _ack()  # close the modal


def _handle_interaction(form: dict) -> dict:
    """Route a Slack interactivity payload. Slack sends a single ``payload`` form
    field holding url-encoded JSON. We handle our three modal submits
    (``view_submission`` with our callback_ids); a view_submission must return
    200 with an empty body to close the modal (or response_action:errors to keep
    it open with field errors)."""
    raw = form.get("payload", [""])[0] or ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return _ack()
    if payload.get("type") != "view_submission":
        return _ack()  # button clicks etc. — no-op for now
    view = payload.get("view", {}) or {}
    callback = view.get("callback_id")
    # Re-check the workspace is still onboarded before acting on any submit.
    team_id = (payload.get("team") or {}).get("id") or ""
    if not trigger_grants.is_workspace_enabled(team_id):
        return _view_errors({"agents": "This workspace isn't onboarded for the fleet."})
    if callback == slack_modals.ONBOARD_VIEW_CALLBACK:
        return _submit_onboard_request(payload, view)
    if callback == slack_modals.MESSAGE_VIEW_CALLBACK:
        return _submit_message_agent(payload, view)
    if callback == slack_notify.NOTIFY_VIEW_CALLBACK:
        config = slack_notify.parse_view_submission(view)
        slack_notify.save_subscription(config)
        return _ack()  # empty 200 closes the modal
    if callback == slack_notify.NOTIFY_PREF_VIEW_CALLBACK:
        config = slack_notify.parse_pref_submission(view)
        if not slack_notify.save_pref(config):
            return _view_errors({"tier_actionable":
                                 "You must be onboarded with a verified Slack "
                                 "identity to receive DMs — ask an admin."})
        return _ack()
    return _ack()


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
        except Exception:
            logger.exception("error handling Slack slash command")
            return _ephemeral("Something went wrong handling that command.")

    # --- interactivity (Block Kit actions + modal submits, form-encoded) ---
    # Interactions POST a `payload=<url-encoded-json>` form field. The only
    # interaction we handle today is the /sdlc-notify modal submit (view_submission
    # with our callback_id); anything else is acknowledged as a no-op.
    if is_interaction:
        try:
            return _handle_interaction(parse_qs(raw_body))
        except Exception:
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
    except Exception:
        logger.exception("error processing Slack event")
        # Do NOT mark seen — let Slack retry the delivery.
        return {"statusCode": 500, "body": "processing error"}
    # Processed cleanly — now record the id so a Slack retry is a no-op.
    _mark_seen(event_id)
    return _ack("ok")
