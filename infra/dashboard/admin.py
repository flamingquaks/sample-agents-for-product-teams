"""Dashboard admin API Lambda — fleet configuration (write surface).

Admin-only counterpart to the read-only query API (api.py). Members of the
``admins`` Cognito group onboard/enable/disable repos and toggle the
restrict-to-allowlist setting. Every route requires auth.is_admin (fails
closed); the read API's operators can view but not configure.

Routes (all admin-only):
    GET    /admin/repos                     list onboarded repos
    POST   /admin/repos                     onboard/update a repo (body: repo, enabled?, multi_repo_eligible?, co_repo_mode?, repo_group?)
    DELETE /admin/repos/{repo}              remove a repo
    GET    /admin/settings                  get fleet settings
    PUT    /admin/settings                  update settings (body: restrict_repos)
    GET    /admin/capabilities              list onboarded capabilities (agents)
    POST   /admin/capabilities              onboard/edit a capability (body: agent_id, description?, aliases?, triggers?, limits?, env?, enabled?)
    DELETE /admin/capabilities/{agent_id}   remove a capability
    GET/POST /admin/slack/workspaces        list / onboard Slack workspaces; DELETE /{team_id}
    GET/POST /admin/slack/channels          list / set channel allow-deny; DELETE /{team_id}/{channel_id}
    GET/POST /admin/trigger-rules           list / create WHO grant rules; DELETE /{rule_id}
    POST   /admin/trigger-rules/simulate    dry-run an access decision (principal, agent, workspace, channel)
    GET    /admin/channel-requests          list channel onboarding requests (?status=pending)
    POST   /admin/channel-requests/{id}/approve|deny   decide a request

Two synchronized effects (see the plan's "exact chain"): a repo change writes
the config table (drives the Dispatch Router allowlist) AND regenerates the
single fleet Cedar policy on the Gateway policy engine (drives the tool-call
boundary). The policy sync is wired in WS5; ``_sync_repo_policy`` is the seam.
The write is ordered so the config row is only marked ``active`` once the policy
sync succeeds — an admin action never widens dispatch while the tool-call policy
still denies (or vice-versa).
"""

import json
import logging
import os
import re
import time
from urllib.parse import unquote

import auth
import config_store
from http_responses import error, json_response, ok

# GitHub owner/repo segment: letters, digits, hyphen, underscore, dot. This is
# stricter than GitHub's own rules but a safe superset for real repos, and it is
# the security boundary that keeps repo names out of the Cedar policy as
# metacharacters — the owner/repo strings are interpolated into the generated
# Cedar statement (fleet_policy.py), so a name containing a quote/pipe/newline
# could otherwise inject into or break the policy.
_REPO_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Deploy stage — names the per-workspace Slack SSM secret paths a workspace row
# records (config_store._slack_secret_param). Matches the receiver's STAGE.
STAGE = os.environ.get("STAGE", "dev")


class PolicySyncError(Exception):
    """Raised when the Gateway Cedar policy could not be updated to match the
    intended repo allowlist. Callers surface this as a 5xx and do NOT report the
    config change as complete."""


def _sync_repo_policy() -> None:
    """Regenerate + push the fleet Cedar policies from the current allowed set to
    the Gateway policy engine.

    Delegates to policy_sync.sync_fleet_policy, which renders the forbid/permit
    statements from config_store.allowed_repos() and CreatePolicy/updates the
    named policies via bedrock-agentcore-control — raising PolicySyncError on
    failure or Cedar-analysis rejection. A no-op (logged) until the gateway is
    provisioned (POLICY_ENGINE_ID unset), so the dispatch allowlist still works
    before the gateway lands.

    Imported lazily so the admin API's config routes don't hard-depend on the
    control-plane client (and tests can monkeypatch this seam directly)."""
    import policy_sync

    policy_sync.sync_fleet_policy()


def _gateway_enforcing() -> bool:
    """Whether the Gateway policy engine is actually blocking tool calls
    (ENFORCE / ACTIVE) vs merely logging (LOG_ONLY). Set from the same parameter
    that drives the gateway→engine attachment mode. When NOT enforcing, a policy
    sync failure can't cause a dispatch-vs-tool-call split (the policy blocks
    nothing), so onboarding treats it as a non-fatal warning rather than failing
    the whole operation."""
    mode = os.environ.get("GATEWAY_ENFORCEMENT", "LOG_ONLY").upper()
    return mode == "ACTIVE"


