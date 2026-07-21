"""Interactive `/sdlc-notify` notification configuration (spec §18.2).

A channel self-serves its notification subscription through a Block Kit modal:
the slash command opens the modal (``views.open`` using the ``trigger_id``); the
submit posts back on the ``/slack/interactions`` route as a ``view_submission``,
which we parse into a ``notif_sub`` row.

A subscription only RECEIVES notifications — it grants no access — so this needs
no admin approval (unlike channel onboarding). The repo scope is nonetheless
bounded to the fleet's onboarded repos so a channel can't subscribe to a repo the
fleet doesn't manage (§18.2); the bounding is enforced here (the writer) and again
if the admin API ever re-writes the row.

The three tiers are rendered as checkbox groups; each option is a specific event
a channel can opt into. Only actionable + error tiers @mention people downstream
(notify.MENTION_TIERS) — the modal doesn't need to say so, but the tier split is
what drives it.
"""

import json
import logging
import os
import time

import boto3
import requests

import config_query

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
STAGE = os.environ.get("STAGE", "dev")

# The callback_id that identifies our notification modal on submit.
NOTIFY_VIEW_CALLBACK = "sdlc_notify_config"

# Tier → the specific events a channel can subscribe to, with human labels. Kept
# here (the modal builder) as the single source of the option set; notify.py
# matches on the raw event ids a subscription stores.
TIER_EVENTS = {
    "actionable": [
        ("proposal_ready", "Agent posted a proposal / decomposition to review"),
        ("awaiting_approval", "A run is awaiting your approval"),
        ("review_requested", "A PR needs review"),
        ("question", "An agent asked the requester a question"),
    ],
    "informative": [
        ("run_started", "A run kicked off"),
        ("run_completed", "A run completed"),
        ("task_picked_up", "An agent picked up a task"),
        ("pr_opened", "A pull request was opened"),
        ("pr_merged", "A pull request was merged"),
        ("issue_opened", "An issue was opened"),
    ],
    "error": [
        ("run_failed", "A run failed"),
        ("guardrail_tripped", "A prompt-injection guardrail tripped"),
        ("credential_expired", "A credential expired"),
        ("assignment_stuck", "An assignment is stuck"),
    ],
}
TIER_LABELS = {
    "actionable": "Actionable — you should engage",
    "informative": "Informative — FYI, no action",
    "error": "Errors — something went wrong",
}

_ssm = boto3.client("ssm")


def _bot_token(team_id: str) -> str | None:
    """The workspace bot token (per-invocation fetch; never a module global —
    T-36). views.open + the modal live in the same trust model as replies."""
    param = f"/sdlc-agents/{STAGE}/slack/{team_id}/bot-token"
    try:
        return _ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001
        logger.exception("could not fetch Slack bot token for %s", team_id)
        return None


def _tier_block(tier: str) -> dict:
    """A checkbox input block for one notification tier."""
    options = [
        {"text": {"type": "plain_text", "text": label}, "value": event}
        for event, label in TIER_EVENTS[tier]
    ]
    return {
        "type": "input",
        "block_id": f"tier_{tier}",
        "optional": True,
        "label": {"type": "plain_text", "text": TIER_LABELS[tier]},
        "element": {
            "type": "checkboxes",
            "action_id": "events",
            "options": options,
        },
    }


