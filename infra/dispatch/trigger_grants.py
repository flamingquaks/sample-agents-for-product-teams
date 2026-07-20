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

_TRIGGER_RULE_PK_PREFIX = "trigger_rule#"
_SLACK_WS_PK_PREFIX = "slack_ws#"
_SLACK_CHAN_PK_PREFIX = "slack_chan#"

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

    Paged on LastEvaluatedKey: a single scan() returns only the first 1MB page,
    and a dropped rule/channel would silently change an authorization decision —
    a granted user would be denied, or a denied channel would be allowed. So we
    always drain every page (mirrors fleet_config._load_snapshot)."""
    table = _get_table()
    rules: list[dict] = []
    workspaces: dict[str, dict] = {}
    channels: dict[tuple, dict] = {}
    start_key = None
    while True:
        resp = table.scan(ExclusiveStartKey=start_key) if start_key else table.scan()
        for item in resp.get("Items", []):
            pk = str(item.get("pk", ""))
            if pk.startswith(_TRIGGER_RULE_PK_PREFIX):
                rules.append(item)
            elif pk.startswith(_SLACK_WS_PK_PREFIX):
                tid = str(item.get("team_id", ""))
                if tid:
                    workspaces[tid] = item
            elif pk.startswith(_SLACK_CHAN_PK_PREFIX):
                tid = str(item.get("team_id", ""))
                cid = str(item.get("channel_id", ""))
                if tid and cid:
                    channels[(tid, cid)] = item
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
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
