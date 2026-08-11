"""Notification fan-out for the fleet (spec §18).

Turns a fleet or SCM event into threaded Slack messages to the channels that
subscribed to it. A subscription (``notif_sub`` row, written by the admin/Slack
side) declares WHICH repos and WHICH tiered events a channel wants; this module
is the read + deliver path the Dispatch Router (fleet events) and the GitHub
webhook (SCM events) call.

Design (spec §18.4):
  - **Tiers.** ``actionable`` (a human should engage), ``informative`` (FYI), and
    ``error`` (a flow errored). Only ``actionable`` + ``error`` @mention people.
  - **Threading.** All messages for one unit of work (an assignment, or a PR)
    share a ``thread_ts`` keyed on a stable ``thread_key`` so a run's lifecycle
    collapses into one thread instead of N channel posts. The first post in a
    thread has no parent; its ``ts`` is remembered (in the assignments table) and
    reused as the parent for follow-ups.
  - **Mentions resolve through the identity map.** "the PR author" / "the
    requester" becomes ``<@U…>`` for THAT person in THAT workspace via
    identity.slack_handle_for; if unresolved, we degrade to an unmentioned post
    rather than mis-ping (§18.4).

Best-effort throughout: a delivery failure is logged + metered, never raised —
a notification must not break the dispatch or webhook path that emitted it.
"""

import logging
import os
import time

import boto3

import config_query
import identity as identity_map
import reply

logger = logging.getLogger(__name__)

# Mirror config_store notification constants (schema contract).
TIER_ACTIONABLE = "actionable"
TIER_INFORMATIVE = "informative"
TIER_ERROR = "error"
TIERS = (TIER_ACTIONABLE, TIER_INFORMATIVE, TIER_ERROR)
MENTION_TIERS = (TIER_ACTIONABLE, TIER_ERROR)
# Severity ordering for the per-subscription floor (min_severity).
_SEVERITY_RANK = {TIER_INFORMATIVE: 0, TIER_ACTIONABLE: 1, TIER_ERROR: 2}

_NOTIF_SUB_PK_PREFIX = "notif_sub#"
_THREAD_PK_PREFIX = "notif_thread#"


def dashboard_run_url(assignment_id: str) -> str:
    """Deep link to a run's dashboard detail page (SPA hash route ``#/run/<id>``),
    or "" when the dashboard isn't deployed (DASHBOARD_URL unset). Read lazily so
    tests can monkeypatch the env."""
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    if not base or not assignment_id:
        return ""
    return f"{base}/#/run/{assignment_id}"


def run_ref(assignment_id: str) -> str:
    """The assignment reference for user-facing Slack replies: a dashboard deep
    link (``<url|text>`` markup) when deployed, else the bare id."""
    url = dashboard_run_url(assignment_id)
    if url:
        return f"<{url}|assignment `{assignment_id}`>"
    return f"assignment `{assignment_id}`"


def unit_for(assignment_id: str, source_context: dict | None) -> str:
    """The unit-of-work key notifications thread under.

    A Slack-threaded dispatch keys on the CONVERSATION (workspace#channel#
    thread_ts), not the assignment — so a D8 follow-up (a new assignment in the
    same thread) continues the parent's ops-channel notification thread instead
    of fragmenting one conversation across several. Everything else keys on the
    assignment id as before."""
    ctx = source_context or {}
    from enrichment import slack_thread_key

    key = slack_thread_key(
        str(ctx.get("workspace", "") or ""),
        str(ctx.get("channel_id", "") or ""),
        str(ctx.get("thread_ts", "") or ""),
    )
    if key:
        return f"thread:{key}"
    return assignment_id or ""

_assignments = None
_cache = None  # list of notif_sub records
_cache_expires_at = 0.0
_CACHE_TTL_SECONDS = 30


def _assignments_table():
    global _assignments
    if _assignments is None:
        _assignments = boto3.resource("dynamodb").Table(
            os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments")
        )
    return _assignments


def _subs_snapshot(now: float | None = None) -> list[dict]:
    global _cache, _cache_expires_at
    current = time.time() if now is None else now
    if _cache is None or current >= _cache_expires_at:
        _cache = config_query.query_kind("notif_sub")
        _cache_expires_at = current + _CACHE_TTL_SECONDS
    return _cache


