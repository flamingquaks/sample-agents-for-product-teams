"""Fleet configuration store — DynamoDB access for onboarded repos + settings.

This is the runtime-mutable config the single-repo → multi-repo transformation
introduced: the admin API writes it, and the Dispatch Router reads it (via its
own minimal reader in infra/dispatch, sharing this table's schema as a
contract). It replaces the deploy-time ``FLEET_GITHUB_REPO`` parameter.

Table shape (single table, ``FLEET_CONFIG_TABLE`` env var). Partition key ``pk``:
  - Repo record:   pk="repo#<owner/repo>",  {kind:"repo", repo, enabled(bool),
                   multi_repo_eligible(bool), co_repo_mode, repo_group?,
                   onboarded_by, onboarded_at, status: "pending"|"active", owner,
                   installation_id?, install_verified_at?}
  - Install record: pk="owner#<owner>",     {kind:"install", owner,
                   owner_type: "User"|"Organization", installation_id(int),
                   install_verified_at(epoch)}
  - Settings:      pk="settings",           {kind:"settings", restrict_repos(bool)}
  - Slack workspace: pk="slack_ws#<team_id>", {kind:"slack_workspace", team_id, ...}
  - Slack channel:   pk="slack_chan#<team_id>#<chan_id>", {kind:"slack_channel", mode:"allow"|"deny", ...}
  - Trigger rule:    pk="trigger_rule#<uuid>", {kind:"trigger_rule", subject_type, subject_id, agent_id, workspace, effect, ...} (WHO axis)
  - Channel request: pk="chan_req#<uuid>",   {kind:"channel_request", team_id, channel_id, requested_by, requested_agents[], status:"pending"|"approved"|"denied", ...}
  - Capability:    pk="capability#<agent_id>", {kind:"capability", agent_id,
                   description, aliases[list], triggers{source:[event...]},
                   limits{max_concurrent, timeout_minutes, daily_token_budget},
                   env{KEY:val}, enabled(bool),
                   status: "pending"|"building"|"active"|"failed"|"disabled",
                   image_tag?, runtime_arn?, build_id?, onboarded_by, onboarded_at,
                   updated_at, status_detail?}
                   (WHO may trigger the agent is NOT here — it's Cedar trigger
                   rules, the trigger_rule# records below.)

Capability record (the UI-onboarded agent): a "capability" is an agent the fleet
can dispatch to. It USED to be spread across code + .dispatch/agents.yaml +
per-agent CI wrappers + hand-created IAM roles; onboarding one now writes a single
row here and the dashboard drives the rest (build → runtime → registry → policy).
The registry the Dispatch Router reads from SSM is RENDERED from these rows
(render_registry), so the row is the source of truth that agents.yaml/sync_registry
previously were. Fields mirror the agents.yaml entry the router consumes
(description/aliases/triggers/limits) plus the deploy state the
UI now owns (image_tag, runtime_arn, build_id, status). ``env`` is the per-agent
runtime environment (e.g. Asana GIDs) the build/runtime step injects — the values
the deploy scripts previously sourced from the bootstrap config / GitHub vars.

``enabled``            — the repo is dispatchable (a mention from it is routed).
``multi_repo_eligible``— the master switch: the repo may participate in cross-repo
                         tool actions at all. A repo can be enabled-but-not-eligible:
                         dispatchable, but a dispatch that originates elsewhere can
                         never reach it, and it can never reach another repo.
``co_repo_mode``       — HOW a dispatch that ORIGINATES in this repo may reach other
                         repos (the "approved to run with" rule). One of:
                           - "isolated" (default): only itself. A dispatch from this
                             repo may act on this repo alone — no cross-repo reach.
                           - "group": itself + every other active, eligible repo in
                             the SAME ``repo_group``. Mutual: A reaches B iff both
                             share a group (and both are eligible).
                           - "all": itself + every other active, eligible repo in the
                             fleet (across owners/orgs). The "run with all" option.
                         Only meaningful when ``multi_repo_eligible`` is True.
``repo_group``         — a free-form label (e.g. "acme-platform"); repos sharing it
                         and in ``co_repo_mode="group"`` may operate on each other.
``restrict_repos``     — when False, any enabled repo is dispatchable; when True,
                         only enabled + eligible repos are (the master allowlist).
                         Independent of the per-origin co-repo rules above, which
                         always apply to cross-repo REACH.

Co-repo model (the "approved to run with specific others OR all" requirement): a
repo isn't merely a boolean "multi-repo" flag. Each repo declares, for a dispatch
that originates in it, which OTHER repos that dispatch may touch — none (isolated),
a named group, or all. ``coreachable_repos(origin)`` resolves that set, and it is
enforced at the credential layer: the GitHub App token minted for a dispatch is
scoped to exactly this repo set, so a dispatch physically cannot reach a repo it
isn't approved to run with (spec §3.3, threat-model T-11/T-12).

Per-owner install record (O-1): the GitHub App is installed per OWNER (a user or
an org), so one installation serves all of that owner's onboarded repos. We key
the installation by owner (``owner#<owner>``) rather than duplicating the
installation_id on every repo row — the repo row references its owner. This lets
one App span many individual + org owners (each a separate installation).
"""

import json
import os
import re
import time

import boto3

_REPO_PK_PREFIX = "repo#"
_OWNER_PK_PREFIX = "owner#"
_SETTINGS_PK = "settings"
_CAPABILITY_PK_PREFIX = "capability#"
_SLACK_WS_PK_PREFIX = "slack_ws#"
_SLACK_CHAN_PK_PREFIX = "slack_chan#"
_TRIGGER_RULE_PK_PREFIX = "trigger_rule#"
_CHANNEL_REQUEST_PK_PREFIX = "chan_req#"
# Part II (spec §16–§18): cross-source identity, permission groups, notifications.
_IDENTITY_PK_PREFIX = "identity#"
_USER_REQUEST_PK_PREFIX = "user_req#"
_PERM_GROUP_PK_PREFIX = "perm_group#"
_NOTIF_SUB_PK_PREFIX = "notif_sub#"

# Capability lifecycle. A row starts "pending" the instant it's onboarded, moves
# to "building" while the shared build pipeline runs, "active" once its runtime is
# READY (only active + enabled capabilities are rendered into the router registry),
# "failed" if a build/runtime step errors (kept so the admin can see why + retry),
# and "disabled" when an admin turns it off without deleting it.
CAP_PENDING = "pending"
CAP_BUILDING = "building"
CAP_ACTIVE = "active"
CAP_FAILED = "failed"
CAP_DISABLED = "disabled"
CAP_STATUSES = (CAP_PENDING, CAP_BUILDING, CAP_ACTIVE, CAP_FAILED, CAP_DISABLED)

# agent_id is used unquoted as an ECR repo suffix (sdlc-agents/<id>), a CodeBuild
# AGENT_NAME override, an agents/<id>/ path, and a Cedar/registry literal. Pin it
# to the same shape those consumers accept so an onboard can't inject a path
# traversal, a shell metachar, or a bogus resource name: lowercase, start with a
# letter, end alphanumeric (no trailing hyphen), 2-64 chars.
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")

# How a dispatch that ORIGINATES in a repo may reach OTHER repos.
CO_REPO_ISOLATED = "isolated"  # only itself (default — safest)
CO_REPO_GROUP = "group"  # itself + same repo_group (mutual)
CO_REPO_ALL = "all"  # itself + every eligible repo in the fleet
CO_REPO_MODES = (CO_REPO_ISOLATED, CO_REPO_GROUP, CO_REPO_ALL)

# --- Connectors: Slack workspaces + channels, and trigger-authz rules --------
# (docs/specs/slack-connectors-spec.md §4). Every id below flows into a Cedar
# entity value (read by the router into IsAuthorized) and/or an SSM parameter
# path, so each is pinned to its provider's shape to keep an onboard from
# injecting a Cedar metacharacter, a path traversal, or a bogus resource name.

# Slack ids: team T…, channel C…, user U…, usergroup S… — Slack uses uppercase
# base-36 (min ~8 chars in practice; we require ≥6 after the type letter).
_SLACK_TEAM_RE = re.compile(r"^T[A-Z0-9]{6,}$")
_SLACK_CHANNEL_RE = re.compile(r"^C[A-Z0-9]{6,}$")