def _sync_after_write(log_msg: str, error_msg: str) -> dict | None:
    """Run the policy sync after a config write that has already been persisted
    (delete/settings). Returns a 502 error response only when the gateway is
    ENFORCING (a stale policy then actually matters); in LOG_ONLY it logs and
    returns None so the caller reports success. Returns None on sync success."""
    try:
        _sync_repo_policy()
        return None
    except PolicySyncError:
        if _gateway_enforcing():
            logger.exception(log_msg)
            return error(502, error_msg)
        logger.warning(
            "%s (gateway not enforcing; will re-sync on next change)", log_msg
        )
        return None


def _verify_github_install(repo: str):
    """Verify the GitHub App is installed on ``repo``'s owner and can reach the
    repo. Returns ``(installation_id, None)`` on success, or ``(None, <response>)``
    where response is a ready 409/502 to return to the caller.

    409s are actionable: "not installed" carries the install deep-link; "not
    covered" tells the admin the repo isn't in the installation's selection."""
    import github_client

    if not github_client.app_configured():
        return None, error(
            409, "GitHub App is not set up yet — set it up in the admin UI first"
        )

    owner, name = repo.split("/", 1)
    try:
        owner_type = github_client.get_owner_type(owner)
        installation_id = github_client.find_installation(owner, owner_type)
        if installation_id is None:
            link = github_client.install_url()
            payload = {
                "error": f"the GitHub App is not installed on '{owner}'. Install "
                "it, then re-check.",
                "install_url": link,
                "owner_type": owner_type,
            }
            return None, json_response(409, payload)
        if not github_client.repo_reachable(owner, name, installation_id):
            return None, json_response(
                409,
                {
                    "error": f"'{repo}' isn't covered by the App installation on "
                    f"'{owner}'. Add it to the installation's repository "
                    "selection, then re-check.",
                    "install_url": github_client.install_url(),
                    "owner_type": owner_type,
                },
            )
        # Record the per-owner installation (source of truth) before returning.
        config_store.put_installation(
            owner, owner_type=owner_type, installation_id=installation_id
        )
        return installation_id, None
    except github_client.GitHubError as exc:
        logger.exception("GitHub App verification failed for %s", repo)
        status = 404 if exc.status == 404 else 502
        return None, error(status, f"GitHub verification failed for {repo}: {exc}")
    except Exception:  # noqa: BLE001
        # Non-GitHub failures (a malformed/empty private key → ValueError, a
        # missing 'token' in GitHub's response → KeyError, or a Secrets
        # Manager/SSM ClientError) would otherwise escape to handler()'s blanket
        # 500 "internal error" — the opaque failure this verification gate exists
        # to prevent. Surface an actionable 502 instead, without echoing the
        # exception (it can carry secret material, e.g. a JWT/token/PEM fragment).
        logger.exception("GitHub App verification errored for %s", repo)
        return None, error(
            502,
            f"GitHub App verification could not complete for {repo} — check the "
            "App's private key is configured, then retry",
        )


def _parse_body(event: dict) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _valid_repo(repo: str) -> bool:
    """Validate owner/repo shape AND character set — exactly one slash, and each
    half matches ``_REPO_SEGMENT`` ([A-Za-z0-9._-]). The character check is not
    cosmetic: owner/repo flows unescaped into the generated Cedar policy
    statement (fleet_policy.py), so rejecting quotes/pipes/newlines/whitespace
    here prevents policy injection or a malformed statement. Not a GitHub
    existence check."""
    if not isinstance(repo, str) or repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    return bool(_REPO_SEGMENT.match(owner)) and bool(_REPO_SEGMENT.match(name))


# Sources a capability may declare event triggers for — must match the keys the
# Dispatch Router understands (router.py routes per source). Kept as a constant so
# an onboard can't register a trigger for a source the router will never fire.
_TRIGGER_SOURCES = ("github", "asana", "slack")