def reset_cache() -> None:
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def _subscription_wants(
    sub: dict, *, tier: str, event: str, repo: str,
    project: str = "", space: str = "",
) -> bool:
    """Whether ``sub`` should receive this (tier, event, container). A
    subscription matches iff: the tier meets the subscription's severity floor,
    the event is listed under that tier, and (when the event names a container)
    that container is in the subscription's scope. The container axes compose:
    a GitHub event checks ``repos``, a Jira event checks ``projects``, a
    Confluence event checks ``spaces`` (atlassian-connector spec §A9.1). A
    container-scoped event with no matching scope is skipped, so a channel never
    gets notifications for a container it didn't select."""
    if _SEVERITY_RANK.get(tier, 0) < _SEVERITY_RANK.get(sub.get("min_severity", TIER_INFORMATIVE), 0):
        return False
    tier_events = (sub.get("tiers") or {}).get(tier) or []
    if event not in tier_events:
        return False
    if repo:
        if repo.strip().casefold() not in (sub.get("repos") or []):
            return False
    if project:
        if project not in (sub.get("projects") or []):
            return False
    if space:
        if space not in (sub.get("spaces") or []):
            return False
    return True


def _thread_key(unit: str) -> str:
    return f"{_THREAD_PK_PREFIX}{unit}"


def _get_thread_ts(team_id: str, channel_id: str, unit: str) -> str | None:
    """The remembered parent ``ts`` for (channel, unit-of-work), or None for the
    first post. Stored in the assignments table (shared TTL store) keyed by a
    synthetic id so a run's follow-ups thread under the first message."""
    if not unit:
        return None
    try:
        resp = _assignments_table().get_item(
            Key={"assignment_id": f"{_thread_key(unit)}#{team_id}#{channel_id}"}
        )
        item = resp.get("Item") or {}
        return item.get("thread_ts")
    except Exception:  # noqa: BLE001
        return None