# How a workspace's channel rows are interpreted (see put_channel_policy):
# allowlist → triggers only in channels with an explicit allow row (default-deny
# per channel — recommended); denylist → any channel except explicit deny rows.
CHANNEL_POLICY_ALLOWLIST = "allowlist"
CHANNEL_POLICY_DENYLIST = "denylist"
CHANNEL_POLICIES = (CHANNEL_POLICY_ALLOWLIST, CHANNEL_POLICY_DENYLIST)
CHANNEL_MODE_ALLOW = "allow"
CHANNEL_MODE_DENY = "deny"
CHANNEL_MODES = (CHANNEL_MODE_ALLOW, CHANNEL_MODE_DENY)

# Workspace + trigger-rule lifecycle / shape.
SLACK_WS_PENDING = "pending"
SLACK_WS_ACTIVE = "active"
SLACK_WS_DISABLED = "disabled"
SLACK_WS_STATUSES = (SLACK_WS_PENDING, SLACK_WS_ACTIVE, SLACK_WS_DISABLED)

# A trigger rule is scoped to exactly one connector sub-page (per-connector
# rules, spec §4.3 / §9). Kept as a constant so the admin API can't persist a
# rule for a connector the router will never evaluate.
TRIGGER_CONNECTORS = ("slack", "asana", "github")
RULE_SUBJECT_USER = "user"
RULE_SUBJECT_GROUP = "group"
RULE_SUBJECT_TYPES = (RULE_SUBJECT_USER, RULE_SUBJECT_GROUP)
RULE_PERMIT = "permit"
RULE_FORBID = "forbid"
RULE_EFFECTS = (RULE_PERMIT, RULE_FORBID)

# Channel onboarding request lifecycle (spec §4.5). A user runs a Slack slash
# command to REQUEST that their channel be onboarded for specific agents; the
# request lands here ``pending`` and an admin approves (→ creates the allow row +
# grant rows) or denies it in the Connectors panel.
CHAN_REQ_PENDING = "pending"
CHAN_REQ_APPROVED = "approved"
CHAN_REQ_DENIED = "denied"
CHAN_REQ_STATUSES = (CHAN_REQ_PENDING, CHAN_REQ_APPROVED, CHAN_REQ_DENIED)

# --- Identity map (spec §16) -------------------------------------------------
# One record per PERSON, keyed on a synthetic identity_id (uuid). Email is the
# golden JOIN id (an attribute + lookup, NOT the pk) because a first touch from
# GitHub/Asana may carry no email. Handles per source are progressively enriched.
# A record is `pending` (known, not usable) until an admin onboards it `active`.
IDENTITY_PENDING = "pending"
IDENTITY_ACTIVE = "active"
IDENTITY_DISABLED = "disabled"
IDENTITY_STATUSES = (IDENTITY_PENDING, IDENTITY_ACTIVE, IDENTITY_DISABLED)

# The sources a handle can come from. `slack` handles are per-workspace (a nested
# {team_id: user_id} map); the rest are a single string handle.
IDENTITY_SOURCES = ("github", "asana", "slack", "sdlc")

# User-onboarding request lifecycle (spec §16.4) — filed on first touch from an
# unknown/pending sender; an admin approves (→ identity active + group assign) or
# denies. One request per identity (reply-every-time, request-once).
USER_REQ_PENDING = "pending"
USER_REQ_APPROVED = "approved"
USER_REQ_DENIED = "denied"
USER_REQ_STATUSES = (USER_REQ_PENDING, USER_REQ_APPROVED, USER_REQ_DENIED)

# Notification tiers (spec §18) — actionable (a human should engage: proposal,
# review, question), informative (FYI, no action), error (a flow errored/stopped).
# Only actionable + error carry @mentions.
NOTIF_TIER_ACTIONABLE = "actionable"
NOTIF_TIER_INFORMATIVE = "informative"
NOTIF_TIER_ERROR = "error"
NOTIF_TIERS = (NOTIF_TIER_ACTIONABLE, NOTIF_TIER_INFORMATIVE, NOTIF_TIER_ERROR)
# Tiers that @mention the resolved person (spec §18.4). Kept here so the fan-out
# and the config UI agree on which tiers connect people vs. stay silent.
NOTIF_MENTION_TIERS = (NOTIF_TIER_ACTIONABLE, NOTIF_TIER_ERROR)

# A permission-group / notification-subscription id shares the safe-slug shape of
# an agent id (lowercase, letter-first, [a-z0-9-], 2-64) — it becomes a Cedar
# group literal and a DynamoDB pk suffix, so keep it injection-proof.
_GROUP_ID_RE = _AGENT_ID_RE

_table = None


def _get_table():
    global _table
    if _table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


# The GSI (template.yaml FleetConfigTable) that indexes every row's `kind`
# attribute (partition) by `pk` (sort). Every "all rows of one kind" read goes
# through it as a bounded Query instead of a full-table Scan + FilterExpression.
KIND_INDEX = "kind-index"


def _query_kind(kind: str, *, pk_prefix: str | None = None) -> list[dict]:
    """All rows of one ``kind`` via the kind-index, draining every page.

    ``pk_prefix`` (optional) range-narrows on the index sort key (``pk``) so e.g.
    one workspace's ``slack_channel#<team>#…`` rows are read without touching the
    other teams'. This is the single place the index name + paging live so a
    change to either is one edit, not a hand-copied loop per lister that could
    drift and silently drop rows past the first 1 MB page.
    """
    table = _get_table()
    rows: list[dict] = []
    start_key = None
    while True:
        kwargs: dict = {
            "IndexName": KIND_INDEX,
            "KeyConditionExpression": "#k = :k",
            "ExpressionAttributeNames": {"#k": "kind"},
            "ExpressionAttributeValues": {":k": kind},
        }
        if pk_prefix:
            kwargs["KeyConditionExpression"] = "#k = :k AND begins_with(pk, :p)"
            kwargs["ExpressionAttributeValues"][":p"] = pk_prefix
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.query(**kwargs)
        rows.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    return rows


def _normalize_repo(repo: str) -> str:
    """Canonical repo form: trimmed + lowercased. GitHub owner/repo is
    case-insensitive, so we store one canonical casing. This keeps every
    downstream consumer in agreement — the pk, the dispatch allowlist
    (fleet_config casefolds its lookups), and the Cedar policy literals
    (fleet_policy compares case-sensitively) all see the same lowercase string,
    instead of the policy pinning the admin's typed casing while dispatch
    casefolds."""
    return repo.strip().casefold()


def _repo_pk(repo: str) -> str:
    return f"{_REPO_PK_PREFIX}{_normalize_repo(repo)}"


def _owner_of(repo: str) -> str:
    """The owner half of a normalized 'owner/repo'."""
    return _normalize_repo(repo).split("/", 1)[0]


def _owner_pk(owner: str) -> str:
    return f"{_OWNER_PK_PREFIX}{owner.strip().casefold()}"


# --- reads -------------------------------------------------------------------


def get_settings() -> dict:
    """Fleet settings, with defaults when unset. New fleets start unrestricted
    (any onboarded repo allowed) until an admin turns restriction on."""
    resp = _get_table().get_item(Key={"pk": _SETTINGS_PK})
    item = resp.get("Item") or {}
    return {"restrict_repos": bool(item.get("restrict_repos", False))}


def list_repos() -> list[dict]:
    """All onboarded repo records, newest first (by onboarded_at)."""
    repos = _query_kind("repo")
    repos.sort(key=lambda r: r.get("onboarded_at", 0), reverse=True)
    return repos


def get_repo(repo: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _repo_pk(repo)})
    return resp.get("Item")


# --- writes ------------------------------------------------------------------