# A runtime env var key: uppercase/underscore, the conventional shape agents read
# (project_config.py does os.environ["ASANA_PROJECT_GID"] etc). Validated so an
# onboard can't smuggle a key that later breaks the runtime's env CSV.
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_capability_body(body: dict) -> tuple[dict, dict | None]:
    """Validate + normalize an onboard/edit body. Returns ``(fields, None)`` ready
    to splat into config_store.put_capability, or ``({}, <error response>)``.

    Everything an admin submits ends up in a resource name, a filesystem path, a
    Cedar/registry literal, or the runtime env CSV, so each field is shape-checked
    here at the API boundary (config_store re-validates agent_id as defense in
    depth)."""
    agent_id = (body.get("agent_id") or "").strip()
    if not config_store.valid_agent_id(agent_id):
        return {}, error(
            400,
            "body.agent_id must be lowercase, start with a letter, end "
            "alphanumeric, [a-z0-9-], 2-64 chars",
        )

    description = (body.get("description") or "").strip()

    aliases = body.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        return {}, error(400, "body.aliases must be a list of strings")

    triggers = body.get("triggers", {})
    if not isinstance(triggers, dict):
        return {}, error(400, "body.triggers must be an object of {source: [events]}")
    for source, events in triggers.items():
        if source not in _TRIGGER_SOURCES:
            return {}, error(
                400, f"body.triggers source must be one of {list(_TRIGGER_SOURCES)}"
            )
        if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
            return {}, error(400, f"body.triggers.{source} must be a list of strings")

    limits = body.get("limits", {})
    if not isinstance(limits, dict):
        return {}, error(400, "body.limits must be an object")
    for k, v in limits.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            return {}, error(400, f"body.limits.{k} must be a non-negative number")

    env = body.get("env", {})
    if not isinstance(env, dict):
        return {}, error(400, "body.env must be an object of {KEY: value}")
    for k, v in env.items():
        if not isinstance(k, str) or not _ENV_KEY_RE.match(k):
            return {}, error(400, f"body.env key {k!r} must match [A-Z][A-Z0-9_]*")
        # The fleet-wide security gates (guardrail + gateway URL) are set by the
        # deployer from the stack, never by a capability — accepting them here
        # would let an onboard disable the guardrail or repoint the tool-call
        # gateway. Reject outright (config_store also wins the base value on merge).
        if k in config_store.RESERVED_ENV_KEYS:
            return {}, error(
                400,
                f"body.env.{k} is a reserved fleet setting and cannot be set per "
                "capability",
            )
        if not isinstance(v, str):
            return {}, error(400, f"body.env.{k} must be a string")
        # The runtime env is passed to create/update-agent-runtime as a
        # comma-separated KEY=value CSV; a comma in a value would split into a
        # bogus extra var. Reject it at the boundary.
        if "," in v:
            return {}, error(400, f"body.env.{k} must not contain a comma")

    return {
        "agent_id": agent_id,
        "description": description,
        "aliases": aliases,
        "triggers": triggers,
        "limits": limits,
        "env": env,
        "enabled": bool(body.get("enabled", True)),
    }, None


