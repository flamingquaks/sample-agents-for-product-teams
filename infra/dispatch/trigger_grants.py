"""Trigger-authorization grant reader for the Dispatch Router.

Read side of the trigger-authz DATA the admin API writes (see
infra/dashboard/config_store.py — the two share this table's schema as a
contract, but the packages deploy independently so neither imports the other).

This is the heart of the data-driven authorization model (spec §5): granting a
user or group access is a plain DynamoDB write (a ``trigger_rule`` row), NOT a
new Cedar policy. The Cedar policy SET is small and FIXED; the router reads these
rows per dispatch and hands the resolved grant sets to AVP as ENTITY ATTRIBUTES.
So the number of AVP policies stays constant no matter how many users/rules
exist — a policy-per-user would be the AVP anti-pattern this avoids.

Two independent axes, both resolved here from the table:

  1. WHO — ``trigger_rule`` rows: subject (a ``user`` principal id, or a
     ``group``) → agent (``"*"`` = any) → workspace (``"*"`` = any), with an
     ``effect`` of permit or forbid. Resolved by ``agent_grants(agent, ws)`` into
     four sets: allowed/denied principals and allowed/denied groups. The FIXED
     Cedar policies permit on an allowed principal/group and forbid (forbid-wins)
     on a denied one.

  2. WHERE — ``slack_workspace`` (``default_channel_policy``) + ``slack_channel``
     (per-channel allow|deny) rows. Resolved by ``channel_allowed(ws, chan)`` into
     ONE boolean per request (the posture math lives here in testable Python, not
     in Cedar): allowlist ⇒ allowed only if an ``allow`` row exists; denylist ⇒
     allowed unless a ``deny`` row exists. Non-Slack sources carry no
     workspace/channel and are treated as channel-allowed (the axis doesn't apply).

Read through a short-TTL cache (mirrors fleet_config) so an admin grant/revoke
takes effect fleet-wide within ``_CACHE_TTL_SECONDS`` without a DynamoDB read on
every dispatch.
"""

import os
import time
from dataclasses import dataclass, field

import boto3

import config_query

# Mirror config_store.CHANNEL_POLICY_* / CHANNEL_MODE_* (schema contract).
CHANNEL_POLICY_ALLOWLIST = "allowlist"
CHANNEL_POLICY_DENYLIST = "denylist"
CHANNEL_MODE_ALLOW = "allow"
CHANNEL_MODE_DENY = "deny"

RULE_PERMIT = "permit"
RULE_FORBID = "forbid"
RULE_SUBJECT_USER = "user"
RULE_SUBJECT_GROUP = "group"

# Short TTL so an admin grant/revoke propagates within a minute across warm
# containers, without a DynamoDB read per dispatch (mirrors fleet_config).
_CACHE_TTL_SECONDS = 30

_table = None
_cache = None  # {"rules": [...], "workspaces": {team_id: rec}, "channels": {(team,chan): rec}}
_cache_expires_at = 0.0


@dataclass(frozen=True)
class AgentGrants:
    """The resolved WHO grant sets for one (agent, workspace) — the Cedar entity
    attributes of the ``Agent`` resource. Each is a list of principal ids / group
    ids. A ``forbid`` (denied) membership beats a permit (Cedar forbid-wins), so
    the router passes both and the fixed policy set does the precedence."""

    allowed_principals: list = field(default_factory=list)
    denied_principals: list = field(default_factory=list)
    allowed_groups: list = field(default_factory=list)
    denied_groups: list = field(default_factory=list)


def _get_table():
    global _table
    if _table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


def _load_snapshot() -> dict:
    """Read the trigger rules + Slack workspace/channel rows from DynamoDB.

    One bounded Query per kind on the kind-index (each drained across pages — a
    dropped rule/channel would silently change an authorization decision: a
    granted user denied, or a denied channel allowed). Unlike the previous
    full-table Scan, this reads only the three authz kinds, not every config row
    (mirrors fleet_config._load_snapshot)."""
    rules = config_query.query_kind("trigger_rule")
    workspaces: dict[str, dict] = {}
    for item in config_query.query_kind("slack_workspace"):
        tid = str(item.get("team_id", ""))
        if tid:
            workspaces[tid] = item
    channels: dict[tuple, dict] = {}
    for item in config_query.query_kind("slack_channel"):
        tid = str(item.get("team_id", ""))
        cid = str(item.get("channel_id", ""))
        if tid and cid:
            channels[(tid, cid)] = item
    return {"rules": rules, "workspaces": workspaces, "channels": channels}


def _snapshot(now: float | None = None) -> dict:
    global _cache, _cache_expires_at
    current = time.time() if now is None else now
    if _cache is None or current >= _cache_expires_at:
        _cache = _load_snapshot()
        _cache_expires_at = current + _CACHE_TTL_SECONDS
    return _cache


def reset_cache() -> None:
    """Drop the cached snapshot (forces a reload on the next read). For tests."""
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def _rule_matches(rule: dict, agent_id: str, workspace: str) -> bool:
    """Whether a rule applies to this (agent, workspace). A rule's ``agent_id`` /
    ``workspace`` of ``"*"`` is a wildcard matching any; a concrete value must
    match exactly. An empty request workspace (github/asana carry none) matches
    only wildcard-workspace rules."""
    r_agent = rule.get("agent_id", "*")
    if r_agent != "*" and r_agent != agent_id:
        return False
    r_ws = rule.get("workspace", "*")
    if r_ws != "*" and r_ws != workspace:
        return False
    return True


