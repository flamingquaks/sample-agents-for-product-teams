"""Block Kit modals for the Slack slash commands (spec §19 interactive flows).

Two modals, both following the pattern slack_notify.build_notify_modal set:

  - **Onboard channel** (``/sdlc-onboard-channel``): agent multi-select + repo
    multi-select + optional note → files a pending ``channel_request`` an admin
    approves in the dashboard. Replaces the old free-text arg parsing (users
    shouldn't have to know agent ids by heart).
  - **Message agent** (``/sdlc-message-agent``): agent dropdown + a
    multi-checkbox of the CHANNEL'S granted repos + a message box → dispatches
    to the router and posts a visible confirmation in the channel. Replaces the
    ``/fleet @agent …`` form, whose ``@`` collided with Slack user tagging.

Anti-tampering: option values are INDICES into lists stashed in the view's
``private_metadata`` at build time (server-set; the submit handler maps them
back). Slack caps option values at 75 chars anyway (a full owner/repo can
exceed it) — same design as the notify modal. A tampered/stale index simply
doesn't resolve.
"""

import json
import logging

logger = logging.getLogger(__name__)

ONBOARD_VIEW_CALLBACK = "sdlc_onboard_channel"
MESSAGE_VIEW_CALLBACK = "sdlc_message_agent"

# Slack caps plain_text option labels; keep room for the marker.
_LABEL_MAX = 75


def _index_options(items: list[str]) -> list[dict]:
    """Checkbox/select options whose values are list indices (see module doc)."""
    return [
        {"text": {"type": "plain_text", "text": item[:_LABEL_MAX]}, "value": str(i)}
        for i, item in enumerate(items)
    ]


def resolve_indices(selected_options: list, items: list[str]) -> list[str]:
    """Map submitted option values (indices) back to the metadata list. Ignores
    anything that doesn't resolve (stale/tampered submit)."""
    out = []
    for o in selected_options or []:
        try:
            idx = int(o.get("value"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            out.append(items[idx])
    return out


def build_onboard_modal(
    *, team_id: str, channel_id: str, channel_name: str,
    agents: list[str], repos: list[str],
) -> dict:
    """The ``/sdlc-onboard-channel`` view. ``agents`` is the fleet's known agent
    ids (registry); ``repos`` the fleet's onboarded repos. Both selects are
    optional — an empty pick means "any agent" / "no repos yet", and the admin
    scopes the final grant at approval anyway."""
    agents = agents[:100]
    repos = repos[:100]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Request fleet access for *#{channel_name or channel_id}*. "
                    "An admin reviews and approves the final scope."
                ),
            },
        },
    ]
    if agents:
        blocks.append({
            "type": "input",
            "block_id": "agents",
            "optional": True,
            "label": {"type": "plain_text", "text": "Agents you want (empty = admin decides)"},
            "element": {
                "type": "multi_static_select",
                "action_id": "selected",
                "options": _index_options(agents),
            },
        })
    if repos:
        blocks.append({
            "type": "input",
            "block_id": "repos",
            "optional": True,
            "label": {"type": "plain_text", "text": "Repositories this channel will work on"},
            "element": {
                "type": "multi_static_select",
                "action_id": "selected",
                "options": _index_options(repos),
            },
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_No repositories onboarded to the fleet yet — an admin can add repos later._",
            }],
        })
    return {
        "type": "modal",
        "callback_id": ONBOARD_VIEW_CALLBACK,
        "private_metadata": json.dumps({
            "team_id": team_id, "channel_id": channel_id,
            "channel_name": channel_name, "agents": agents, "repos": repos,
        }),
        "title": {"type": "plain_text", "text": "Onboard this channel"},
        "submit": {"type": "plain_text", "text": "Request"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def parse_onboard_submission(view: dict) -> dict:
    """→ ``{team_id, channel_id, channel_name, requested_agents, requested_repos}``.
    Team/channel come from private_metadata (server-set at open), never client
    state, so a tampered submit can't file a request for another channel."""
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (ValueError, TypeError):
        meta = {}
    state = (view.get("state") or {}).get("values") or {}

    def _selected(block_id: str, items: list[str]) -> list[str]:
        block = state.get(block_id) or {}
        opts = (block.get("selected") or {}).get("selected_options") or []
        return resolve_indices(opts, items)

    return {
        "team_id": meta.get("team_id", ""),
        "channel_id": meta.get("channel_id", ""),
        "channel_name": meta.get("channel_name", ""),
        "requested_agents": _selected("agents", meta.get("agents") or []),
        "requested_repos": _selected("repos", meta.get("repos") or []),
    }


def build_message_modal(
    *, team_id: str, channel_id: str, channel_name: str,
    agents: list[str], channel_repos: list[str],
) -> dict:
    """The ``/sdlc-message-agent`` view: agent dropdown (required), the
    channel's GRANTED repos as checkboxes (optional — an agent may be
    repo-less, e.g. Asana-only), and the message (required)."""
    agents = agents[:100]
    channel_repos = channel_repos[:100]
    blocks = [
        {
            "type": "input",
            "block_id": "agent",
            "label": {"type": "plain_text", "text": "Agent"},
            "element": {
                "type": "static_select",
                "action_id": "selected",
                "options": _index_options(agents),
            },
        },
    ]
    if channel_repos:
        blocks.append({
            "type": "input",
            "block_id": "repos",
            "optional": True,
            "label": {"type": "plain_text", "text": "Repositories to work on (approved for this channel)"},
            "element": {
                "type": "checkboxes",
                "action_id": "selected",
                "options": _index_options(channel_repos),
            },
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_No repositories approved for this channel — the agent can still act on non-repo work._",
            }],
        })
    blocks.append({
        "type": "input",
        "block_id": "message",
        "label": {"type": "plain_text", "text": "Message"},
        "element": {
            "type": "plain_text_input",
            "action_id": "text",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": "What should the agent do?"},
        },
    })
    return {
        "type": "modal",
        "callback_id": MESSAGE_VIEW_CALLBACK,
        "private_metadata": json.dumps({
            "team_id": team_id, "channel_id": channel_id,
            "channel_name": channel_name, "agents": agents, "repos": channel_repos,
        }),
        "title": {"type": "plain_text", "text": "Message an agent"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def parse_message_submission(view: dict) -> dict:
    """→ ``{team_id, channel_id, channel_name, agent_id, repos, message}``.
    ``agent_id``/``repos`` resolve through private_metadata indices — the submit
    can only name an agent the modal OFFERED and repos GRANTED to the channel
    at open time (the modal is the authorization surface for repo scope)."""
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (ValueError, TypeError):
        meta = {}
    state = (view.get("state") or {}).get("values") or {}

    agent_block = state.get("agent") or {}
    agent_opt = (agent_block.get("selected") or {}).get("selected_option") or {}
    agent_ids = resolve_indices([agent_opt] if agent_opt else [], meta.get("agents") or [])

    repo_block = state.get("repos") or {}
    repo_opts = (repo_block.get("selected") or {}).get("selected_options") or []
    repos = resolve_indices(repo_opts, meta.get("repos") or [])

    msg_block = state.get("message") or {}
    message = ((msg_block.get("text") or {}).get("value") or "").strip()

    return {
        "team_id": meta.get("team_id", ""),
        "channel_id": meta.get("channel_id", ""),
        "channel_name": meta.get("channel_name", ""),
        "agent_id": agent_ids[0] if agent_ids else "",
        "repos": repos,
        "message": message,
    }