def put_repo(
    repo: str,
    *,
    enabled: bool = True,
    multi_repo_eligible: bool = True,
    co_repo_mode: str = CO_REPO_ISOLATED,
    repo_group: str | None = None,
    onboarded_by: str = "",
    status: str = "pending",
    installation_id: int | None = None,
    install_verified_at: int | None = None,
) -> dict:
    """Create/replace a repo record. ``status`` defaults to ``pending``; the
    admin onboarding flow instead writes ``active`` and rolls back to ``pending``
    if the Gateway policy sync fails — because ``allowed_repos()`` only returns
    ``active`` rows, so syncing while ``pending`` would omit the very repo being
    onboarded. See admin.py's POST /admin/repos.

    ``co_repo_mode`` / ``repo_group`` declare which OTHER repos a dispatch that
    originates HERE may reach (see module docstring + ``coreachable_repos``). The
    default is ``isolated`` (only itself) — a repo becomes cross-repo-capable only
    when an admin explicitly opts it into a group or ``all``.

    ``installation_id`` / ``install_verified_at`` are set once onboarding has
    verified the GitHub App is installed on the repo's owner and can reach the
    repo. They're denormalized onto the repo row for convenience; the per-owner
    install record (put_installation) is the source of truth."""
    mode = co_repo_mode if co_repo_mode in CO_REPO_MODES else CO_REPO_ISOLATED
    normalized = _normalize_repo(repo)
    item = {
        "pk": _repo_pk(normalized),
        "kind": "repo",
        "repo": normalized,
        "owner": _owner_of(normalized),
        "enabled": bool(enabled),
        "multi_repo_eligible": bool(multi_repo_eligible),
        "co_repo_mode": mode,
        "onboarded_by": onboarded_by,
        "onboarded_at": int(time.time()),
        "status": status,
    }
    # Store the group label only in group mode, normalized like a repo half so it
    # can't smuggle Cedar metacharacters (it's compared, never rendered into
    # policy, but keep the invariant tight).
    if mode == CO_REPO_GROUP and repo_group:
        item["repo_group"] = repo_group.strip().casefold()
    if installation_id is not None:
        item["installation_id"] = int(installation_id)
    if install_verified_at is not None:
        item["install_verified_at"] = int(install_verified_at)
    _get_table().put_item(Item=item)
    return item


# --- per-owner GitHub App installation records -------------------------------


def get_installation(owner: str) -> dict | None:
    """The GitHub App install record for ``owner``, or None if not onboarded."""
    resp = _get_table().get_item(Key={"pk": _owner_pk(owner)})
    return resp.get("Item")


def put_installation(owner: str, *, owner_type: str, installation_id: int) -> dict:
    """Record (create/replace) the App installation for an owner. One install
    serves all of that owner's repos."""
    item = {
        "pk": _owner_pk(owner),
        "kind": "install",
        "owner": owner.strip().casefold(),
        "owner_type": owner_type,
        "installation_id": int(installation_id),
        "install_verified_at": int(time.time()),
    }
    _get_table().put_item(Item=item)
    return item


def delete_installation(owner: str) -> bool:
    """Remove an owner's install record. Returns True if one existed."""
    resp = _get_table().delete_item(
        Key={"pk": _owner_pk(owner)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


def owner_has_repos(owner: str) -> bool:
    """Whether any onboarded repo still belongs to ``owner`` — used to decide
    whether deleting a repo should also drop the shared per-owner install
    record (only once the owner's LAST repo is gone)."""
    target = owner.strip().casefold()
    return any(r.get("owner") == target for r in list_repos())


def set_repo_status(repo: str, status: str) -> None:
    _get_table().update_item(
        Key={"pk": _repo_pk(repo)},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
        ConditionExpression="attribute_exists(pk)",
    )


def delete_repo(repo: str) -> bool:
    """Delete a repo row. Returns True if a row was actually removed, False if
    no such repo existed (DynamoDB delete_item is idempotent, so we ask for the
    old item to distinguish a real delete from a no-op — the admin API reports
    the difference instead of always claiming success)."""
    resp = _get_table().delete_item(Key={"pk": _repo_pk(repo)}, ReturnValues="ALL_OLD")
    return bool(resp.get("Attributes"))


def put_settings(*, restrict_repos: bool) -> dict:
    item = {
        "pk": _SETTINGS_PK,
        "kind": "settings",
        "restrict_repos": bool(restrict_repos),
    }
    _get_table().put_item(Item=item)
    return item


# --- capabilities (UI-onboarded agents) --------------------------------------


def valid_agent_id(agent_id: str) -> bool:
    """Whether ``agent_id`` is a safe, canonical capability id (see _AGENT_ID_RE)."""
    return bool(_AGENT_ID_RE.match(agent_id or ""))


def _capability_pk(agent_id: str) -> str:
    return f"{_CAPABILITY_PK_PREFIX}{agent_id}"


def list_capabilities() -> list[dict]:
    """All onboarded capability records, newest first (by onboarded_at). A dropped
    row would be silently missing from both the admin listing and the rendered
    registry, so the kind-index Query drains every page."""
    caps = _query_kind("capability")
    caps.sort(key=lambda c: c.get("onboarded_at", 0), reverse=True)
    return caps


def get_capability(agent_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _capability_pk(agent_id)})
    return resp.get("Item")


def put_capability(
    agent_id: str,
    *,
    description: str = "",
    aliases: list[str] | None = None,
    triggers: dict | None = None,
    limits: dict | None = None,
    env: dict | None = None,
    tool_grants: list[str] | None = None,
    system_prompt: str | None = None,
    requirements: list[str] | None = None,
    skills: list[dict] | None = None,
    enabled: bool = True,
    status: str | None = None,
    onboarded_by: str = "",
    builtin: bool | None = None,
) -> dict:
    """Create/replace a capability record's DECLARATIVE fields (the parts an admin
    edits). Deploy-state fields — image_tag, runtime_arn, build_id, status_detail —
    are NOT set here; they're written by the build/runtime steps via
    set_capability_deploy_state / set_capability_status, so re-submitting the
    onboarding form can't clobber a live runtime's ARN. Preserves onboarded_at +
    existing deploy state on an update; bumps updated_at.

    ``status`` is LIFECYCLE state owned by set_capability_status, not a declarative
    field: on a NEW row it defaults to ``pending``; on an EDIT it is preserved
    (an admin editing an active capability's aliases must not knock it back to
    pending and out of the registry). Pass it explicitly only to force a state.

    ``builtin`` marks a seeded system agent (fixed config, enable/disable-only,
    undeletable — spec §3.1). It is a PROVENANCE flag, not admin-editable: passing
    ``None`` (the admin-API path) preserves the existing row's value and defaults a
    new row to ``False``; only the deploy-time seeder passes ``True``. So the
    onboard form can never turn a custom agent into a built-in or vice versa.

    Raises ValueError on an invalid agent_id — it flows into resource names and
    filesystem paths downstream, so it's validated at the store boundary."""
    if not valid_agent_id(agent_id):
        raise ValueError(
            f"invalid agent_id {agent_id!r} — must match {_AGENT_ID_RE.pattern} "
            f"(lowercase, starts with a letter, [a-z0-9-], 2-64 chars)"
        )
    now = int(time.time())
    existing = get_capability(agent_id) or {}
    if status is None:
        status = existing.get("status", CAP_PENDING)
    if status not in CAP_STATUSES:
        raise ValueError(f"invalid capability status {status!r}")
    # Normalize + de-dup aliases, order-preserving. The router lowercases the
    # mention before matching (resolve_agent), so aliases must be lowercase; dupes
    # are harmless there but pointless to store.
    norm_aliases: list[str] = []
    for a in aliases or []:
        low = a.strip().lower()
        if low and low not in norm_aliases:
            norm_aliases.append(low)
    # builtin is provenance, preserved across edits: None (admin path) keeps the
    # existing value / defaults new rows False; only the seeder passes True.
    if builtin is None:
        builtin = bool(existing.get("builtin", False))
    item = {
        "pk": _capability_pk(agent_id),
        "kind": "capability",
        "agent_id": agent_id,
        "description": description,
        "aliases": norm_aliases,
        "triggers": triggers or {},
        "limits": limits or {},
        "env": env or {},
        "tool_grants": list(tool_grants or []),
        # None means "not supplied — preserve existing"; "" is a deliberate clear.
        # (``prompt or existing`` could never store an empty prompt.)
        "system_prompt": system_prompt if system_prompt is not None else existing.get("system_prompt", ""),
        "requirements": list(requirements) if requirements is not None else list(existing.get("requirements", [])),
        "skills": list(skills) if skills is not None else list(existing.get("skills", [])),
        "review_status": existing.get("review_status", "approved"),
        "enabled": bool(enabled),
        "status": status,
        "builtin": bool(builtin),
        "onboarded_by": onboarded_by or existing.get("onboarded_by", ""),
        "onboarded_at": existing.get("onboarded_at", now),
        "updated_at": now,
    }
    # Carry forward deploy state the UI form doesn't own.
    for k in ("image_tag", "runtime_arn", "build_id", "status_detail"):
        if k in existing:
            item[k] = existing[k]
    _get_table().put_item(Item=item)
    return item


def set_capability_status(agent_id: str, status: str, *, detail: str = "") -> None:
    """Advance a capability's lifecycle status (build/runtime steps call this).
    ``detail`` records the last transition reason (e.g. a build failure message)
    for the admin UI. Condition-guarded so a status write to a deleted row fails
    loudly rather than resurrecting it."""
    if status not in CAP_STATUSES:
        raise ValueError(f"invalid capability status {status!r}")
    _get_table().update_item(
        Key={"pk": _capability_pk(agent_id)},
        UpdateExpression="SET #s = :s, status_detail = :d, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":d": detail,
            ":u": int(time.time()),
        },
        ConditionExpression="attribute_exists(pk)",
    )