def agent_grants(agent_id: str, workspace: str = "") -> AgentGrants:
    """Resolve the WHO grant sets for ``agent_id`` in ``workspace`` from the
    trigger rules. Deterministic; order-preserving within each set."""
    ap, dp, ag, dg = [], [], [], []
    for rule in _snapshot()["rules"]:
        if not _rule_matches(rule, agent_id, workspace):
            continue
        subject = str(rule.get("subject_id", ""))
        if not subject:
            continue
        is_group = rule.get("subject_type") == RULE_SUBJECT_GROUP
        is_forbid = rule.get("effect") == RULE_FORBID
        target = (dg if is_group else dp) if is_forbid else (ag if is_group else ap)
        if subject not in target:
            target.append(subject)
    return AgentGrants(
        allowed_principals=ap,
        denied_principals=dp,
        allowed_groups=ag,
        denied_groups=dg,
    )


def is_workspace_enabled(team_id: str) -> bool:
    """Whether ``team_id`` is an onboarded, enabled, active Slack workspace.
    Fail-closed: unknown / disabled / non-active ⇒ False. Used by the Slack
    receiver to reject deliveries from a workspace an admin hasn't cleared."""
    if not team_id:
        return False
    ws = _snapshot()["workspaces"].get(team_id)
    if ws is None:
        return False
    return bool(ws.get("enabled")) and ws.get("status") == "active"


def put_channel_request(
    *,
    team_id: str,
    channel_id: str,
    channel_name: str = "",
    requested_by: str,
    requested_agents=None,
    requested_repos=None,
) -> None:
    """Write a pending channel-onboarding request from the Slack receiver.

    The dispatch package can't import the dashboard's config_store (separate
    deploy), so this writes the same ``channel_request`` row shape directly —
    the two share the table as a contract (mirrors fleet_config ↔ config_store).
    Validates the Slack ids so the receiver can't persist a malformed request.
    Raises ValueError on bad input."""
    import re
    import uuid

    if not re.match(r"^T[A-Z0-9]{6,}$", team_id or ""):
        raise ValueError("invalid team id")
    if not re.match(r"^C[A-Z0-9]{6,}$", channel_id or ""):
        raise ValueError("invalid channel id")
    if not (requested_by or "").strip():
        raise ValueError("requested_by is required")
    agents = [a for a in (requested_agents or []) if a and a != "*"]
    for a in agents:
        if not re.match(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$", a):
            raise ValueError(f"invalid requested agent id {a!r}")
    # Requested repos (owner/repo) — same shape check the admin API applies;
    # these flow into the admin's approval UI and, once approved, into the
    # channel's repo grant, so a malformed value is rejected at the boundary.
    repos = [r.strip().casefold() for r in (requested_repos or []) if r and r.strip()]
    for r in repos:
        if not re.match(r"^[a-z0-9._-]+/[a-z0-9._-]+$", r):
            raise ValueError(f"invalid requested repo {r!r}")
    request_id = str(uuid.uuid4())
    _get_table().put_item(
        Item={
            "pk": f"chan_req#{request_id}",
            "kind": "channel_request",
            "request_id": request_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "requested_by": requested_by.strip(),
            "requested_agents": agents,
            "requested_repos": repos,
            "note": "",
            "status": "pending",
            "created_at": int(time.time()),
            "decided_by": "",
            "decided_at": None,
        }
    )


def channel_repos(workspace: str, channel_id: str) -> list[str]:
    """The repos APPROVED for this channel — the set an admin granted when
    approving the channel's onboarding request (stored on the channel allow
    row). This is the channel's direct-work scope: a Slack-triggered dispatch
    may only NAME these repos as its work target. Repos grouped with an
    approved repo (co_repo_mode) remain reachable BY THE AGENT when working an
    approved repo — the gateway/broker's co-repo grouping handles that — but
    they are not directly selectable from the channel unless approved here.
    Empty when the channel has no grant (or none recorded)."""
    row = _snapshot()["channels"].get((workspace, channel_id))
    if not row:
        return []
    return sorted({r for r in (row.get("repos") or []) if r})


def channel_allowed(workspace: str, channel_id: str) -> bool:
    """Whether triggering is allowed in this channel, per the workspace's
    ``default_channel_policy`` + the per-channel allow/deny rows.

    Non-Slack (no workspace) ⇒ True: the channel axis doesn't apply to
    github/asana. An unknown workspace ⇒ False (fail-closed): a dispatch tagged
    with a workspace we don't have config for shouldn't be channel-allowed by
    accident. allowlist ⇒ allowed iff an ``allow`` row exists (default-deny per
    channel). denylist ⇒ allowed unless a ``deny`` row exists (default-allow)."""
    if not workspace:
        return True
    snap = _snapshot()
    ws = snap["workspaces"].get(workspace)
    if ws is None:
        return False
    policy = ws.get("default_channel_policy", CHANNEL_POLICY_ALLOWLIST)
    row = snap["channels"].get((workspace, channel_id))
    if policy == CHANNEL_POLICY_DENYLIST:
        return not (row is not None and row.get("mode") == CHANNEL_MODE_DENY)
    # allowlist (default, recommended): only explicit allow rows pass.
    return row is not None and row.get("mode") == CHANNEL_MODE_ALLOW