def build_notify_modal(*, team_id: str, channel_id: str, channel_name: str, repos: list[str]) -> dict:
    """The Block Kit view for `/sdlc-notify`. ``repos`` is the channel's grantable
    repo set (fleet-onboarded); rendered as a multi-select so the channel scopes
    which repos' events it wants. ``private_metadata`` carries the team/channel
    AND the ordered repo list so the submit handler doesn't trust client-supplied
    ids and can map option values back to full repo names."""
    # Slack caps an option's `value` at 75 chars; a repo full_name (owner/repo,
    # up to ~100 chars) can exceed that and would make views.open fail outright.
    # Use the repo's index as the value (always short) and resolve it back to the
    # full name on submit via the repo list stashed in private_metadata.
    shown = repos[:100]
    repo_options = [
        {"text": {"type": "plain_text", "text": r[:75]}, "value": str(i)}
        for i, r in enumerate(shown)
    ]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Configure fleet notifications for *#{channel_name or channel_id}*.",
            },
        },
    ]
    if repo_options:
        blocks.append(
            {
                "type": "input",
                "block_id": "repos",
                "optional": True,
                "label": {"type": "plain_text", "text": "Repositories (leave empty for none)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "selected",
                    "options": repo_options,
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_No onboarded repos yet — ask an admin to onboard one._"}
                ],
            }
        )
    for tier in ("actionable", "informative", "error"):
        blocks.append(_tier_block(tier))
    return {
        "type": "modal",
        "callback_id": NOTIFY_VIEW_CALLBACK,
        "private_metadata": json.dumps(
            {"team_id": team_id, "channel_id": channel_id, "channel_name": channel_name, "repos": shown}
        ),
        "title": {"type": "plain_text", "text": "Fleet Notifications"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def open_modal(*, team_id: str, trigger_id: str, view: dict) -> bool:
    """Open the modal via views.open. Best-effort — a failure just means the user
    doesn't see the modal (they can retry the command)."""
    token = _bot_token(team_id)
    if not token:
        return False
    try:
        resp = requests.post(
            f"{SLACK_API}/views.open",
            json={"trigger_id": trigger_id, "view": view},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=3,  # Slack requires views.open within 3s of the trigger_id
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("views.open rejected for %s: %s", team_id, data.get("error"))
            return False
        return True
    except (requests.RequestException, ValueError):
        logger.exception("views.open failed for %s", team_id)
        return False


def onboarded_repos() -> list[str]:
    """The fleet's onboarded, enabled repos — the grantable notification scope
    (§18.2). One bounded Query on the kind-index (not a full-table scan)."""
    try:
        return sorted(
            item["repo"]
            for item in config_query.query_kind("repo")
            if item.get("enabled") and item.get("repo")
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not list onboarded repos")
        return []


def parse_view_submission(view: dict) -> dict:
    """Extract the subscription config from a submitted notify modal's ``view``.
    Returns ``{team_id, channel_id, repos, tiers, min_severity}``. The team/channel
    come from ``private_metadata`` (server-set at open), never from client state,
    so a tampered submit can't retarget another channel."""
    meta = {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (ValueError, TypeError):
        pass
    state = (view.get("state") or {}).get("values") or {}

    # Option values are indices into the repo list stashed in private_metadata at
    # build time (Slack caps option values at 75 chars, so we can't put the full
    # repo name there). Map each selected index back to its repo name; ignore any
    # index that doesn't resolve (stale/tampered submit).
    meta_repos = meta.get("repos") or []
    repo_block = state.get("repos") or {}
    selected = (repo_block.get("selected") or {}).get("selected_options") or []
    repos = []
    for o in selected:
        val = o.get("value")
        if val is None:
            continue
        try:
            idx = int(val)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(meta_repos):
            repos.append(meta_repos[idx])

    tiers: dict[str, list[str]] = {}
    for tier in ("actionable", "informative", "error"):
        block = state.get(f"tier_{tier}") or {}
        opts = (block.get("events") or {}).get("selected_options") or []
        events = [o.get("value") for o in opts if o.get("value")]
        if events:
            tiers[tier] = events

    # Severity floor: the lowest tier the channel opted into (so an error-only
    # subscription doesn't get informative noise). Default informative.
    min_severity = "informative"
    if tiers:
        if "informative" in tiers:
            min_severity = "informative"
        elif "actionable" in tiers:
            min_severity = "actionable"
        else:
            min_severity = "error"

    return {
        "team_id": meta.get("team_id", ""),
        "channel_id": meta.get("channel_id", ""),
        "repos": repos,
        "tiers": tiers,
        "min_severity": min_severity,
    }


def save_subscription(config: dict) -> None:
    """Persist a parsed subscription as a ``notif_sub`` row, bounding repos to the
    fleet's onboarded set (§18.2). Writes the same row shape the admin config_store
    does (shared table contract; the dispatch package can't import config_store)."""
    team_id = config.get("team_id", "")
    channel_id = config.get("channel_id", "")
    if not team_id or not channel_id:
        logger.error("save_subscription missing team/channel")
        return
    grantable = {r.strip().casefold() for r in onboarded_repos()}
    repos = sorted({r.strip().casefold() for r in config.get("repos", []) if r} & grantable)
    now = int(time.time())
    table = boto3.resource("dynamodb").Table(os.environ["FLEET_CONFIG_TABLE"])
    existing = table.get_item(Key={"pk": f"notif_sub#{team_id}#{channel_id}"}).get("Item") or {}
    table.put_item(
        Item={
            "pk": f"notif_sub#{team_id}#{channel_id}",
            "kind": "notif_sub",
            "team_id": team_id,
            "channel_id": channel_id,
            "repos": repos,
            "tiers": config.get("tiers", {}),
            "min_severity": config.get("min_severity", "informative"),
            "created_by": existing.get("created_by", f"slack:{team_id}"),
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
    )