def set_review_status(agent_id: str, review_status: str) -> None:
    """Set a capability's approval-gate review status (approved | pending_review).
    Used by the admin API to park a custom agent for second-admin approval when
    the RequireAgentApproval gate is on (spec §7.5)."""
    _get_table().update_item(
        Key={"pk": _capability_pk(agent_id)},
        UpdateExpression="SET review_status = :r, updated_at = :u",
        ExpressionAttributeValues={":r": review_status, ":u": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )


def set_capability_deploy_state(
    agent_id: str,
    *,
    image_tag: str | None = None,
    runtime_arn: str | None = None,
    build_id: str | None = None,
) -> None:
    """Record deploy artifacts produced by the build/runtime steps, without
    touching the declarative fields. Only the provided fields are written."""
    sets, names, values = ["updated_at = :u"], {}, {":u": int(time.time())}
    for attr, val in (
        ("image_tag", image_tag),
        ("runtime_arn", runtime_arn),
        ("build_id", build_id),
    ):
        if val is not None:
            placeholder = f":{attr}"
            sets.append(f"#{attr} = {placeholder}")
            names[f"#{attr}"] = attr
            values[placeholder] = val
    _get_table().update_item(
        Key={"pk": _capability_pk(agent_id)},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names or None,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(pk)",
    )


class BuiltinCapabilityError(Exception):
    """Raised when a delete targets a seeded built-in capability. Built-ins are
    system agents (spec §3.1) — enable/disable only, never removed. The admin API
    maps this to a 409."""


def delete_capability(agent_id: str) -> bool:
    """Delete a capability row. Returns True if a row was removed. Does NOT tear
    down the runtime/role/image — the admin API handles that lifecycle before
    removing the row, so a bare delete here never orphans the record ahead of its
    resources.

    Refuses a built-in (``BuiltinCapabilityError``): a system agent is fixed and
    undeletable; disable it instead. This is the store-level backstop to the admin
    API's own 409 guard (defense in depth — a non-API caller can't delete one)."""
    existing = get_capability(agent_id)
    if existing and existing.get("builtin"):
        raise BuiltinCapabilityError(
            f"{agent_id} is a built-in system agent and cannot be deleted; disable it instead"
        )
    resp = _get_table().delete_item(
        Key={"pk": _capability_pk(agent_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


def render_registry() -> dict:
    """Render the Dispatch Router registry from the active capability rows.

    Reproduces exactly the shape the router consumes from SSM (see
    infra/dispatch/router.py load_registry / resolve_agent): a top-level
    ``{"agents": {agent_id: {description, runtime_arn, aliases, triggers,
    limits}}}`` map. This REPLACES .dispatch/agents.yaml + scripts/sync_registry.py
    — the registry is now generated from the table the admin UI writes, so
    onboarding an agent needs no code/YAML edit.

    Note there is NO ``authorization`` block: WHO may trigger an agent is decided
    by the Cedar ``SdlcTrigger`` policy store (trigger rules), not a per-capability
    allowlist carried in the registry. The registry answers "which agent does this
    mention resolve to, and where is its runtime" — not "may this sender use it".

    Routability keys on "has a working runtime + enabled + not disabled", NOT on
    status == active: a capability is included iff it is ``enabled``, not
    ``disabled``, and has a resolved ``runtime_arn``. This is deliberate so a
    WEEKLY REBUILD (or any re-deploy) of a live agent never drops it from dispatch:
    while it is ``building`` — and even if that rebuild lands in ``failed`` — its
    previous runtime is still up and serving, so it stays routable on its existing
    ARN. A brand-new agent that has never deployed has no ``runtime_arn`` yet, so a
    still-building or failed FIRST onboard is correctly excluded (the router would
    otherwise resolve a mention to a runtime that isn't up)."""
    agents = {}
    for cap in list_capabilities():
        if not cap.get("enabled") or cap.get("status") == CAP_DISABLED:
            continue
        arn = cap.get("runtime_arn")
        if not arn:
            continue
        agents[cap["agent_id"]] = {
            "description": cap.get("description", ""),
            "runtime_arn": arn,
            "aliases": list(cap.get("aliases", [])),
            "triggers": dict(cap.get("triggers", {})),
            "limits": dict(cap.get("limits", {})),
        }
    return {"agents": agents}


def publish_registry() -> dict:
    """Render the registry from the active capabilities and write it to the SSM
    parameter the Dispatch Router reads (REGISTRY_PARAM). Returns the rendered
    dict. This is what scripts/sync_registry.py used to do on deploy — now it runs
    on every capability change, so the router picks up onboard/enable/disable
    without a redeploy.

    Written as YAML because router.load_registry() does ``yaml.safe_load`` on the
    parameter value. json.dumps output is valid YAML (YAML is a JSON superset), so
    we emit compact JSON via the stdlib and avoid a PyYAML dependency in the write
    path — the router still parses it with yaml.safe_load. DynamoDB Decimals from
    limits are coerced to plain numbers so the value is JSON-serializable.

    Capabilities are unbounded (UI-onboarded), so the rendered registry can grow
    past the SSM Standard-tier 4KB value cap. We select the Advanced tier once the
    value crosses that limit so a large fleet's registry still publishes instead
    of raising ValidationException. (Standard is free; Advanced is used only when
    needed.)"""
    registry = render_registry()
    value = json.dumps(registry, default=_decimal_default)
    # SSM Standard tier caps a parameter value at 4096 bytes; Advanced allows 8KB.
    tier = "Standard" if len(value.encode("utf-8")) <= 4096 else "Advanced"
    ssm = boto3.client("ssm")
    ssm.put_parameter(
        Name=os.environ["REGISTRY_PARAM"],
        Value=value,
        Type="String",
        Tier=tier,
        Overwrite=True,
    )
    return registry


def _decimal_default(o):
    """JSON encoder hook for the DynamoDB Decimal values that reach the registry
    via a capability's ``limits`` (max_concurrent, timeout_minutes, token budget).
    Integral → int, else float — matches http_responses._DecimalEncoder."""
    from decimal import Decimal

    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


# Runtime env keys a capability may NOT set — the fleet-wide security gates.
# BEDROCK_GUARDRAIL_* attaches the prompt-injection guardrail to every model call
# (threat-model T-1/2/3); GATEWAY_MCP_URL points the agent at the Cedar-enforced
# tool-call gateway (the agent refuses to start without it). Letting a capability's
# own env override these would silently disable the guardrail or repoint the tool
# boundary — so the base values ALWAYS win for these keys, and the admin API
# rejects them outright (see admin._validate_capability_body). Kept here so both
# the API validation and the merge agree on one list.
RESERVED_ENV_KEYS = frozenset(
    {"BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "GATEWAY_MCP_URL"}
)


# Runtime env the generic base agent (agents/_base/agent.py) reads to identify
# itself and locate its prompt + skills (spec §4). Derived from the capability
# row, so the ONE generic image behaves as any authored agent with no per-agent
# code. Built-in agents ship their own agent.py and ignore these — injecting them
# is harmless (a built-in doesn't read AGENT_ID/SYSTEM_PROMPT/SKILLS_DIR), and it
# keeps one env-assembly path for the whole fleet. SKILLS_DIR is fixed to the
# container path the deployer syncs skill packages into.
SKILLS_MOUNT_DIR = "/app/skills"

# Env keys the base agent derives from the capability ROW, never from a
# capability's free-form ``env``. AGENT_ID is the identity the Gateway's per-agent
# Cedar policy keys on, so an authored env that set it could assume another
# agent's tool grants — these are computed here and rejected at the admin API.
BASE_AGENT_ENV_KEYS = frozenset({"AGENT_ID", "SYSTEM_PROMPT", "SKILLS_DIR"})


def capability_env_pairs(cap: dict, base_env: dict[str, str]) -> dict[str, str]:
    """The full runtime environment for a capability: the fleet-wide base env
    (guardrail id/version + gateway URL — the hard gates every agent needs) merged
    with the generic base agent's identity/prompt/skills vars (§4) and the
    capability's own ``env`` (e.g. Asana GIDs).

    The base env ALWAYS wins for RESERVED_ENV_KEYS: even though the admin API
    rejects a capability that sets them, this merge re-enforces it as defense in
    depth (e.g. a row written before this rule, or by a future non-API path) so a
    capability can never disable the guardrail or repoint the gateway.

    Returned as a plain dict; the caller renders the CSV create/update-agent-runtime
    wants. Kept here so the onboard path and the weekly rebuild share one
    definition of an agent's environment."""
    merged = dict(base_env)
    # Generic-base-agent identity/prompt/skills (§4). Set BEFORE the capability's
    # own env loop so an authored env can't shadow them; RESERVED_ENV_KEYS below
    # still can't be touched either way. SYSTEM_PROMPT defaults to empty (the base
    # agent supplies a generic fallback) so a built-in with no prompt is fine.
    merged["AGENT_ID"] = cap.get("agent_id", "")
    merged["SYSTEM_PROMPT"] = cap.get("system_prompt", "") or ""
    merged["SKILLS_DIR"] = SKILLS_MOUNT_DIR
    # Skill delivery (§6.2): AgentCore runtimes are immutable containers, so the
    # base agent pulls its referenced skill packages from S3 on startup. We inject
    # the manifest (name/s3_prefix/sha256) as env; the base agent syncs + verifies
    # each into SKILLS_DIR before wiring AgentSkills. Only emitted when the row has
    # skills AND SKILLS_BUCKET is in the base env (deployer wires it from the
    # stack) — otherwise there's nothing to sync and the var stays absent.
    skills = cap.get("skills") or []
    if skills and base_env.get("SKILLS_BUCKET"):
        manifest = [
            {"name": s.get("name", ""), "s3_prefix": s.get("s3_prefix", ""),
             "sha256": s.get("sha256", "")}
            for s in skills
            if s.get("s3_prefix")
        ]
        if manifest:
            merged["SKILLS_MANIFEST"] = json.dumps(manifest, separators=(",", ":"))
    for k, v in (cap.get("env") or {}).items():
        # RESERVED_ENV_KEYS: base gate wins. BASE_AGENT_ENV_KEYS: derived from the
        # row, never from free-form env — AGENT_ID especially IS the identity the
        # Gateway's per-agent Cedar policy keys on, so letting a capability set it
        # via env would let a custom agent assume another agent's tool grants.
        if k in RESERVED_ENV_KEYS or k in BASE_AGENT_ENV_KEYS:
            continue  # derived/gated — never overridable by authored env
        merged[k] = str(v)
    # Re-assert the base gates last in case the loop above was bypassed.
    for k in RESERVED_ENV_KEYS:
        if k in base_env:
            merged[k] = base_env[k]
    return merged


# --- derived -----------------------------------------------------------------


def _eligible_active_repos() -> list[dict]:
    """Active, enabled, multi-repo-eligible repo records (the cross-repo pool)."""
    return [
        r
        for r in list_repos()
        if r.get("enabled")
        and r.get("multi_repo_eligible")
        and r.get("status") == "active"
    ]


def allowed_repos() -> list[str]:
    """The full set of repos that may participate in cross-repo tool actions —
    rendered into the Gateway Cedar policy. Enabled + eligible + active. This is
    the master allowlist; the per-origin ``coreachable_repos`` narrows WITHIN it
    which of these a given dispatch may actually reach."""
    return [r["repo"] for r in _eligible_active_repos()]


def coreachable_repos(origin: str) -> list[str]:
    """The repos a dispatch that ORIGINATES in ``origin`` (owner/repo) may operate
    on — the enforced "approved to run with" set. Always includes ``origin`` when
    it's an onboarded, enabled, active repo; adds others per ``origin``'s
    ``co_repo_mode``:

      - isolated → just ``origin``.
      - group    → ``origin`` + every eligible repo sharing ``origin``'s
                   ``repo_group`` (mutual — both must be group-mode + same label).
      - all      → ``origin`` + every eligible repo in the fleet (any owner/org).

    Returns lowercased ``owner/repo`` strings, sorted, ``origin`` first. An origin
    that isn't onboarded/enabled/active returns ``[]`` (fails closed). This spans
    owners: a group or ``all`` may mix individual and org repos freely — the token
    minter scopes the credential to exactly this set regardless of owner."""
    norm = _normalize_repo(origin)
    rec = get_repo(norm)
    if not rec or not rec.get("enabled") or rec.get("status") != "active":
        return []

    reachable = {norm}
    # An origin that isn't itself eligible can still act on itself, but can never
    # reach out (cross-repo requires eligibility on the origin).
    if rec.get("multi_repo_eligible"):
        mode = rec.get("co_repo_mode", CO_REPO_ISOLATED)
        if mode == CO_REPO_ALL:
            reachable.update(r["repo"] for r in _eligible_active_repos())
        elif mode == CO_REPO_GROUP:
            group = rec.get("repo_group")
            if group:
                reachable.update(
                    r["repo"]
                    for r in _eligible_active_repos()
                    if r.get("co_repo_mode") == CO_REPO_GROUP
                    and r.get("repo_group") == group
                )
    return sorted(reachable, key=lambda r: (r != norm, r))


# --- Slack workspaces --------------------------------------------------------
# Multi-workspace: an admin onboards one or more workspaces, each with its own
# bot token + signing secret (stored as SSM SecureStrings — NEVER on the row).
# The Slack receiver selects the secret by the inbound team_id, so a workspace
# row is the source of truth for "is this workspace onboarded + enabled, and
# where are its secrets" (spec §4.1).


def valid_slack_team(team_id: str) -> bool:
    return bool(_SLACK_TEAM_RE.match(team_id or ""))


def valid_slack_channel(channel_id: str) -> bool:
    return bool(_SLACK_CHANNEL_RE.match(channel_id or ""))


def _slack_ws_pk(team_id: str) -> str:
    return f"{_SLACK_WS_PK_PREFIX}{team_id}"


def _slack_bot_token_param(stage: str, team_id: str) -> str:
    """Canonical SSM SecureString path for a workspace's bot token (per-workspace
    — the receiver/reply reads it by team id). The SIGNING secret is app-level
    (one per Slack app, at /sdlc-agents/<stage>/slack/signing-secret) and is not
    recorded per workspace. Kept here so the admin API + receiver agree on the
    layout."""
    return f"/sdlc-agents/{stage}/slack/{team_id}/bot-token"


def list_slack_workspaces() -> list[dict]:
    """All onboarded Slack workspace records, newest first (kind-index Query,
    drained across pages so a large fleet never drops a workspace)."""
    rows = _query_kind("slack_workspace")
    rows.sort(key=lambda r: r.get("onboarded_at", 0), reverse=True)
    return rows


def get_slack_workspace(team_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _slack_ws_pk(team_id)})
    return resp.get("Item")


def put_slack_workspace(
    team_id: str,
    *,
    team_name: str = "",
    stage: str,
    enabled: bool = True,
    default_channel_policy: str = CHANNEL_POLICY_ALLOWLIST,
    onboarded_by: str = "",
    status: str = SLACK_WS_PENDING,
) -> dict:
    """Create/replace a Slack workspace record. Raises ValueError on a malformed
    team_id (it flows into the SSM bot-token path + the Cedar workspace literal)
    or an invalid channel policy / status. The bot-token param PATH is derived +
    stored on the row; the token VALUE is written out-of-band (bootstrap_slack.py).
    The signing secret is app-level, not per-workspace, so it isn't on the row."""
    if not valid_slack_team(team_id):
        raise ValueError(
            f"invalid Slack team id {team_id!r} — must match {_SLACK_TEAM_RE.pattern}"
        )
    if default_channel_policy not in CHANNEL_POLICIES:
        raise ValueError(f"invalid channel policy {default_channel_policy!r}")
    if status not in SLACK_WS_STATUSES:
        raise ValueError(f"invalid workspace status {status!r}")
    existing = get_slack_workspace(team_id) or {}
    now = int(time.time())
    item = {
        "pk": _slack_ws_pk(team_id),
        "kind": "slack_workspace",
        "team_id": team_id,
        "team_name": team_name or existing.get("team_name", ""),
        "enabled": bool(enabled),
        "default_channel_policy": default_channel_policy,
        "bot_token_param": _slack_bot_token_param(stage, team_id),
        "onboarded_by": onboarded_by or existing.get("onboarded_by", ""),
        "onboarded_at": existing.get("onboarded_at", now),
        "updated_at": now,
        "status": status,
    }
    _get_table().put_item(Item=item)
    return item


def set_slack_workspace_status(team_id: str, status: str) -> None:
    if status not in SLACK_WS_STATUSES:
        raise ValueError(f"invalid workspace status {status!r}")
    _get_table().update_item(
        Key={"pk": _slack_ws_pk(team_id)},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )


def delete_slack_workspace(team_id: str) -> bool:
    """Delete a workspace row. Returns True if one existed. Does NOT delete the
    channel rows or the SSM secrets — the admin API handles that lifecycle so a
    bare delete here never orphans the record ahead of its dependents."""
    resp = _get_table().delete_item(
        Key={"pk": _slack_ws_pk(team_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Slack channel policy ----------------------------------------------------
# Per-workspace allow/deny rows. Interpreted against the workspace's
# default_channel_policy (allowlist ⇒ default-deny per channel; denylist ⇒
# default-allow). Both are expressible in Cedar (spec §4.2 / §5.2).


def _slack_chan_pk(team_id: str, channel_id: str) -> str:
    return f"{_SLACK_CHAN_PK_PREFIX}{team_id}#{channel_id}"


def list_channels(team_id: str) -> list[dict]:
    """All channel-policy rows for a workspace — a kind-index Query range-narrowed
    to this team's ``slack_chan#<team>#…`` pk prefix, so it reads only this
    workspace's channels rather than every team's."""
    prefix = f"{_SLACK_CHAN_PK_PREFIX}{team_id}#"
    rows = _query_kind("slack_channel", pk_prefix=prefix)
    rows.sort(key=lambda r: r.get("channel_id", ""))
    return rows


def put_channel_policy(
    team_id: str,
    channel_id: str,
    *,
    mode: str,
    channel_name: str = "",
    note: str = "",
    created_by: str = "",
) -> dict:
    """Create/replace a channel allow|deny row. Raises ValueError on a malformed
    team/channel id (both flow into Cedar literals) or an invalid mode."""
    if not valid_slack_team(team_id):
        raise ValueError(f"invalid Slack team id {team_id!r}")
    if not valid_slack_channel(channel_id):
        raise ValueError(
            f"invalid Slack channel id {channel_id!r} — must match {_SLACK_CHANNEL_RE.pattern}"
        )
    if mode not in CHANNEL_MODES:
        raise ValueError(f"invalid channel mode {mode!r}")
    item = {
        "pk": _slack_chan_pk(team_id, channel_id),
        "kind": "slack_channel",
        "team_id": team_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "mode": mode,
        "note": note,
        "created_by": created_by,
        "created_at": int(time.time()),
    }
    _get_table().put_item(Item=item)
    return item


def delete_channel_policy(team_id: str, channel_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _slack_chan_pk(team_id, channel_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Trigger-authz rules -----------------------------------------------------
# The admin-authored unit of "WHO may trigger which agent" — subject (user or
# group) → agent → workspace, permit or forbid. These rows are pure DATA: the
# Dispatch Router reads them and assembles the granted/denied principal + group
# sets as Cedar ENTITY ATTRIBUTES for a fixed AVP policy set (spec §5). Granting
# access is therefore a plain DynamoDB write — NO per-rule AVP CreatePolicy, so
# the policy count stays constant no matter how many users/rules exist (this is
# the model AVP is built for; a policy-per-user would be an anti-pattern).
#
# The "WHERE" axis (which channels) is a SEPARATE concern carried by the
# slack_channel rows above — a trigger rule is WHO-only, so the two axes compose
# without overlap. Scoped to one connector so each connector sub-page manages
# only its own rules (per-connector rules).


def _trigger_rule_pk(rule_id: str) -> str:
    return f"{_TRIGGER_RULE_PK_PREFIX}{rule_id}"


def valid_rule_shape(
    *, connector: str, subject_type: str, effect: str
) -> str | None:
    """Return an error string if the rule's enum fields are invalid, else None."""
    if connector not in TRIGGER_CONNECTORS:
        return f"invalid connector {connector!r}"
    if subject_type not in RULE_SUBJECT_TYPES:
        return f"invalid subject_type {subject_type!r}"
    if effect not in RULE_EFFECTS:
        return f"invalid effect {effect!r}"
    return None


def list_trigger_rules(connector: str | None = None) -> list[dict]:
    """All trigger rules, or just one connector's (per-connector listing). Newest
    first. kind-index Query drains every page so no rule is silently dropped."""
    rows = _query_kind("trigger_rule")
    if connector is not None:
        rows = [r for r in rows if r.get("connector") == connector]
    rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return rows


def get_trigger_rule(rule_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _trigger_rule_pk(rule_id)})
    return resp.get("Item")


def put_trigger_rule(
    *,
    connector: str,
    subject_type: str,
    subject_id: str,
    agent_id: str = "*",
    workspace: str = "*",
    effect: str = RULE_PERMIT,
    created_by: str = "",
    rule_id: str | None = None,
) -> dict:
    """Create (or replace, when rule_id is given) a WHO trigger rule row.

    A rule grants (permit) or blocks (forbid) a subject — a ``user`` (principal
    id like ``slack:T:U``) or a ``group`` — on an agent (``"*"`` = any) within a
    workspace (``"*"`` = any). The channel/"where" axis is NOT here; it lives on
    the slack_channel rows. Validates the enum fields and the ids that become
    Cedar entity values: agent_id (unless ``"*"``) against _AGENT_ID_RE, a
    concrete workspace against the Slack team shape.

    Pure data: the router reads these rows into Cedar entity attributes for a
    fixed policy set (no per-rule AVP policy)."""
    err = valid_rule_shape(
        connector=connector, subject_type=subject_type, effect=effect
    )
    if err:
        raise ValueError(err)
    if not (subject_id or "").strip():
        raise ValueError("subject_id is required")
    if agent_id != "*" and not valid_agent_id(agent_id):
        raise ValueError(f"invalid agent_id {agent_id!r}")
    if workspace != "*" and not valid_slack_team(workspace):
        raise ValueError(f"invalid workspace {workspace!r}")
    if rule_id is None:
        import uuid

        rule_id = str(uuid.uuid4())
    existing = get_trigger_rule(rule_id) or {}
    item = {
        "pk": _trigger_rule_pk(rule_id),
        "kind": "trigger_rule",
        "rule_id": rule_id,
        "connector": connector,
        "subject_type": subject_type,
        "subject_id": subject_id.strip(),
        "agent_id": agent_id,
        "workspace": workspace,
        "effect": effect,
        "created_by": created_by or existing.get("created_by", ""),
        "created_at": existing.get("created_at", int(time.time())),
    }
    _get_table().put_item(Item=item)
    return item


def delete_trigger_rule(rule_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _trigger_rule_pk(rule_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Channel onboarding requests ---------------------------------------------
# A Slack user runs a slash command (/sdlc-onboard-channel) to REQUEST their channel
# be onboarded for specific agents (spec §4.5). The request is captured here
# ``pending``; an admin approves or denies it in the Connectors panel. Approval
# is the ONLY path that grants access — a request never self-applies. The
# requester is the immutable Slack user id (T-4), so an approval is auditable.


def _channel_request_pk(request_id: str) -> str:
    return f"{_CHANNEL_REQUEST_PK_PREFIX}{request_id}"


def list_channel_requests(status: str | None = None) -> list[dict]:
    """All channel onboarding requests, or just those in ``status``. Newest first.
    kind-index Query drains every page so a pending request is never dropped."""
    rows = _query_kind("channel_request")
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return rows


def get_channel_request(request_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _channel_request_pk(request_id)})
    return resp.get("Item")


def put_channel_request(
    *,
    team_id: str,
    channel_id: str,
    channel_name: str = "",
    requested_by: str,
    requested_agents: list[str] | None = None,
    note: str = "",
    request_id: str | None = None,
) -> dict:
    """Record a channel onboarding request (status ``pending``). Validates the
    Slack ids (they become Cedar/allow-row values on approval) and each requested
    agent id. ``requested_agents`` is the scope the user is asking for — ``["*"]``
    or empty means "any agent"; concrete ids are validated. ``requested_by`` is
    the immutable Slack user id of the requester (for the audit trail)."""
    if not valid_slack_team(team_id):
        raise ValueError(f"invalid Slack team id {team_id!r}")
    if not valid_slack_channel(channel_id):
        raise ValueError(f"invalid Slack channel id {channel_id!r}")
    if not (requested_by or "").strip():
        raise ValueError("requested_by is required")
    agents = [a for a in (requested_agents or []) if a and a != "*"]
    for a in agents:
        if not valid_agent_id(a):
            raise ValueError(f"invalid requested agent id {a!r}")
    if request_id is None:
        import uuid

        request_id = str(uuid.uuid4())
    existing = get_channel_request(request_id) or {}
    item = {
        "pk": _channel_request_pk(request_id),
        "kind": "channel_request",
        "request_id": request_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "channel_name": channel_name or existing.get("channel_name", ""),
        "requested_by": requested_by.strip(),
        "requested_agents": agents,  # [] = any agent
        "note": note,
        "status": existing.get("status", CHAN_REQ_PENDING),
        "created_at": existing.get("created_at", int(time.time())),
        "decided_by": existing.get("decided_by", ""),
        "decided_at": existing.get("decided_at"),
    }
    _get_table().put_item(Item=item)
    return item


def resolve_channel_request(
    request_id: str, *, status: str, decided_by: str
) -> dict | None:
    """Mark a request ``approved`` or ``denied`` (records who + when). Returns the
    updated row, or None if the request doesn't exist. Does NOT itself create the
    allow row / grants — the admin API composes approval (put_channel_policy +
    optional put_trigger_rule) with this status write, so the effect is explicit
    and testable. Condition-guarded so a decision on a deleted request fails
    rather than resurrecting it."""
    if status not in (CHAN_REQ_APPROVED, CHAN_REQ_DENIED):
        raise ValueError(f"invalid decision status {status!r}")
    if get_channel_request(request_id) is None:
        return None
    _get_table().update_item(
        Key={"pk": _channel_request_pk(request_id)},
        UpdateExpression="SET #s = :s, decided_by = :b, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":b": decided_by,
            ":t": int(time.time()),
        },
        ConditionExpression="attribute_exists(pk)",
    )
    return get_channel_request(request_id)


def delete_channel_request(request_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _channel_request_pk(request_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Identity map (spec §16) -------------------------------------------------
# The cross-source join: email is the golden id, but records are keyed on a
# synthetic identity_id because a first touch from GitHub/Asana may carry no
# email. Handles are stored under `handles` (slack is a per-workspace map).
# Following the fleet-config convention (single pk, no GSIs — cf. fleet_config /
# trigger_grants), lookups by handle/email are filtered scans over the small
# identity set, cached at the read layer (identity.py) for the dispatch path.


def valid_group_id(group_id: str) -> bool:
    """Whether ``group_id`` is a safe permission-group / subscription slug."""
    return bool(_GROUP_ID_RE.match(group_id or ""))


def _identity_pk(identity_id: str) -> str:
    return f"{_IDENTITY_PK_PREFIX}{identity_id}"


def _norm_email(email: str) -> str:
    """Canonical email form for the golden join (trimmed + lowercased). '' when
    absent — an email-less first touch (GitHub/Asana) is valid; it's filled in
    later when a source reveals it (§16.3)."""
    return (email or "").strip().casefold()


def _handle_key(source: str, handle: str, workspace: str = "") -> str:
    """The namespaced handle string stored on the record + matched on resolve —
    ``github:<login>``, ``asana:<gid>``, ``slack:<team>:<uid>``, ``sdlc:<sub>``.
    Mirrors router.namespaced_principal so a resolved identity's handle key and
    the trigger-authz principal are the same string."""
    source = source.strip().lower()
    handle = handle.strip()
    if source == "slack":
        return f"slack:{workspace.strip()}:{handle}"
    return f"{source}:{handle}"


def list_identities() -> list[dict]:
    """All identity records, newest first. Paged like the other listers so a
    large directory never silently drops a person from the admin view."""
    return _scan_kind("identity", sort_key="created_at")


def get_identity(identity_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _identity_pk(identity_id)})
    return resp.get("Item")


def _rebuild_handle_keys(handles: dict) -> list[str]:
    """Flatten a ``handles`` map into the denormalized ``handle_keys`` list used
    for O(1)-ish handle membership tests. slack is a {team: uid} sub-map."""
    keys: list[str] = []
    for source, val in (handles or {}).items():
        if source == "slack" and isinstance(val, dict):
            for team, uid in val.items():
                if uid:
                    keys.append(_handle_key("slack", str(uid), str(team)))
        elif val:
            keys.append(_handle_key(source, str(val)))
    return keys


def put_identity(
    *,
    identity_id: str | None = None,
    email: str = "",
    display_name: str = "",
    handles: dict | None = None,
    groups: list[str] | None = None,
    verified: dict | None = None,
    status: str = IDENTITY_PENDING,
    onboarded_by: str = "",
    created_from: dict | None = None,
) -> dict:
    """Create or replace an identity record. Generates an ``identity_id`` when
    none is given. Normalizes email, validates group ids, and rebuilds the
    denormalized ``handle_keys``. Preserves ``created_at``/``created_from`` on an
    update. This is the admin proactive-create path (an admin-supplied, trusted
    email); the get-or-create-on-touch enrich path lives on the dispatch side
    (``identity.resolve``), which owns the runtime identity map."""
    if status not in IDENTITY_STATUSES:
        raise ValueError(f"invalid identity status {status!r}")
    for g in groups or []:
        if not valid_group_id(g):
            raise ValueError(f"invalid group id {g!r}")
    if identity_id is None:
        import uuid

        identity_id = str(uuid.uuid4())
    existing = get_identity(identity_id) or {}
    now = int(time.time())
    handles = handles or {}
    item = {
        "pk": _identity_pk(identity_id),
        "kind": "identity",
        "identity_id": identity_id,
        "email": _norm_email(email),
        "display_name": display_name or existing.get("display_name", ""),
        "handles": handles,
        "handle_keys": _rebuild_handle_keys(handles),
        "groups": sorted({*(groups or [])}),
        "verified": verified or existing.get("verified", {}),
        "status": status,
        "onboarded_by": onboarded_by or existing.get("onboarded_by", ""),
        "created_from": created_from or existing.get("created_from", {}),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
        "merged_from": existing.get("merged_from", []),
    }
    _get_table().put_item(Item=item)
    return item


def set_identity_status(identity_id: str, status: str) -> None:
    if status not in IDENTITY_STATUSES:
        raise ValueError(f"invalid identity status {status!r}")
    _get_table().update_item(
        Key={"pk": _identity_pk(identity_id)},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )


def set_identity_groups(identity_id: str, groups: list[str]) -> dict | None:
    """Replace an identity's group membership (the onboarding access step, §17.4).
    Validates each group id. Returns the updated record, or None if absent."""
    for g in groups or []:
        if not valid_group_id(g):
            raise ValueError(f"invalid group id {g!r}")
    if get_identity(identity_id) is None:
        return None
    _get_table().update_item(
        Key={"pk": _identity_pk(identity_id)},
        UpdateExpression="SET #g = :g, updated_at = :u",
        ExpressionAttributeNames={"#g": "groups"},
        ExpressionAttributeValues={":g": sorted({*(groups or [])}), ":u": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )
    return get_identity(identity_id)


def set_identity_verified(identity_id: str, source: str, verified: bool) -> None:
    """Mark one source handle verified/unverified (admin approval is the trust
    event — §16.6). A verified link is authz-load-bearing; unverified is
    display/best-effort only."""
    if source not in IDENTITY_SOURCES:
        raise ValueError(f"invalid identity source {source!r}")
    _get_table().update_item(
        Key={"pk": _identity_pk(identity_id)},
        UpdateExpression="SET verified.#src = :v, updated_at = :u",
        ExpressionAttributeNames={"#src": source},
        ExpressionAttributeValues={":v": bool(verified), ":u": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )


def delete_identity(identity_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _identity_pk(identity_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- User-onboarding requests (spec §16.4) -----------------------------------
# Filed on first touch from an unknown/pending sender. One request per identity;
# an admin approves (→ identity active + group assign) or denies.


def _user_request_pk(request_id: str) -> str:
    return f"{_USER_REQUEST_PK_PREFIX}{request_id}"


def list_user_requests(status: str | None = None) -> list[dict]:
    """All user-onboarding requests, or just those in ``status``, newest first."""
    rows = _scan_kind("user_request", sort_key="created_at")
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    return rows


def get_user_request(request_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _user_request_pk(request_id)})
    return resp.get("Item")


def find_user_request_for_identity(identity_id: str) -> dict | None:
    """The (single) pending request for an identity, if any — enforces
    request-once: a second touch updates/no-ops rather than stacking duplicates."""
    for r in list_user_requests(status=USER_REQ_PENDING):
        if r.get("identity_id") == identity_id:
            return r
    return None


def put_user_request(
    *,
    identity_id: str,
    source: str,
    source_context: dict | None = None,
    proposed_email: str = "",
    display_name: str = "",
    request_id: str | None = None,
) -> dict:
    """Create (or refresh) a pending user-onboarding request for an identity.
    ``source_context`` records where to reply on completion (repo+issue/PR, or
    team+channel+thread_ts). Idempotent per identity via a deterministic id, so a
    repeated first-touch updates the one request instead of stacking duplicates."""
    if source not in IDENTITY_SOURCES:
        raise ValueError(f"invalid identity source {source!r}")
    if not (identity_id or "").strip():
        raise ValueError("identity_id is required")
    if request_id is None:
        request_id = f"user-{identity_id}"
    existing = get_user_request(request_id) or {}
    item = {
        "pk": _user_request_pk(request_id),
        "kind": "user_request",
        "request_id": request_id,
        "identity_id": identity_id,
        "source": source,
        "source_context": source_context or {},
        "proposed_email": _norm_email(proposed_email) or existing.get("proposed_email", ""),
        "display_name": display_name or existing.get("display_name", ""),
        "status": existing.get("status", USER_REQ_PENDING),
        "created_at": existing.get("created_at", int(time.time())),
        "decided_by": existing.get("decided_by", ""),
        "decided_at": existing.get("decided_at"),
    }
    _get_table().put_item(Item=item)
    return item


def resolve_user_request(
    request_id: str, *, status: str, decided_by: str
) -> dict | None:
    """Mark a user request approved/denied (records who + when). Does NOT itself
    flip the identity to active or assign groups — the admin API composes that so
    the effect is explicit + testable (mirrors resolve_channel_request)."""
    if status not in (USER_REQ_APPROVED, USER_REQ_DENIED):
        raise ValueError(f"invalid decision status {status!r}")
    if get_user_request(request_id) is None:
        return None
    _get_table().update_item(
        Key={"pk": _user_request_pk(request_id)},
        UpdateExpression="SET #s = :s, decided_by = :b, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":b": decided_by, ":t": int(time.time())},
        ConditionExpression="attribute_exists(pk)",
    )
    return get_user_request(request_id)


def delete_user_request(request_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _user_request_pk(request_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Permission groups (spec §17) --------------------------------------------
# A named group is metadata only; its ACCESS is expressed as group-scoped
# trigger_rule rows (subject_type=group). Membership lives on the identity
# record. This fills the group axis trigger_authz already evaluates — no Cedar
# policy change, groups are just more grant DATA.


def _perm_group_pk(group_id: str) -> str:
    return f"{_PERM_GROUP_PK_PREFIX}{group_id}"


def list_perm_groups() -> list[dict]:
    return _scan_kind("perm_group", sort_key="created_at")


def get_perm_group(group_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _perm_group_pk(group_id)})
    return resp.get("Item")


def put_perm_group(
    group_id: str,
    *,
    name: str = "",
    description: str = "",
    recommended: bool = False,
    created_by: str = "",
) -> dict:
    """Create/replace a permission group's metadata. Raises ValueError on an
    invalid id (it becomes a Cedar group literal). Access is granted separately
    via group-scoped trigger rules; membership via set_identity_groups."""
    if not valid_group_id(group_id):
        raise ValueError(
            f"invalid group id {group_id!r} — must match {_GROUP_ID_RE.pattern}"
        )
    existing = get_perm_group(group_id) or {}
    item = {
        "pk": _perm_group_pk(group_id),
        "kind": "perm_group",
        "group_id": group_id,
        "name": name or existing.get("name", group_id),
        "description": description or existing.get("description", ""),
        "recommended": bool(recommended),
        "created_by": created_by or existing.get("created_by", ""),
        "created_at": existing.get("created_at", int(time.time())),
    }
    _get_table().put_item(Item=item)
    return item


def group_members(group_id: str) -> list[dict]:
    """Identities that belong to ``group_id`` (membership lives on the record)."""
    return [i for i in list_identities() if group_id in (i.get("groups") or [])]


def delete_perm_group(group_id: str) -> bool:
    """Delete a group's metadata row. Does NOT strip membership or group-scoped
    trigger rules — the admin API handles that lifecycle so a bare delete never
    leaves a rule referencing a vanished group silently in force."""
    resp = _get_table().delete_item(
        Key={"pk": _perm_group_pk(group_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


# --- Notification subscriptions (spec §18) -----------------------------------
# A channel self-subscribes (via /sdlc-notify) to fleet + SCM events, scoped to
# repos it's actually granted, split into tiers. A subscription only RECEIVES —
# it grants no access — so it needs no admin approval.


def _notif_sub_pk(team_id: str, channel_id: str) -> str:
    return f"{_NOTIF_SUB_PK_PREFIX}{team_id}#{channel_id}"


def list_notif_subs(team_id: str | None = None) -> list[dict]:
    """All notification subscriptions, or just one workspace's, newest first."""
    rows = _scan_kind("notif_sub", sort_key="updated_at")
    if team_id is not None:
        rows = [r for r in rows if r.get("team_id") == team_id]
    return rows


def get_notif_sub(team_id: str, channel_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": _notif_sub_pk(team_id, channel_id)})
    return resp.get("Item")


def put_notif_sub(
    team_id: str,
    channel_id: str,
    *,
    repos: list[str] | None = None,
    tiers: dict | None = None,
    min_severity: str = NOTIF_TIER_INFORMATIVE,
    created_by: str = "",
) -> dict:
    """Create/replace a channel's notification subscription. Validates the Slack
    ids and the tier map (keys ⊆ NOTIF_TIERS). ``repos`` is stored normalized;
    the ADMIN API is responsible for bounding it to the channel's granted repos
    before calling this (§18.2) — the store validates shape, the API validates
    authorization, matching the repo/capability split elsewhere."""
    if not valid_slack_team(team_id):
        raise ValueError(f"invalid Slack team id {team_id!r}")
    if not valid_slack_channel(channel_id):
        raise ValueError(f"invalid Slack channel id {channel_id!r}")
    if min_severity not in NOTIF_TIERS:
        raise ValueError(f"invalid min_severity {min_severity!r}")
    clean_tiers: dict[str, list[str]] = {}
    for tier, events in (tiers or {}).items():
        if tier not in NOTIF_TIERS:
            raise ValueError(f"invalid notification tier {tier!r}")
        clean_tiers[tier] = [str(e).strip() for e in (events or []) if str(e).strip()]
    existing = get_notif_sub(team_id, channel_id) or {}
    now = int(time.time())
    item = {
        "pk": _notif_sub_pk(team_id, channel_id),
        "kind": "notif_sub",
        "team_id": team_id,
        "channel_id": channel_id,
        "repos": sorted({_normalize_repo(r) for r in (repos or []) if r}),
        "tiers": clean_tiers,
        "min_severity": min_severity,
        "created_by": created_by or existing.get("created_by", ""),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    _get_table().put_item(Item=item)
    return item


def delete_notif_sub(team_id: str, channel_id: str) -> bool:
    resp = _get_table().delete_item(
        Key={"pk": _notif_sub_pk(team_id, channel_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


def _scan_kind(kind: str, *, sort_key: str) -> list[dict]:
    """All rows of one ``kind`` via the kind-index, sorted DESC by ``sort_key``
    (0 when absent). Shared by the Part II listers — the bounded Query drains
    every page so a >1MB result never silently drops a row."""
    rows = _query_kind(kind)
    rows.sort(key=lambda r: r.get(sort_key, 0) or 0, reverse=True)
    return rows