def _start_capability_build(agent_id: str) -> str:
    """Trigger the shared build pipeline for ``agent_id`` and return the image tag
    the build will push (and the runtime will then use). The admin API only ever
    STARTS the build (codebuild:StartBuild on the single shared project) — it holds
    no privilege to create the runtime/role; a CodeBuild-completion event drives
    that via the capability_deployer Lambda. So this returns fast and the caller
    reports "building"; it does not block on the multi-minute build.

    The image tag is time-based (the caller can't read git SHA here) so each build
    is a distinct tag. A short random suffix is appended so two builds for the same
    agent in the same wall-clock second (double-submit, retry, onboard-then-edit)
    can't collide on the IMMUTABLE ECR tag — a collision would fail the second
    push and surface a spurious build failure. AGENT_NAME + IMAGE_TAG are passed as
    build env overrides; the deployer reads them back off the completion event.

    No-op returning '' when CAPABILITY_BUILD_PROJECT is unset (gateway/dashboard
    deployed without the build pipeline) — the capability stays pending and an
    operator can wire the pipeline later; we don't fail the onboard write."""
    project = os.environ.get("CAPABILITY_BUILD_PROJECT")
    if not project:
        logger.warning("CAPABILITY_BUILD_PROJECT unset — capability %s left pending, "
                       "no build started", agent_id)
        return ""
    import boto3

    # ``build-<epoch>-<rand>``: still matches the deployer's IMAGE_TAG allowlist
    # (^[A-Za-z0-9_.-]+$) and stays unique within a second. secrets.token_hex is
    # collision-resistant without needing wall-clock sub-second precision.
    import secrets

    image_tag = f"build-{int(time.time())}-{secrets.token_hex(3)}"
    boto3.client("codebuild").start_build(
        projectName=project,
        environmentVariablesOverride=[
            {"name": "AGENT_NAME", "value": agent_id, "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
        ],
    )
    return image_tag


def _onboard_capability(event: dict, body: dict) -> dict:
    """Onboard or edit a capability, then kick off its build.

    Persists the declarative row and republishes the router registry (so an edit
    to a LIVE capability takes effect at once). Then starts the shared build
    pipeline and marks the row ``building``; the capability_deployer Lambda,
    triggered by build completion, creates/updates the runtime and flips the row
    to ``active`` (entering the registry). A brand-new capability therefore stays
    OUT of the registry until its runtime is actually up — a mention can never
    resolve to a runtime that isn't ready.

    Starting a build on every onboard/edit is intentional: an edit that changes
    the runtime env (e.g. new Asana GID) must reach the runtime, and a rebuild is
    how env is re-applied (update-agent-runtime replaces env wholesale). Editing
    only registry-level fields (aliases) also rebuilds — cheap, and it keeps one
    code path. A build failure marks the row ``failed`` without disturbing any
    existing runtime."""
    fields, err = _validate_capability_body(body)
    if err is not None:
        return err
    agent_id = fields["agent_id"]
    config_store.put_capability(onboarded_by=auth.caller_sub(event), **fields)
    _publish_registry_safe()

    try:
        image_tag = _start_capability_build(agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("failed to start build for capability %s", agent_id)
        config_store.set_capability_status(
            agent_id, config_store.CAP_FAILED, detail="could not start build pipeline"
        )
        return error(502, f"capability {agent_id} saved but the build could not be "
                          "started; edit it to retry")
    if image_tag:
        config_store.set_capability_status(
            agent_id, config_store.CAP_BUILDING, detail=f"build started ({image_tag})"
        )
    return ok(config_store.get_capability(agent_id))


def _delete_capability(agent_id: str) -> dict:
    """Remove a capability row and republish the registry so the router stops
    resolving it. Phase 4 adds runtime/role teardown ahead of this delete; for now
    a delete just removes the row + drops it from the registry."""
    if not config_store.valid_agent_id(agent_id):
        return error(400, "invalid agent_id")
    deleted = config_store.delete_capability(agent_id)
    _publish_registry_safe()
    return ok({"agent_id": agent_id, "deleted": deleted})


def _publish_registry_safe() -> None:
    """Publish the rendered registry to SSM. A publish failure is logged but not
    fatal to the config write: the row is already persisted, and the registry
    re-publishes on the next capability change (and the router keeps serving its
    cached copy meanwhile). Mirrors the LOG_ONLY policy-sync tolerance."""
    try:
        config_store.publish_registry()
    except Exception:  # noqa: BLE001
        logger.exception("registry publish failed; will re-publish on next change")


def _resolve_agent_grants(agent_id: str, workspace: str) -> dict:
    """Resolve an agent's WHO grant sets from the trigger_rule rows — the SAME
    logic the router's trigger_grants.agent_grants applies (config_store and the
    dispatch reader share the row shape as a contract). Used by the access
    simulator so an admin sees exactly what the router would decide."""
    ap, dp, ag, dg = [], [], [], []
    for r in config_store.list_trigger_rules():
        r_agent = r.get("agent_id", "*")
        if r_agent != "*" and r_agent != agent_id:
            continue
        r_ws = r.get("workspace", "*")
        if r_ws != "*" and r_ws != workspace:
            continue
        subject = r.get("subject_id", "")
        if not subject:
            continue
        is_group = r.get("subject_type") == config_store.RULE_SUBJECT_GROUP
        is_forbid = r.get("effect") == config_store.RULE_FORBID
        target = (dg if is_group else dp) if is_forbid else (ag if is_group else ap)
        if subject not in target:
            target.append(subject)
    return {"allowedPrincipals": ap, "deniedPrincipals": dp,
            "allowedGroups": ag, "deniedGroups": dg}


def _channel_allowed(workspace: str, channel_id: str) -> bool:
    """Mirror of trigger_grants.channel_allowed for the simulator (see that
    function for the posture rules). Non-Slack ⇒ True; unknown workspace ⇒
    False."""
    if not workspace:
        return True
    ws = config_store.get_slack_workspace(workspace)
    if ws is None:
        return False
    policy = ws.get("default_channel_policy", config_store.CHANNEL_POLICY_ALLOWLIST)
    rows = {c["channel_id"]: c for c in config_store.list_channels(workspace)}
    row = rows.get(channel_id)
    if policy == config_store.CHANNEL_POLICY_DENYLIST:
        return not (row is not None and row.get("mode") == config_store.CHANNEL_MODE_DENY)
    return row is not None and row.get("mode") == config_store.CHANNEL_MODE_ALLOW


def _simulate_access(body: dict) -> dict:
    """Dry-run the trigger-authz decision for a hypothetical (principal, agent,
    workspace, channel, groups) — the "Test access" panel. Evaluates the SAME
    data-driven logic the router applies (grant sets + channel posture + Cedar
    forbid-wins), locally, so it needs no AVP round-trip and works before the
    store is wired. Returns {decision, reason}."""
    principal = (body.get("principal") or "").strip()
    agent_id = (body.get("agent_id") or "").strip()
    workspace = (body.get("workspace") or "").strip()
    channel = (body.get("channel_id") or "").strip()
    groups = set(body.get("principal_groups") or [])
    if not principal or not agent_id:
        return error(400, "body.principal and body.agent_id are required")

    grants = _resolve_agent_grants(agent_id, workspace)
    channel_ok = _channel_allowed(workspace, channel)

    # forbid-wins: an explicit deny (principal or group) or a blocked channel
    # denies regardless of any permit; then a permit requires an allowed
    # principal or group; else default-deny.
    if principal in grants["deniedPrincipals"] or (groups & set(grants["deniedGroups"])):
        return ok({"decision": "DENY", "reason": "explicitly-denied"})
    if not channel_ok:
        return ok({"decision": "DENY", "reason": "channel-not-allowed"})
    if principal in grants["allowedPrincipals"] or (groups & set(grants["allowedGroups"])):
        return ok({"decision": "ALLOW", "reason": "granted"})
    return ok({"decision": "DENY", "reason": "no-matching-grant"})


def _decide_channel_request(event: dict, request_id: str, approve: bool, body: dict) -> dict:
    """Approve or deny a channel onboarding request. APPROVAL is the only path
    that grants access: it creates the channel allow row and — for each requested
    agent (or none = a workspace-wide permit is intentionally NOT created; an
    admin scopes agents explicitly) — a permit trigger rule keyed on the channel's
    workspace, then marks the request approved. Denial just records the decision.
    All effects are explicit here so the outcome is auditable + testable."""
    req = config_store.get_channel_request(request_id)
    if req is None:
        return error(404, f"no such channel request: {request_id}")
    caller = auth.caller_sub(event)
    if not approve:
        rec = config_store.resolve_channel_request(
            request_id, status=config_store.CHAN_REQ_DENIED, decided_by=caller
        )
        return ok({"request": rec})

    team_id = req["team_id"]
    channel_id = req["channel_id"]
    # 1) allow the channel (the WHERE axis) so triggers there pass the channel gate.
    config_store.put_channel_policy(
        team_id, channel_id,
        mode=config_store.CHANNEL_MODE_ALLOW,
        channel_name=req.get("channel_name", ""),
        note=f"approved from request {request_id}",
        created_by=caller,
    )
    # 2) the admin may override the requested agent scope at approval time.
    agents = body.get("approved_agents")
    if agents is None:
        agents = req.get("requested_agents", [])
    created = []
    for agent_id in agents:
        rule = config_store.put_trigger_rule(
            connector="slack",
            subject_type=config_store.RULE_SUBJECT_GROUP,
            # A channel-scoped grant is modeled as a group whose members are the
            # channel's participants; the receiver passes the channel id as a
            # principal group so this permits anyone triggering FROM that channel.
            subject_id=f"channel:{team_id}:{channel_id}",
            agent_id=agent_id,
            workspace=team_id,
            effect=config_store.RULE_PERMIT,
            created_by=caller,
        )
        created.append(rule["rule_id"])
    rec = config_store.resolve_channel_request(
        request_id, status=config_store.CHAN_REQ_APPROVED, decided_by=caller
    )
    return ok({"request": rec, "channel_allowed": True, "created_rules": created})


def _route(event: dict) -> dict:
    resource = event.get("resource", "")
    method = event.get("httpMethod", "")
    path_params = event.get("pathParameters") or {}
    body = _parse_body(event)

    if resource == "/admin/repos":
        if method == "GET":
            return ok({"repos": config_store.list_repos()})
        if method == "POST":
            repo = (body.get("repo") or "").strip()
            if not _valid_repo(repo):
                return error(400, "body.repo must be 'owner/repo'")
            enabled = bool(body.get("enabled", True))
            eligible = bool(body.get("multi_repo_eligible", True))
            # Co-repo rule: which OTHER repos a dispatch originating here may reach
            # (isolated|group|all). Validate the enum and the group-label shape so
            # a bad value can't silently widen reach or smuggle metacharacters.
            co_repo_mode = (body.get("co_repo_mode") or config_store.CO_REPO_ISOLATED).strip()
            if co_repo_mode not in config_store.CO_REPO_MODES:
                return error(
                    400,
                    "body.co_repo_mode must be one of "
                    f"{list(config_store.CO_REPO_MODES)}",
                )
            repo_group = (body.get("repo_group") or "").strip() or None
            if co_repo_mode == config_store.CO_REPO_GROUP:
                if not repo_group:
                    return error(400, "body.repo_group is required when co_repo_mode='group'")
                if not _REPO_SEGMENT.match(repo_group):
                    return error(400, "body.repo_group must match [A-Za-z0-9._-]")
            # GitHub App verification gates bringing a repo INTO the fleet —
            # confirm the App is installed on the owner and
            # can reach the repo BEFORE a NEW onboard, so we never silently
            # activate an unreachable repo. It must NOT gate UPDATES to an
            # already-onboarded repo: an admin has to be able to disable/adjust a
            # repo during an incident (e.g. the App was just uninstalled, or
            # GitHub is down) — blocking that on live reachability is the opposite
            # of what's needed. So for an existing repo we skip verification and
            # preserve its recorded installation_id.
            existing = config_store.get_repo(repo)
            if existing is not None:
                installation_id = existing.get("installation_id")
            else:
                installation_id, verify_response = _verify_github_install(repo)
                if verify_response is not None:
                    return verify_response
            # 1) write the row active, 2) sync the tool-call policy from the
            # allowed set (which now INCLUDES this repo — allowed_repos() only
            # returns active rows, so syncing while pending would omit the very
            # repo being onboarded), 3) roll back to pending if the sync fails.
            # A repo is only ever left active once its policy sync succeeded, so
            # dispatch never treats as allowed a repo the tool-call policy still
            # denies. During the brief window before the sync lands the policy
            # still denies (the safe direction), and the dispatch config cache is
            # stale anyway, so dispatch does not widen ahead of the policy.
            # Preserve the prior verification timestamp on an update (we didn't
            # re-verify); stamp now only on a fresh verified onboard.
            if existing is not None:
                verified_at = existing.get("install_verified_at")
            else:
                verified_at = int(time.time()) if installation_id else None
            # Model cost attribution is fleet-wide (one shared Mantle project set
            # as MANTLE_PROJECT_ID on the agent runtimes), not per-repo — a
            # dispatch may span repos, so there's nothing repo-scoped to create
            # here.
            config_store.put_repo(
                repo,
                enabled=enabled,
                multi_repo_eligible=eligible,
                co_repo_mode=co_repo_mode,
                repo_group=repo_group,
                onboarded_by=auth.caller_sub(event),
                status="active",
                installation_id=installation_id,
                install_verified_at=verified_at,
            )
            try:
                _sync_repo_policy()
            except PolicySyncError:
                # When the gateway is ENFORCING, a sync failure means dispatch
                # would widen ahead of a tool-call policy that still denies — roll
                # back to pending and fail. When it's LOG_ONLY (or the gateway
                # isn't fully wired yet), the policy blocks nothing, so onboarding
                # succeeds and the sync is retried on the next admin action; we
                # return the repo with a warning rather than failing.
                if _gateway_enforcing():
                    logger.exception("policy sync failed for %s; rolling back", repo)
                    config_store.set_repo_status(repo, "pending")
                    return error(
                        502,
                        f"repo recorded but policy update failed for {repo}; "
                        "left pending, retry",
                    )
                logger.warning(
                    "policy sync failed for %s but gateway is not enforcing; "
                    "repo left active, policy will re-sync on next change",
                    repo,
                )
                rec = config_store.get_repo(repo)
                rec["policy_sync_warning"] = (
                    "onboarded; Gateway policy not yet synced (gateway in LOG_ONLY "
                    "or not fully wired)"
                )
                return ok(rec)
            return ok(config_store.get_repo(repo))

    # Greedy {repo+} so an "owner/repo" (with its slash) is one path parameter;
    # accept the plain {repo} form too so the handler isn't coupled to the exact
    # template path.
    if resource in ("/admin/repos/{repo+}", "/admin/repos/{repo}"):
        if method == "DELETE":
            # API Gateway URL-decodes path params, but a client that
            # double-encoded the slash would leave a literal "%2F"; decode
            # defensively so the pk matches the stored (decoded) key.
            raw = path_params.get("repo") or path_params.get("repo+")
            if not raw:
                return error(400, "missing repo")
            repo = unquote(raw)
            deleted = config_store.delete_repo(repo)
            # The per-owner install record is shared across all of an owner's
            # repos, so drop it only once this was the owner's LAST repo —
            # otherwise a sibling repo would lose its installation reference.
            # Leaving it forever would orphan a record pointing at an
            # installation that may later be revoked.
            if deleted and "/" in repo:
                owner = repo.split("/", 1)[0]
                if not config_store.owner_has_repos(owner):
                    config_store.delete_installation(owner)
            # Removing a repo narrows the allowlist — sync the policy so its tool
            # calls stop being allowed. Delete has already narrowed dispatch (the
            # safe direction), so a sync failure is only reported as fatal when the
            # gateway is enforcing; in LOG_ONLY it's a non-fatal warning.
            failure = _sync_after_write(
                f"policy sync failed after deleting {repo}",
                "repo removed from dispatch but policy update failed; retry",
            )
            if failure is not None:
                return failure
            return ok({"repo": repo, "deleted": deleted})

    if resource == "/admin/settings":
        if method == "GET":
            return ok(config_store.get_settings())
        if method == "PUT":
            if "restrict_repos" not in body:
                return error(400, "body.restrict_repos (bool) required")
            settings = config_store.put_settings(
                restrict_repos=bool(body["restrict_repos"])
            )
            failure = _sync_after_write(
                "policy sync failed after settings change",
                "settings saved but policy update failed; retry",
            )
            if failure is not None:
                return failure
            return ok(settings)

    # --- Capabilities (UI-onboarded agents) ---
    if resource == "/admin/capabilities":
        if method == "GET":
            return ok({"capabilities": config_store.list_capabilities()})
        if method == "POST":
            return _onboard_capability(event, body)

    if resource in ("/admin/capabilities/{agent_id}", "/admin/capabilities/{agent_id+}"):
        agent_id = (path_params.get("agent_id") or path_params.get("agent_id+") or "").strip()
        if not agent_id:
            return error(400, "missing agent_id")
        if method == "DELETE":
            return _delete_capability(agent_id)

    # --- GitHub App setup (manifest flow) ---
    if resource == "/admin/github-app/status" and method == "GET":
        import github_client

        configured = github_client.app_configured()
        # Resolve the slug once and derive the install URL from it (install_url()
        # would otherwise re-read the slug SSM param a second time per status poll).
        slug = github_client.app_slug() if configured else None
        install = f"{github_client.GITHUB_API}/apps/{slug}/installations/new" if slug else None
        return ok(
            {
                "configured": configured,
                "slug": slug,
                "install_url": install,
            }
        )

    if resource == "/admin/github-app/setup/manifest" and method == "GET":
        import github_client

        # GET carries no body — read optional org/app_name from the query string.
        qs = event.get("queryStringParameters") or {}
        app_name = qs.get("app_name") or os.environ.get(
            "GITHUB_APP_NAME", "SDLC Agent Fleet"
        )
        # Derive the API base from the request itself (avoids a CFN cycle between
        # the admin function and its own API).
        rc = event.get("requestContext") or {}
        domain = rc.get("domainName", "")
        stage = rc.get("stage", "")
        api_base = f"https://{domain}/{stage}" if domain else ""
        frontend = os.environ.get("DASHBOARD_URL", "")
        # The App's webhook must deliver to the dispatch-side webhook API, NOT the
        # admin/dashboard API. That endpoint is a different API Gateway, so it's
        # supplied via env (WEBHOOK_API_BASE) rather than derived from this request.
        webhook_base = os.environ.get("WEBHOOK_API_BASE", "")
        if not api_base or not frontend or not webhook_base:
            return error(500, "could not resolve API/dashboard/webhook URL for the manifest")
        manifest = github_client.generate_manifest(
            app_name, api_base, frontend, f"{webhook_base}/github/webhook"
        )
        # The SPA POSTs this to the org form when an org is given, else the user
        # form. Hand back both the manifest and the target so the SPA doesn't
        # hardcode GitHub URLs.
        org = (qs.get("org") or "").strip()
        post_url = (
            f"{github_client.GITHUB_API}/organizations/{org}/settings/apps/new"
            if org
            else f"{github_client.GITHUB_API}/settings/apps/new"
        )
        return ok({"manifest": manifest, "post_url": post_url})

    if resource == "/admin/github-app/setup/callback" and method == "POST":
        import github_client

        code = (body.get("code") or "").strip() if body else ""
        if not code:
            return error(400, "body.code (manifest conversion code) required")
        try:
            result = github_client.exchange_manifest_code(code)
        except github_client.GitHubError as exc:
            logger.exception("manifest code exchange failed")
            return error(502, f"GitHub App creation failed: {exc}")
        return ok(result)

    # --- Slack workspaces ---
    if resource == "/admin/slack/workspaces":
        if method == "GET":
            return ok({"workspaces": config_store.list_slack_workspaces()})
        if method == "POST":
            team_id = (body.get("team_id") or "").strip()
            if not config_store.valid_slack_team(team_id):
                return error(400, "body.team_id must be a Slack team id (T…)")
            policy = (body.get("default_channel_policy")
                      or config_store.CHANNEL_POLICY_ALLOWLIST)
            if policy not in config_store.CHANNEL_POLICIES:
                return error(400, f"body.default_channel_policy must be one of {list(config_store.CHANNEL_POLICIES)}")
            try:
                rec = config_store.put_slack_workspace(
                    team_id,
                    team_name=(body.get("team_name") or "").strip(),
                    stage=STAGE,
                    enabled=bool(body.get("enabled", True)),
                    default_channel_policy=policy,
                    onboarded_by=auth.caller_sub(event),
                    status=config_store.SLACK_WS_ACTIVE,
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource in ("/admin/slack/workspaces/{team_id}", "/admin/slack/workspaces/{team_id+}"):
        team_id = (path_params.get("team_id") or path_params.get("team_id+") or "").strip()
        if method == "DELETE":
            return ok({"team_id": team_id, "deleted": config_store.delete_slack_workspace(team_id)})

    # --- Slack channel policy ---
    if resource == "/admin/slack/channels":
        if method == "GET":
            team_id = (event.get("queryStringParameters") or {}).get("team_id", "")
            return ok({"channels": config_store.list_channels(team_id)})
        if method == "POST":
            try:
                rec = config_store.put_channel_policy(
                    (body.get("team_id") or "").strip(),
                    (body.get("channel_id") or "").strip(),
                    mode=(body.get("mode") or "").strip(),
                    channel_name=(body.get("channel_name") or "").strip(),
                    note=(body.get("note") or "").strip(),
                    created_by=auth.caller_sub(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource == "/admin/slack/channels/{team_id}/{channel_id}":
        if method == "DELETE":
            team_id = (path_params.get("team_id") or "").strip()
            channel_id = (path_params.get("channel_id") or "").strip()
            return ok({"deleted": config_store.delete_channel_policy(team_id, channel_id)})

    # --- Trigger rules (WHO axis) ---
    if resource == "/admin/trigger-rules":
        if method == "GET":
            connector = (event.get("queryStringParameters") or {}).get("connector")
            return ok({"rules": config_store.list_trigger_rules(connector)})
        if method == "POST":
            try:
                rec = config_store.put_trigger_rule(
                    connector=(body.get("connector") or "").strip(),
                    subject_type=(body.get("subject_type") or "").strip(),
                    subject_id=(body.get("subject_id") or "").strip(),
                    agent_id=(body.get("agent_id") or "*").strip(),
                    workspace=(body.get("workspace") or "*").strip(),
                    effect=(body.get("effect") or config_store.RULE_PERMIT).strip(),
                    created_by=auth.caller_sub(event),
                    rule_id=(body.get("rule_id") or None),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource in ("/admin/trigger-rules/{rule_id}", "/admin/trigger-rules/{rule_id+}"):
        rule_id = (path_params.get("rule_id") or path_params.get("rule_id+") or "").strip()
        if method == "DELETE":
            return ok({"rule_id": rule_id, "deleted": config_store.delete_trigger_rule(rule_id)})

    if resource == "/admin/trigger-rules/simulate" and method == "POST":
        return _simulate_access(body)

    # --- Channel onboarding requests (approve/deny queue) ---
    if resource == "/admin/channel-requests":
        if method == "GET":
            status = (event.get("queryStringParameters") or {}).get("status")
            return ok({"requests": config_store.list_channel_requests(status)})

    if resource in ("/admin/channel-requests/{request_id}/approve",
                    "/admin/channel-requests/{request_id}/deny"):
        if method == "POST":
            request_id = (path_params.get("request_id") or "").strip()
            approve = resource.endswith("/approve")
            return _decide_channel_request(event, request_id, approve, body)

    return error(404, f"no such admin route: {method} {resource}")


def handler(event, context=None):
    """Lambda entry point. Enforces admin auth, then routes."""
    if not auth.is_admin(event):
        return error(403, "admin role required")
    try:
        return _route(event)
    except Exception:
        logger.exception(
            "admin request failed for %s %s (caller=%s)",
            event.get("httpMethod"),
            event.get("resource"),
            auth.caller_sub(event),
        )
        return error(500, "internal error")
