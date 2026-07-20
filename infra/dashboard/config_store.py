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
  - Capability:    pk="capability#<agent_id>", {kind:"capability", agent_id,
                   description, aliases[list], triggers{source:[event...]},
                   authorization_users[list], limits{max_concurrent,
                   timeout_minutes, daily_token_budget}, env{KEY:val}, enabled(bool),
                   status: "pending"|"building"|"active"|"failed"|"disabled",
                   image_tag?, runtime_arn?, build_id?, onboarded_by, onboarded_at,
                   updated_at, status_detail?}

Capability record (the UI-onboarded agent): a "capability" is an agent the fleet
can dispatch to. It USED to be spread across code + .dispatch/agents.yaml +
per-agent CI wrappers + hand-created IAM roles; onboarding one now writes a single
row here and the dashboard drives the rest (build → runtime → registry → policy).
The registry the Dispatch Router reads from SSM is RENDERED from these rows
(render_registry), so the row is the source of truth that agents.yaml/sync_registry
previously were. Fields mirror the agents.yaml entry the router consumes
(description/aliases/triggers/authorization.users/limits) plus the deploy state the
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

_table = None


def _get_table():
    global _table
    if _table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


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
    # A Query on a constant would need a GSI; the repo set is small (an admin
    # onboards a handful), so a Scan filtered to repo records is fine here. We
    # still page on LastEvaluatedKey: a single scan() returns only the first 1MB
    # page, and silently dropping repos past it would omit them from both the
    # admin listing and the Cedar allowlist (allowed_repos builds on this).
    table = _get_table()
    repos: list[dict] = []
    start_key = None
    while True:
        kwargs = {
            "FilterExpression": "kind = :k",
            "ExpressionAttributeValues": {":k": "repo"},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**kwargs)
        repos.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
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
    """All onboarded capability records, newest first (by onboarded_at). Pages on
    LastEvaluatedKey for the same reason list_repos does — a dropped row would be
    silently missing from both the admin listing and the rendered registry."""
    table = _get_table()
    caps: list[dict] = []
    start_key = None
    while True:
        kwargs = {
            "FilterExpression": "kind = :k",
            "ExpressionAttributeValues": {":k": "capability"},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**kwargs)
        caps.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
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
    authorization_users: list[str] | None = None,
    limits: dict | None = None,
    env: dict | None = None,
    enabled: bool = True,
    status: str | None = None,
    onboarded_by: str = "",
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
    item = {
        "pk": _capability_pk(agent_id),
        "kind": "capability",
        "agent_id": agent_id,
        "description": description,
        "aliases": norm_aliases,
        "triggers": triggers or {},
        "authorization_users": [u for u in (authorization_users or []) if u],
        "limits": limits or {},
        "env": env or {},
        "enabled": bool(enabled),
        "status": status,
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


def delete_capability(agent_id: str) -> bool:
    """Delete a capability row. Returns True if a row was removed. Does NOT tear
    down the runtime/role/image — the admin API handles that lifecycle before
    removing the row, so a bare delete here never orphans the record ahead of its
    resources."""
    resp = _get_table().delete_item(
        Key={"pk": _capability_pk(agent_id)}, ReturnValues="ALL_OLD"
    )
    return bool(resp.get("Attributes"))


def render_registry() -> dict:
    """Render the Dispatch Router registry from the active capability rows.

    Reproduces exactly the shape the router consumes from SSM (see
    infra/dispatch/router.py load_registry / resolve_agent): a top-level
    ``{"agents": {agent_id: {description, runtime_arn, aliases, triggers,
    authorization: {users}, limits}}}`` map. This REPLACES .dispatch/agents.yaml +
    scripts/sync_registry.py — the registry is now generated from the table the
    admin UI writes, so onboarding an agent needs no code/YAML edit.

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
            "authorization": {"users": list(cap.get("authorization_users", []))},
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


def capability_env_pairs(cap: dict, base_env: dict[str, str]) -> dict[str, str]:
    """The full runtime environment for a capability: the fleet-wide base env
    (guardrail id/version + gateway URL — the hard gates every agent needs) merged
    with the capability's own ``env`` (e.g. Asana GIDs).

    The base env ALWAYS wins for RESERVED_ENV_KEYS: even though the admin API
    rejects a capability that sets them, this merge re-enforces it as defense in
    depth (e.g. a row written before this rule, or by a future non-API path) so a
    capability can never disable the guardrail or repoint the gateway.

    Returned as a plain dict; the caller renders the CSV create/update-agent-runtime
    wants. Kept here so the onboard path and the weekly rebuild share one
    definition of an agent's environment."""
    merged = dict(base_env)
    for k, v in (cap.get("env") or {}).items():
        if k in RESERVED_ENV_KEYS:
            continue  # base gate wins — never overridable
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