def _remember_thread_ts(team_id: str, channel_id: str, unit: str, ts: str) -> None:
    if not unit or not ts:
        return
    try:
        _assignments_table().put_item(
            Item={
                "assignment_id": f"{_thread_key(unit)}#{team_id}#{channel_id}",
                "kind": "notif_thread",
                "thread_ts": ts,
                "ttl": int(time.time()) + 30 * 24 * 60 * 60,
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to remember thread ts for %s", unit)


def _resolve_actor_identity_id(actor: dict | None) -> str:
    """Resolve ``actor`` (``{source, handle, workspace}``) to an identity_id once,
    independent of any workspace — the per-workspace Slack uid is looked up later.
    '' if unresolvable (the caller degrades to an unmentioned post, §18.4)."""
    if not actor:
        return ""
    person = identity_map.find_by_handle(
        actor.get("source", ""), actor.get("handle", ""), actor.get("workspace", "")
    )
    return person.identity_id if person and person.identity_id else ""


def _mention_in(identity_id: str, team_id: str) -> str:
    """Render an @mention for an already-resolved identity in ``team_id``, or ''
    if that person has no Slack handle in this workspace (never mis-ping)."""
    if not identity_id:
        return ""
    uid = identity_map.slack_handle_for(identity_id, team_id)
    return f"<@{uid}>" if uid else ""


def notify(
    *,
    tier: str,
    event: str,
    text: str,
    repo: str = "",
    project: str = "",
    space: str = "",
    unit: str = "",
    actor: dict | None = None,
) -> int:
    """Fan a fleet/SCM event out to every subscribed channel. Returns the count of
    channels a message was posted to (0 if none subscribed / all failed).

    - ``tier`` / ``event`` — the tier bucket and the specific event id a
      subscription lists (e.g. tier ``error`` / event ``run_failed``).
    - ``repo`` — the repo the event concerns ("" for repo-less fleet events);
      gates repo-scoped subscriptions.
    - ``unit`` — the unit-of-work key for threading (e.g. an assignment id or
      ``pr:<repo>:<number>``); follow-ups reuse the first post's thread.
    - ``actor`` — who to @mention (actionable/error tiers only); resolved through
      the identity map to the right Slack user in each workspace.
    """
    if tier not in TIERS:
        logger.warning("notify called with unknown tier %r", tier)
        return 0
    # Resolve the actor→person ONCE (workspace-independent); inside the fan-out we
    # only look up that person's Slack uid per workspace. Avoids re-scanning the
    # identity set for the same actor on every subscribed channel.
    actor_identity_id = (
        _resolve_actor_identity_id(actor) if tier in MENTION_TIERS else ""
    )
    posted = 0
    for sub in _subs_snapshot():
        if not _subscription_wants(
            sub, tier=tier, event=event, repo=repo, project=project, space=space
        ):
            continue
        team_id = sub.get("team_id", "")
        channel_id = sub.get("channel_id", "")
        body = text
        # Mentions only on actionable + error, and only if we can resolve the
        # person to a Slack user in THIS workspace.
        if actor_identity_id:
            mention = _mention_in(actor_identity_id, team_id)
            if mention:
                body = f"{mention} {text}"
        thread_ts = _get_thread_ts(team_id, channel_id, unit)
        ok, new_ts = reply.post_slack_message_ts(
            team_id=team_id, channel=channel_id, body=body, thread_ts=thread_ts
        )
        if ok:
            posted += 1
            # First post in a thread → remember its ts as the parent for follow-ups.
            if unit and not thread_ts and new_ts:
                _remember_thread_ts(team_id, channel_id, unit, new_ts)
        else:
            logger.warning("notification delivery failed for %s/%s", team_id, channel_id)
    return posted


# --- per-user DM notifications (atlassian-connector spec §A9.2) ---------------
# Opt-in, self-serve: an ACTIVE identity with a VERIFIED Slack handle can hold a
# notif_pref# row. Delivery opens an IM (conversations.open) and threads the DM.
# No verified handle for a team ⇒ SILENT degrade (metric, never a mis-ping).

_prefs_cache = None
_prefs_expires_at = 0.0


def _prefs_snapshot(now: float | None = None) -> list[dict]:
    global _prefs_cache, _prefs_expires_at
    current = time.time() if now is None else now
    if _prefs_cache is None or current >= _prefs_expires_at:
        _prefs_cache = config_query.query_kind("notif_pref")
        _prefs_expires_at = current + _CACHE_TTL_SECONDS
    return _prefs_cache


def _get_pref(identity_id: str) -> dict | None:
    for p in _prefs_snapshot():
        if p.get("identity_id") == identity_id:
            return p
    return None


def _pref_wants(pref: dict, *, tier: str, event: str) -> bool:
    if _SEVERITY_RANK.get(tier, 0) < _SEVERITY_RANK.get(pref.get("min_tier", TIER_ACTIONABLE), 0):
        return False
    return event in ((pref.get("tiers") or {}).get(tier) or [])


def notify_user(
    identity_id: str, *, tier: str, event: str, text: str,
    team_id: str, unit: str = "",
) -> bool:
    """DM a person if they've opted into this (tier, event) via their notif_pref
    (§A9.2). Resolves the identity's Slack handle in ``team_id``; no handle ⇒
    silent degrade (metric, never a mis-ping). Returns True if a DM was sent.

    The same seam serves non-Atlassian events for free (run_completed / _failed /
    awaiting_approval DMs to the requester — assignment_notifier's one new call
    site). Best-effort throughout."""
    if tier not in TIERS or not identity_id:
        return False
    pref = _get_pref(identity_id)
    if not pref or not _pref_wants(pref, tier=tier, event=event):
        return False
    uid = identity_map.slack_handle_for(identity_id, team_id)
    if not uid:
        # No verified handle for this workspace — degrade silently (never DM the
        # wrong person). A metric would be emitted by the caller's context.
        logger.info("notify_user: no slack handle for %s in %s — degrading", identity_id, team_id)
        return False
    channel = _open_im(team_id, uid)
    if not channel:
        return False
    thread_ts = _get_thread_ts(team_id, channel, unit) if unit else None
    ok, new_ts = reply.post_slack_message_ts(team_id=team_id, channel=channel,
                                             body=text, thread_ts=thread_ts)
    if ok and unit and not thread_ts and new_ts:
        _remember_thread_ts(team_id, channel, unit, new_ts)
    return ok


def _open_im(team_id: str, user_id: str) -> str | None:
    """Open (or fetch) the IM channel id for a user via conversations.open
    (needs the ``im:write`` scope). "" / None on failure."""
    import os

    import requests

    token = reply._get_secret(reply.slack_bot_token_param(team_id))
    if not token:
        return None
    try:
        resp = requests.post(
            "https://slack.com/api/conversations.open",
            json={"users": user_id},
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("conversations.open failed for %s: %s", team_id, data.get("error"))
            return None
        return (data.get("channel") or {}).get("id")
    except (requests.RequestException, ValueError):
        logger.exception("conversations.open error for %s", team_id)
        return None


def reset_prefs_cache() -> None:
    global _prefs_cache, _prefs_expires_at
    _prefs_cache = None
    _prefs_expires_at = 0.0
