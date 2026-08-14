"""Dashboard admin API Lambda — fleet configuration (write surface).

Admin-only counterpart to the read-only query API (api.py). Members of the
``admins`` Cognito group onboard/enable/disable repos and toggle the
restrict-to-allowlist setting. Every route requires auth.is_admin (fails
closed); the read API's operators can view but not configure.

Routes (all admin-only):
    GET    /admin/repos                     list onboarded repos
    POST   /admin/repos                     onboard/update repo(s) (body: repo | repos[], enabled?, multi_repo_eligible?, co_repo_mode?, repo_group?)
    GET    /admin/github-app/repos          installations + accessible repos (the onboarding picker's source)
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
    GET/POST /admin/identities              list / proactively create cross-source identities; GET/PUT/DELETE /{identity_id}
    GET    /admin/user-requests             list user-onboarding requests (?status=pending)
    POST   /admin/user-requests/{id}/approve|deny   decide a user request (approve = active + assign groups + verify handles)
    GET/POST /admin/groups                  list / create permission groups; GET/DELETE /{group_id}
    GET/POST /admin/notif-subs              list / upsert channel notification subscriptions; DELETE /{team_id}/{channel_id}

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
import fleet_policy
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


def _approval_gate_on() -> bool:
    """Whether the second-admin approval gate is on (spec §7.5). Deploy-time
    parameter (RequireAgentApproval → REQUIRE_AGENT_APPROVAL env); defaults ON —
    the fail-safe direction. One helper so the onboard and clone paths can never
    disagree on the parse or the default."""
    return os.environ.get("REQUIRE_AGENT_APPROVAL", "true").lower() == "true"


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
    except PolicySyncError as exc:
        if _gateway_enforcing():
            logger.exception(log_msg)
            return error(502, error_msg)
        # Non-fatal in LOG_ONLY, but keep the CAUSE in the log — a bare
        # "sync failed" line made the real error (IAM, Cedar validation, …)
        # undiagnosable without rerunning the sync by hand.
        logger.warning(
            "%s: %s (gateway not enforcing; will re-sync on next change)",
            log_msg, exc,
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
        # One JWT call answers "does an installation cover this exact repo?".
        # (Never GET /users/{owner} here — that endpoint rejects an App JWT with
        # 401 Bad credentials.)
        found = github_client.find_repo_installation(owner, name)
        if found is None:
            # Distinguish the two actionable 409s: App not installed on the
            # owner at all, vs installed but the repo isn't in its selection.
            owner_install = github_client.find_installation(owner)
            link = github_client.install_url()
            if owner_install is None:
                return None, json_response(
                    409,
                    {
                        "error": f"the GitHub App is not installed on '{owner}'. "
                        "Install it, then re-check.",
                        "install_url": link,
                    },
                )
            return None, json_response(
                409,
                {
                    "error": f"'{repo}' isn't covered by the App installation on "
                    f"'{owner}'. Add it to the installation's repository "
                    "selection, then re-check.",
                    "install_url": link,
                    "owner_type": owner_install[1],
                },
            )
        installation_id, owner_type = found
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


def _onboard_repos(event: dict, body: dict) -> dict:
    """Onboard/update one or more repos in a single admin action.

    ``body.repos`` (list) or the legacy ``body.repo`` (single). All repos in the
    batch share the same access settings; ``co_repo_mode`` declares which OTHER
    repos a dispatch originating in each may reach (isolated | group | all) —
    the UI's "share access together" sends group mode with one group name for
    the whole batch. Batch semantics:

      - Validate + GitHub-verify EVERY new repo up front, writing nothing on a
        failure — a batch never half-onboards; the 409/502 names the repo and
        carries the install deep-link so the admin can fix and resubmit whole.
      - Already-onboarded repos in the batch are updated (settings applied) but
        NOT re-verified — same incident-tolerance rationale as before: an admin
        must be able to adjust a repo while GitHub is down or the App was just
        uninstalled.
      - ONE policy sync for the whole batch (not one per repo). On sync failure
        with the gateway ENFORCING, every row this call activated is rolled back
        to pending; in LOG_ONLY the batch succeeds with a warning.

    Single-repo calls return the repo record itself (the shape the UI and tests
    have always consumed); a multi-repo batch returns {repos: [...]}."""
    raw_list = body.get("repos")
    single = raw_list is None
    if single:
        raw_list = [body.get("repo")]
    if not isinstance(raw_list, list) or not raw_list:
        return error(400, "body.repos must be a non-empty list of 'owner/repo'")
    repos: list[str] = []
    for raw in raw_list:
        repo = (raw or "").strip() if isinstance(raw, str) else ""
        if not _valid_repo(repo):
            return error(400, f"repo {raw!r} must be 'owner/repo'")
        norm = config_store._normalize_repo(repo)
        if norm not in repos:
            repos.append(norm)

    enabled = bool(body.get("enabled", True))
    eligible = bool(body.get("multi_repo_eligible", True))
    # Co-repo rule: validate the enum and the group-label shape so a bad value
    # can't silently widen reach or smuggle metacharacters.
    co_repo_mode = (body.get("co_repo_mode") or config_store.CO_REPO_ISOLATED).strip()
    if co_repo_mode not in config_store.CO_REPO_MODES:
        return error(
            400,
            f"body.co_repo_mode must be one of {list(config_store.CO_REPO_MODES)}",
        )
    repo_group = (body.get("repo_group") or "").strip() or None
    if co_repo_mode == config_store.CO_REPO_GROUP:
        if not repo_group:
            return error(400, "body.repo_group is required when co_repo_mode='group'")
        if not _REPO_SEGMENT.match(repo_group):
            return error(400, "body.repo_group must match [A-Za-z0-9._-]")

    # GitHub App verification gates bringing a repo INTO the fleet — confirm an
    # installation covers each NEW repo BEFORE writing anything, so we never
    # silently activate an unreachable repo and a batch never half-onboards.
    # UPDATES to an already-onboarded repo skip verification and preserve the
    # recorded installation_id (incident tolerance — see docstring).
    plan: list[tuple[str, int | None, int | None]] = []  # (repo, install_id, verified_at)
    for repo in repos:
        existing = config_store.get_repo(repo)
        if existing is not None:
            plan.append(
                (repo, existing.get("installation_id"), existing.get("install_verified_at"))
            )
            continue
        installation_id, verify_response = _verify_github_install(repo)
        if verify_response is not None:
            return verify_response
        plan.append((repo, installation_id, int(time.time()) if installation_id else None))

    # 1) write every row active, 2) ONE policy sync from the allowed set (which
    # now includes the whole batch — allowed_repos() only returns active rows),
    # 3) roll the batch back to pending if the sync fails while enforcing. A repo
    # is only ever left active once its policy sync succeeded, so dispatch never
    # treats as allowed a repo the tool-call policy still denies.
    # Model cost attribution is fleet-wide (one shared Mantle project), not
    # per-repo — nothing repo-scoped to create here.
    caller = auth.caller_email(event)
    for repo, installation_id, verified_at in plan:
        config_store.put_repo(
            repo,
            enabled=enabled,
            multi_repo_eligible=eligible,
            co_repo_mode=co_repo_mode,
            repo_group=repo_group,
            onboarded_by=caller,
            status="active",
            installation_id=installation_id,
            install_verified_at=verified_at,
        )
    try:
        _sync_repo_policy()
    except PolicySyncError as exc:
        # ENFORCING: dispatch would widen ahead of a tool-call policy that still
        # denies — roll the batch back to pending and fail. LOG_ONLY (or gateway
        # not fully wired): the policy blocks nothing, so onboarding succeeds
        # (no jargon warning for the admin) and the sync retries on the next
        # admin action. Keep the CAUSE in the log for diagnosability.
        if _gateway_enforcing():
            logger.exception("policy sync failed for %s; rolling back", repos)
            for repo, _, _ in plan:
                config_store.set_repo_status(repo, "pending")
            return error(
                502,
                f"repos recorded but policy update failed for {', '.join(repos)}; "
                "left pending, retry",
            )
        logger.warning(
            "policy sync failed for %s but gateway is not enforcing; repos left "
            "active, policy will re-sync on next change: %s",
            repos, exc,
        )
    if single:
        return ok(config_store.get_repo(repos[0]))
    return ok({"repos": [config_store.get_repo(r) for r in repos]})


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

# A "plain pip specifier" (§7.1): a package name, optional extras, optional
# version constraints and environment markers (PEP 508 allows spaces around
# each part, e.g. ``requests >= 2.31``) — never a flag, a VCS ref, a direct
# URL/path, or an embedded control character (a \n inside one "requirement"
# would emit a second physical line into requirements-extra.txt that pip parses
# as a standalone global option like --index-url).
#
# CONTRACT: this regex + the substring/control-character checks MUST stay
# byte-identical with agents/_base/gen_requirements.py (the last-line-of-defense
# re-validation inside the build). If they diverge, a spec can pass onboarding
# then fail the build (or vice versa). The parity test
# (tests/test_requirements_parity.py) enforces this.
_REQ_SPECIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"           # package name
    r" *(\[[A-Za-z0-9,. _-]+\])?"             # optional extras
    r" *([<>=!~][=]?[^;]*)?"                  # optional version constraint(s)
    r"(;.*)?$"                                # optional environment marker
)
_REQ_FORBIDDEN_SUBSTRINGS = ("://", "@", "git+", "svn+", "hg+", "bzr+")


def _valid_requirement_spec(spec: str) -> bool:
    """Whether ``spec`` is a plain PyPI specifier safe to write into
    requirements-extra.txt. Same decision procedure as
    gen_requirements._validate — see the CONTRACT note on _REQ_SPECIFIER_RE."""
    stripped = spec.strip()
    lowered = stripped.lower()
    return bool(
        stripped
        and not any(ord(c) < 32 or ord(c) == 127 for c in stripped)
        and not stripped.startswith(("-", "/", "./", "../"))
        # Whitespace-then-dash is how pip's PER-REQUIREMENT options attach
        # ("pkg>=1 --hash=…", "--config-settings=…"): the leading-dash check
        # above misses them and the version-tail regex would swallow them. No
        # legitimate specifier/marker contains " -".
        and not re.search(r"\s-", stripped)
        and not any(bad in lowered for bad in _REQ_FORBIDDEN_SUBSTRINGS)
        and _REQ_SPECIFIER_RE.match(stripped)
    )


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

    # ``plugins`` is RESERVED for the future marketplace-install seam (§6.4):
    # the schema keeps the name, but the API rejects it until the Claude Code
    # runtime lands — silently dropping it would let a caller believe a plugin
    # was installed.
    if "plugins" in body:
        return {}, error(
            400,
            "body.plugins is reserved for a future runtime and not yet supported (§6.4)",
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
        # AGENT_ID/SYSTEM_PROMPT/SKILLS_DIR are DERIVED from the capability row by
        # the deployer (§4), not free-form env. AGENT_ID in particular is the
        # identity the Gateway's Cedar policy keys on, so accepting it here would
        # let an agent assume another's tool grants. system_prompt has its own
        # top-level field. Reject them as env (config_store also drops them on merge).
        if k in config_store.BASE_AGENT_ENV_KEYS:
            return {}, error(
                400,
                f"body.env.{k} is derived from the capability (set 'system_prompt' "
                "for the prompt); it cannot be set as free-form env",
            )
        if not isinstance(v, str):
            return {}, error(400, f"body.env.{k} must be a string")
        # The runtime env is passed to create/update-agent-runtime as a
        # comma-separated KEY=value CSV; a comma in a value would split into a
        # bogus extra var. Reject it at the boundary.
        if "," in v:
            return {}, error(400, f"body.env.{k} must not contain a comma")

    # tool_grants — the per-tool allowlist for a custom agent (spec §3.5). Each
    # entry must be a KNOWN read/write tool in the fleet catalog; a destructive
    # tool (or an unknown/unclassified id) is rejected here so a config-authored
    # agent can never exceed the read/write/destructive envelope. Built-in agents
    # ignore this (their grants are the fixed AGENT_TOOL_GRANTS), but validating
    # it for every body keeps one code path.
    tool_grants = body.get("tool_grants", [])
    if not isinstance(tool_grants, list) or any(not isinstance(t, str) for t in tool_grants):
        return {}, error(400, "body.tool_grants must be a list of strings")
    norm_grants: list[str] = []
    for t in tool_grants:
        klass = fleet_policy.classify_tool(t)
        if klass is None:
            return {}, error(
                400,
                f"body.tool_grants entry {t!r} is not a known grantable tool "
                "(unknown or unclassified in the fleet tool catalog)",
            )
        if klass == fleet_policy.CLASS_DESTRUCTIVE:
            return {}, error(
                400,
                f"body.tool_grants entry {t!r} is a destructive tool and can never "
                "be granted",
            )
        if t not in norm_grants:
            norm_grants.append(t)

    # system_prompt — the agent's context/instructions (§3.2). Optional; built-in
    # agents ignore it (their prompt is code-defined).
    system_prompt = body.get("system_prompt", "")
    if not isinstance(system_prompt, str):
        return {}, error(400, "body.system_prompt must be a string")

    # requirements — pip specifiers for the per-agent extra deps (§5). Validated
    # to be plain specifiers: no --index-url, -e, VCS refs, or direct URLs (§7.1).
    requirements = body.get("requirements", [])
    if not isinstance(requirements, list) or any(not isinstance(r, str) for r in requirements):
        return {}, error(400, "body.requirements must be a list of strings")
    for r in requirements:
        if r.strip() and not _valid_requirement_spec(r):
            return {}, error(
                400,
                f"body.requirements entry {r!r} is not a plain pip specifier "
                "(no flags, VCS refs, direct URLs, local paths, or control "
                "characters allowed — §7.1)",
            )

    # skills — SKILL.md packages the agent loads (spec §6). Each entry references
    # an already-uploaded skill by ``{name, s3_prefix, sha256, scope}``; the
    # deployer syncs exactly these prefixes into the container's SKILLS_DIR. Like
    # requirements, novel skills trip the approval gate (§7.5), so this must reach
    # the persisted row rather than being dropped on the floor.
    #
    # Every field is boundary-validated because each one is load-bearing:
    #   - scope is allowlisted (it's an S3 key segment + the teardown/list axis);
    #   - name must be a valid skill name (it becomes the container skill dir);
    #   - s3_prefix must be EXACTLY the canonical skills/<scope>/<name>/ — a
    #     free-form prefix could point the runtime sync at _staging/ or another
    #     package's tree;
    #   - sha256 is REQUIRED (64 hex chars): the base agent verifies the synced
    #     tree against it at startup, and an empty hash silently disables that
    #     integrity check (§6.3).
    import skill_store

    skills = body.get("skills", [])
    if not isinstance(skills, list):
        return {}, error(400, "body.skills must be a list of skill references")
    norm_skills: list[dict] = []
    for s in skills:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s.get("name"):
            return {}, error(400, "body.skills entry must be an object with a non-empty 'name'")
        name = s["name"]
        if not skill_store._SKILL_NAME_RE.match(name):
            return {}, error(400, f"body.skills entry {name!r} is not a valid skill name")
        scope = str(s.get("scope", "shared"))
        if scope not in skill_store.VALID_SCOPES:
            return {}, error(
                400,
                f"body.skills entry {name!r} has invalid scope {scope!r} — must be "
                f"one of {list(skill_store.VALID_SCOPES)}",
            )
        expected_prefix = f"skills/{scope}/{name}/"
        if s.get("s3_prefix") != expected_prefix:
            return {}, error(
                400,
                f"body.skills entry {name!r} must reference its canonical prefix "
                f"{expected_prefix!r}",
            )
        sha = str(s.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return {}, error(
                400,
                f"body.skills entry {name!r} must include the package's sha256 "
                "(64 lowercase hex chars) — attach skills from the skills library",
            )
        # One skill per NAME per capability, across scopes: the base agent
        # syncs every package into SKILLS_DIR/<name>, so shared/pb and
        # capability/pb would overlay into one directory whose merged content
        # matches neither verified hash.
        if any(s["name"] == name for s in norm_skills):
            return {}, error(
                400,
                f"body.skills references {name!r} more than once (skill names "
                "must be unique per agent — they share one skills directory)",
            )
        norm_skills.append({
            "name": name,
            "s3_prefix": expected_prefix,
            "sha256": sha,
            "scope": scope,
        })

    return {
        "agent_id": agent_id,
        "description": description,
        "aliases": aliases,
        "triggers": triggers,
        "limits": limits,
        "env": env,
        "tool_grants": norm_grants,
        "system_prompt": system_prompt,
        "requirements": [r.strip() for r in requirements if r.strip()],
        "skills": norm_skills,
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
    existing runtime.

    **Built-in (system) agents** are fixed config, enable/disable-only (spec
    §3.1): a submit against a built-in row honors ONLY ``enabled`` — every other
    declarative field is taken from the seeded row, so the onboard form can never
    edit a system agent's config. **Disabling** any agent (built-in or custom)
    de-routes it WITHOUT a rebuild; only **enabling** builds/deploys."""
    fields, err = _validate_capability_body(body)
    if err is not None:
        return err
    agent_id = fields["agent_id"]

    existing = config_store.get_capability(agent_id)
    if existing and existing.get("builtin"):
        # System agent: config is fixed — the only admin lever is enable/disable
        # (spec §3.1/§8.2). A submit that carries any OTHER declarative field is
        # rejected (400) rather than silently ignored, so a caller can't believe
        # it edited a built-in. The UI's toggle sends exactly {agent_id, enabled}.
        offered = set(body) - {"agent_id", "enabled"}
        if offered:
            return error(
                400,
                f"{agent_id} is a built-in system agent; only 'enabled' can be "
                f"changed (got: {sorted(offered)})",
            )
        # Preserve the seeded declarative fields. (put_capability re-preserves
        # builtin/deploy-state itself; we just avoid overwriting config from body.)
        fields = {
            "agent_id": agent_id,
            "description": existing.get("description", ""),
            "aliases": list(existing.get("aliases", [])),
            "triggers": dict(existing.get("triggers", {})),
            "limits": dict(existing.get("limits", {})),
            "env": dict(existing.get("env", {})),
            "tool_grants": list(existing.get("tool_grants", [])),
            "system_prompt": existing.get("system_prompt", ""),
            "requirements": list(existing.get("requirements", [])),
            "skills": list(existing.get("skills", [])),
            "enabled": fields["enabled"],
        }

    # A sparse body must not CLEAR config it didn't mention: the UI's
    # Enable/Disable toggle sends exactly {agent_id, enabled}, and
    # _validate_capability_body defaults every omitted field to an empty value
    # that put_capability would persist as a deliberate clear — wiping a custom
    # agent's prompt/deps/grants/skills on a toggle (and, with requirements
    # emptied, sailing past the approval gate's novelty check). For every
    # declarative field ABSENT from the body, preserve the existing row's value.
    if existing:
        _preserved = {
            "description": existing.get("description", ""),
            "aliases": list(existing.get("aliases", [])),
            "triggers": dict(existing.get("triggers", {})),
            "limits": dict(existing.get("limits", {})),
            "env": dict(existing.get("env", {})),
            "tool_grants": list(existing.get("tool_grants", [])),
            "system_prompt": existing.get("system_prompt", ""),
            "requirements": list(existing.get("requirements", [])),
            "skills": list(existing.get("skills", [])),
        }
        for key, prior in _preserved.items():
            if key not in body:
                fields[key] = prior

    # Disabling de-routes without a rebuild — building a disabled agent is wasted
    # work, and render_registry already drops it. This is "disable = de-route,
    # runtime left running, not torn down" (spec §9).
    if not fields["enabled"]:
        config_store.put_capability(onboarded_by=auth.caller_email(event), **fields)
        config_store.set_capability_status(
            agent_id, config_store.CAP_DISABLED, detail="disabled by admin (de-routed)"
        )
        _publish_registry_safe()
        # Disabling must also retract the agent's Gateway tool permit (§3.5) —
        # de-routing stops NEW dispatches, but the running runtime could still
        # call tools until its permit is dropped from the policy engine.
        failure = _sync_after_write(
            f"policy sync failed after disabling {agent_id}",
            f"{agent_id} disabled but its tool-permit update failed; retry",
        )
        if failure is not None:
            return failure
        return ok(config_store.get_capability(agent_id))

    config_store.put_capability(onboarded_by=auth.caller_email(event), **fields)

    # Approval gate (spec §7.5, §303): if ON and this is a CUSTOM agent whose
    # deps/skills are NOVEL relative to the last-approved row, park it as
    # pending_review instead of starting a build. "Novel" — not merely "present" —
    # is the trigger: a brand-new agent with deps has novel deps, and adding a new
    # dep/skill to an already-approved agent re-gates it, but editing a
    # registry-only field (an alias) on an approved agent must NOT re-park it and
    # block the rebuild. Comparing against the approved baseline covers both.
    gate_on = _approval_gate_on()
    is_custom = not (existing and existing.get("builtin"))
    already_approved = bool(existing) and existing.get("review_status") == "approved"

    def _skill_key(skills) -> set:
        # A skill is "the same" if its content (sha256) and identity (scope/name)
        # are unchanged; a re-upload with new content bumps sha256 and re-gates.
        return {(s.get("scope"), s.get("name"), s.get("sha256")) for s in (skills or [])}

    if already_approved:
        # Only deps/skills that weren't in the approved baseline count as novel.
        prior_reqs = set(existing.get("requirements", []))
        prior_skills = _skill_key(existing.get("skills"))
        has_novel = bool(
            (set(fields.get("requirements") or []) - prior_reqs)
            or (_skill_key(fields.get("skills")) - prior_skills)
        )
    else:
        # New agent (or one never approved): any dep/skill is novel.
        has_novel = bool(fields.get("requirements") or fields.get("skills"))
    if gate_on and is_custom and has_novel:
        config_store.set_review_status(agent_id, "pending_review")
        _publish_registry_safe()
        # Sync now: a pending_review row's (unapproved, possibly widened)
        # grants are excluded from the rendered permits (_grants_by_agent), so
        # this retracts the agent's permit until a second admin approves —
        # without it the old permit (or, via a later unrelated sync, nothing at
        # all) would govern a row whose grants were never approved.
        _sync_after_write(
            f"policy sync failed after parking {agent_id} for review",
            f"{agent_id} parked for review but its permit retraction failed",
        )
        return ok(config_store.get_capability(agent_id))

    _publish_registry_safe()

    # Sync the agent's tool permit to the Gateway policy engine (§3.5): a custom
    # agent's authored tool_grants only take effect when the per-agent permit is
    # rendered onto the engine, and an edit that NARROWS grants must retract the
    # old permit rather than waiting for the next unrelated repo/settings change.
    # Runs before the build starts — a permit for a runtime that isn't up yet is
    # inert, while a runtime that comes up without its permit can call nothing.
    failure = _sync_after_write(
        f"policy sync failed after capability change for {agent_id}",
        f"{agent_id} saved but its tool-permit update failed; edit it to retry",
    )
    if failure is not None:
        return failure

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
    """Destroy a CUSTOM agent (spec §8.2, §10-P3): de-route it immediately, then
    hand the privileged teardown (runtime + role + image + capability-scoped
    skills + row) to the deployer Lambda. A built-in (system) agent is undeletable
    (409) — it's disabled, not removed (spec §3.1).

    The admin API holds NO IAM/runtime/ECR privileges by design, so it does NOT
    delete resources itself. It flips the row to ``deleting`` (which de-routes it
    the instant delete is requested — render_registry excludes ``deleting``) and
    async-invokes the deployer, which owns the guarded teardown. If the deployer
    function isn't wired (dashboard deployed without it), the row can't be safely
    torn down, so we surface that rather than orphaning resources."""
    if not config_store.valid_agent_id(agent_id):
        return error(400, "invalid agent_id")
    cap = config_store.get_capability(agent_id)
    if cap is None:
        return error(404, f"no such capability: {agent_id}")
    if cap.get("builtin"):
        return error(409, f"{agent_id} is a built-in system agent and cannot be "
                          "deleted; disable it instead")

    deployer = os.environ.get("CAPABILITY_DEPLOYER_FUNCTION")
    if not deployer:
        return error(503, "custom-agent teardown is unavailable (deployer not "
                          "configured); cannot safely delete")

    # De-route first so a mention stops resolving immediately, even before the
    # async teardown finishes.
    config_store.set_capability_status(
        agent_id, config_store.CAP_DELETING, detail="teardown requested"
    )
    _publish_registry_safe()
    # Retract the agent's Gateway tool permit now (§3.5): the deployer teardown
    # deletes resources, not policies, and a still-running runtime could call
    # tools until its permit is dropped. Non-fatal in LOG_ONLY (same posture as
    # every other narrow-direction sync) — deletion proceeds regardless.
    _sync_after_write(
        f"policy sync failed while deleting {agent_id}",
        f"{agent_id} de-routed but its tool-permit retraction failed",
    )

    import boto3

    try:
        boto3.client("lambda").invoke(
            FunctionName=deployer,
            InvocationType="Event",  # async — teardown runs for minutes
            Payload=json.dumps({"action": "teardown", "agent_id": agent_id}).encode(),
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to invoke deployer teardown for %s", agent_id)
        config_store.set_capability_status(
            agent_id, config_store.CAP_FAILED, detail="teardown could not be started"
        )
        return error(502, f"{agent_id} de-routed but teardown could not be started; "
                          "retry the delete")
    return ok({"agent_id": agent_id, "status": "deleting"})


def _approve_capability(event: dict, agent_id: str) -> dict:
    """Second-admin approval of a pending_review custom agent (spec §7.5, §8.2).
    Sets review_status=approved, then starts the build. The caller must differ
    from the author — the onboarded_by field records who created/edited it."""
    cap = config_store.get_capability(agent_id)
    if cap is None:
        return error(404, f"no such capability: {agent_id}")
    if cap.get("review_status") != "pending_review":
        return error(409, f"{agent_id} is not pending review (status: {cap.get('review_status')})")
    caller = auth.caller_email(event)
    if caller == cap.get("onboarded_by"):
        return error(403, "the approver must be a different admin than the author")
    config_store.set_review_status(agent_id, "approved")
    # The approved agent's tool permit reaches the Gateway now (§3.5) — its
    # runtime comes up right after the build, and without the permit it could
    # call nothing (fail-closed, but broken). On a sync failure the approval is
    # rolled back to pending_review so the approve can simply be retried (a row
    # left "approved" would 409 the retry).
    failure = _sync_after_write(
        f"policy sync failed after approving {agent_id}",
        f"{agent_id}'s tool-permit update failed; approval rolled back — retry",
    )
    if failure is not None:
        config_store.set_review_status(agent_id, "pending_review")
        return failure
    try:
        image_tag = _start_capability_build(agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("approved but failed to start build for %s", agent_id)
        config_store.set_capability_status(
            agent_id, config_store.CAP_FAILED, detail="approved but build failed to start"
        )
        return error(502, f"{agent_id} approved but the build could not be started")
    if image_tag:
        config_store.set_capability_status(
            agent_id, config_store.CAP_BUILDING, detail=f"approved + build started ({image_tag})"
        )
    _publish_registry_safe()
    return ok(config_store.get_capability(agent_id))


def _clone_capability(event: dict, source_id: str, body: dict) -> dict:
    """Clone an existing capability (built-in or custom) into a new, editable
    custom agent (spec §8.2). Copies declarative config, strips deploy-state +
    builtin, and writes a new ``pending`` row. The admin then edits + enables.
    The required ``new_agent_id`` comes from the body."""
    new_id = (body.get("new_agent_id") or "").strip()
    if not new_id or not config_store.valid_agent_id(new_id):
        return error(400, "body.new_agent_id must be a valid agent id")
    source = config_store.get_capability(source_id)
    if source is None:
        return error(404, f"no such capability: {source_id}")
    if config_store.get_capability(new_id) is not None:
        return error(409, f"agent_id {new_id!r} already exists")
    # Approval gate (§7.5/§8.2): a NEW row defaults review_status='approved',
    # but this clone may be copying the source's requirements/skills into it.
    # Born 'approved', a later edit+enable would compute ZERO novelty against
    # that copied "baseline" and skip the second-admin gate entirely — even
    # though no admin ever approved these deps ON THIS agent. So when the gate
    # is on and the clone carries any deps/skills, the row is born
    # pending_review (atomically, in the same put — a post-hoc status flip
    # would leave the vulnerable 'approved' state behind on a crash between the
    # writes). A dep-free clone stays approved, matching the onboard path for a
    # new agent without deps.
    has_deps = bool(source.get("requirements") or source.get("skills"))
    cloned = config_store.put_capability(
        new_id,
        description=source.get("description", ""),
        aliases=[],  # fresh — don't collide with source's aliases
        triggers=source.get("triggers", {}),
        limits=source.get("limits", {}),
        env=source.get("env", {}),
        tool_grants=source.get("tool_grants", []),
        system_prompt=source.get("system_prompt", ""),
        requirements=source.get("requirements", []),
        skills=source.get("skills", []),
        enabled=False,
        status=config_store.CAP_PENDING,
        onboarded_by=auth.caller_email(event),
        builtin=False,
        review_status="pending_review" if (_approval_gate_on() and has_deps) else None,
    )
    return ok(cloned)


def _unpack_skill_zip_isolated(data: bytes, scope: str) -> dict:
    """Stage a raw skill zip to the skills bucket and hand it to the ISOLATED
    skill-unpacker Lambda for validated expansion (spec §6.3). The admin API never
    expands an untrusted zip in-process — that's the whole point of the isolation
    boundary. Returns the skill ref on success or an error() on validation failure.

    Falls back to nothing: if the unpacker function isn't wired (dashboard without
    the skills feature), the upload is refused (503) rather than silently unpacking
    here — the isolation is a security invariant, not an optimization."""
    import os as _os

    import skill_store

    bucket = skill_store.SKILLS_BUCKET
    unpacker = _os.environ.get("SKILL_UNPACKER_FUNCTION")
    if not bucket:
        return error(503, "skills storage is not configured")
    if not unpacker:
        return error(503, "skill zip unpacking is unavailable (unpacker not configured)")

    import boto3

    # A random staging key under an _staging/ prefix (never a skill prefix, so a
    # half-processed upload can't be mistaken for a real skill by the sync/list).
    import secrets

    staging_key = f"_staging/{secrets.token_hex(16)}.zip"
    s3 = boto3.client("s3")
    try:
        s3.put_object(Bucket=bucket, Key=staging_key, Body=data)
    except Exception:  # noqa: BLE001
        logger.exception("could not stage skill upload")
        return error(502, "could not stage the upload for validation")

    try:
        resp = boto3.client("lambda").invoke(
            FunctionName=unpacker,
            InvocationType="RequestResponse",  # synchronous — we return its verdict
            Payload=json.dumps({"staging_key": staging_key, "scope": scope}).encode(),
        )
        result = json.loads(resp["Payload"].read() or b"{}")
    except Exception:  # noqa: BLE001
        logger.exception("skill unpacker invocation failed")
        # Best-effort staging cleanup; the unpacker also deletes on its own paths.
        try:
            s3.delete_object(Bucket=bucket, Key=staging_key)
        except Exception:  # noqa: BLE001
            pass
        return error(502, "skill validation service failed; retry the upload")

    if result.get("ok"):
        return ok(result["ref"])
    return error(400, result.get("error", "skill validation failed"))


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


def _container_allowed(source: str, site_id: str, container_key: str) -> bool:
    """Mirror of trigger_grants.container_allowed for the simulator — the WHERE
    axis for a Jira/Confluence dispatch. Posture math over the product's
    container rows plus the site's per-product default policy. Fail-closed on an
    unknown container key or a missing site."""
    product = "jira" if source == "jira" else "confluence"
    site = config_store.get_atlassian_site(site_id) or {}
    policy_key = "default_project_policy" if product == "jira" else "default_space_policy"
    policy = site.get(policy_key, config_store.CONTAINER_POLICY_ALLOWLIST)
    if product == "jira":
        row = config_store.get_jira_project(site_id, container_key)
    else:
        row = config_store.get_confluence_space(site_id, container_key)
    if policy == config_store.CONTAINER_POLICY_DENYLIST:
        return not (row is not None and row.get("mode") == config_store.CONTAINER_MODE_DENY)
    return row is not None and row.get("mode") == config_store.CONTAINER_MODE_ALLOW


def _simulate_access(body: dict) -> dict:
    """Dry-run the trigger-authz decision for a hypothetical (principal, agent,
    source, workspace, channel/container, groups) — the "Test access" panel.
    Evaluates the SAME data-driven logic the router applies (grant sets + WHERE
    posture + Cedar forbid-wins), locally, so it needs no AVP round-trip and
    works before the store is wired. Returns {decision, reason}.

    The WHERE axis is per-source, matching trigger_authz.is_authorized: Slack's
    is the channel id + Slack-workspace posture; Jira/Confluence's is the
    project/space key + the site's container posture. A Jira/Confluence container
    arrives as ``project_key``/``space_key`` (never ``channel_id``), so we accept
    those and fall back to ``channel_id`` for a generic caller."""
    principal = (body.get("principal") or "").strip()
    agent_id = (body.get("agent_id") or "").strip()
    source = (body.get("source") or "").strip()
    workspace = (body.get("workspace") or "").strip()
    groups = set(body.get("principal_groups") or [])
    if not principal or not agent_id:
        return error(400, "body.principal and body.agent_id are required")

    is_atlassian = source in ("jira", "confluence")
    # The container key: project/space for Atlassian, channel for Slack. Accept
    # the source-native field, falling back to channel_id.
    if source == "jira":
        channel = (body.get("project_key") or body.get("channel_id") or "").strip()
    elif source == "confluence":
        channel = (body.get("space_key") or body.get("channel_id") or "").strip()
    else:
        channel = (body.get("channel_id") or "").strip()

    # The receiver drops every delivery from a workspace/site that isn't onboarded
    # + enabled + active, BEFORE authz runs — so the simulation must reflect that
    # gate first, or the panel would report ALLOW where production is silent. The
    # gate is per-source: Slack workspace vs Atlassian site (+ product enabled).
    if workspace:
        if is_atlassian:
            site = config_store.get_atlassian_site(workspace)
            product = "jira" if source == "jira" else "confluence"
            if (site is None or not site.get("enabled")
                    or site.get("status") != config_store.ATLASSIAN_SITE_ACTIVE
                    or not (site.get("products") or {}).get(product)):
                return ok({"decision": "DENY", "reason": "site-not-enabled"})
        else:
            ws = config_store.get_slack_workspace(workspace)
            if ws is None or not ws.get("enabled") or ws.get("status") != config_store.SLACK_WS_ACTIVE:
                return ok({"decision": "DENY", "reason": "workspace-not-enabled"})

    grants = _resolve_agent_grants(agent_id, workspace)
    if is_atlassian:
        # Fail-closed: an Atlassian dispatch always carries a container; without a
        # workspace we can't resolve the site posture, so treat as not-allowed.
        channel_ok = _container_allowed(source, workspace, channel) if workspace else False
    else:
        channel_ok = _channel_allowed(workspace, channel)

    # forbid-wins: an explicit deny (principal or group) or a blocked channel/
    # container denies regardless of any permit; then a permit requires an
    # allowed principal or group; else default-deny.
    if principal in grants["deniedPrincipals"] or (groups & set(grants["deniedGroups"])):
        return ok({"decision": "DENY", "reason": "explicitly-denied"})
    if not channel_ok:
        return ok({"decision": "DENY", "reason": "container-not-allowed" if is_atlassian else "channel-not-allowed"})
    if principal in grants["allowedPrincipals"] or (groups & set(grants["allowedGroups"])):
        return ok({"decision": "ALLOW", "reason": "granted"})
    return ok({"decision": "DENY", "reason": "no-matching-grant"})


def _decide_channel_request(event: dict, request_id: str, approve: bool, body: dict) -> dict:
    """Approve or deny a channel onboarding request. APPROVAL is the only path
    that grants access: it creates the channel allow row and, for each CONCRETE
    approved agent, a permit trigger rule keyed on the channel group, then marks
    the request approved. An empty/wildcard scope is rejected (400) — the admin
    must name the agents, so approval can neither be a silent no-op nor a
    workspace-wide over-grant. Only a pending request can be decided (409
    otherwise). Denial just records the decision. All effects are explicit here so
    the outcome is auditable + testable."""
    req = config_store.get_channel_request(request_id)
    if req is None:
        return error(404, f"no such channel request: {request_id}")
    # Only a still-pending request can be decided. Without this guard a
    # double-click / replayed POST re-runs the writes below (duplicate permit
    # rows), and an approve-then-deny would flip the status while the granted
    # rules linger. A decided request is terminal.
    if req.get("status") != config_store.CHAN_REQ_PENDING:
        return error(409, f"request {request_id} already {req.get('status')}")
    caller = auth.caller_email(event)
    if not approve:
        rec = config_store.resolve_channel_request(
            request_id, status=config_store.CHAN_REQ_DENIED, decided_by=caller
        )
        return ok({"request": rec})

    team_id = req["team_id"]
    channel_id = req["channel_id"]
    # The admin scopes the grant to CONCRETE agents (the requirement is "access
    # specific things for that channel"). Prefer an explicit approval override,
    # else the agents the user requested. An empty scope is REJECTED — we never
    # silently coerce it to a wildcard (that would grant every fleet agent to
    # everyone in the channel) nor to nothing (a channel-allow with no WHO grant
    # that still default-denies). The admin must name the agents.
    # Repo scope (spec §19): the channel's approved direct-work repos. Prefer an
    # explicit approval override, else what the user requested. BOUNDED to the
    # fleet's onboarded repos — approving a channel can never widen the fleet.
    # Empty is fine (a channel may be agent-only, e.g. Asana work).
    repos = body.get("approved_repos")
    if repos is None:
        repos = req.get("requested_repos", [])
    onboarded = {r["repo"] for r in config_store.list_repos()}
    repos = sorted({str(r).strip().casefold() for r in repos if r} & onboarded)

    agents = body.get("approved_agents")
    if agents is None:
        agents = req.get("requested_agents", [])
    agents = [a for a in agents if a and a != "*"]
    if not agents:
        return error(
            400,
            "approve requires a concrete agent scope: pass body.approved_agents "
            "(the request did not name specific agents)",
        )
    # 1) allow the channel (the WHERE axis) so triggers there pass the channel
    # gate, carrying the approved direct-work repo scope (§19).
    config_store.put_channel_policy(
        team_id, channel_id,
        mode=config_store.CHANNEL_MODE_ALLOW,
        channel_name=req.get("channel_name", ""),
        note=f"approved from request {request_id}",
        created_by=caller,
        repos=repos,
    )
    # 2) one permit rule per approved agent, keyed on the channel group (the
    # receiver stamps `channel:<team>:<chan>` into principal_groups, so this
    # permits anyone triggering FROM that channel). Deterministic rule_id per
    # (team, channel, agent) so a retry overwrites rather than duplicates.
    created = []
    for agent_id in agents:
        rule = config_store.put_trigger_rule(
            connector="slack",
            subject_type=config_store.RULE_SUBJECT_GROUP,
            subject_id=f"channel:{team_id}:{channel_id}",
            agent_id=agent_id,
            workspace=team_id,
            effect=config_store.RULE_PERMIT,
            created_by=caller,
            rule_id=f"chan-{team_id}-{channel_id}-{agent_id}",
        )
        created.append(rule["rule_id"])
    rec = config_store.resolve_channel_request(
        request_id, status=config_store.CHAN_REQ_APPROVED, decided_by=caller
    )
    return ok({"request": rec, "channel_allowed": True, "created_rules": created,
               "approved_repos": repos})


def _decide_user_request(event: dict, request_id: str, approve: bool, body: dict) -> dict:
    """Approve or deny a user-onboarding request (spec §16.4 / §17.4). APPROVAL
    is the trust event: it flips the identity to ``active``, assigns the admin's
    chosen permission groups (the access step), and marks every source handle the
    identity already carries as ``verified`` (admin approval IS the verification —
    §16.6). Denial just records the decision. Only a pending request can be
    decided (409 otherwise); all effects are explicit here for auditability."""
    req = config_store.get_user_request(request_id)
    if req is None:
        return error(404, f"no such user request: {request_id}")
    if req.get("status") != config_store.USER_REQ_PENDING:
        return error(409, f"request {request_id} already {req.get('status')}")
    caller = auth.caller_email(event)
    identity_id = req.get("identity_id", "")
    if not approve:
        rec = config_store.resolve_user_request(
            request_id, status=config_store.USER_REQ_DENIED, decided_by=caller
        )
        return ok({"request": rec})

    identity = config_store.get_identity(identity_id)
    if identity is None:
        return error(404, f"identity {identity_id} no longer exists")
    # The access step: assign the chosen groups (recommended path). Groups may be
    # empty — an admin can onboard someone with no group yet and grant later — but
    # then the identity is active-yet-ungranted (default-deny still applies).
    groups = [g for g in (body.get("groups") or []) if g]
    for g in groups:
        if not config_store.valid_group_id(g):
            return error(400, f"invalid group id {g!r}")
        if config_store.get_perm_group(g) is None:
            return error(400, f"no such permission group: {g!r}")
    config_store.set_identity_groups(identity_id, groups)
    # Admin approval verifies every handle the identity carries (the trust event).
    for source in (identity.get("handles") or {}).keys():
        try:
            config_store.set_identity_verified(identity_id, source, True)
        except ValueError:
            pass  # ignore an unknown source key defensively
    config_store.set_identity_status(identity_id, config_store.IDENTITY_ACTIVE)
    rec = config_store.resolve_user_request(
        request_id, status=config_store.USER_REQ_APPROVED, decided_by=caller
    )
    return ok({"request": rec, "identity_id": identity_id, "groups": groups})


def _channel_granted_repos(team_id: str, channel_id: str) -> set[str]:
    """Repos a channel is allowed to receive notifications about (spec §18.2): the
    fleet's onboarded repos, since a channel's trigger grants are agent-scoped not
    repo-scoped and the fleet is small. Bounds a notification subscription so a
    channel can't subscribe to a repo the fleet doesn't even manage. (A tighter
    per-channel repo grant can layer on later; today the onboarded-repo set is the
    ceiling.)"""
    return {r["repo"] for r in config_store.list_repos() if r.get("enabled")}


class _LabelDirectory:
    """Turns the raw ids that trigger rules / channel rows / requests store into
    the human labels the dashboard shows — so an admin never sees a bare Slack
    ``T…:U…`` or ``C…`` id. Built once per request from a bounded set of Queries
    (identities, all channels, workspaces, groups) and reused across every row
    being decorated, so decorating N rows is a handful of reads, not N×4.

    The label vocabulary, all with a graceful fallback to the raw id so a
    not-yet-enriched row still renders something meaningful:

      - person   ``slack:T:U`` / ``github:login`` / ``asana:gid`` / email
                 → the identity's ``display_name``, else its ``email``, else the
                   raw handle (display_name is the verified Slack name we now
                   capture; email is the golden fallback — the user's stated rule)
      - channel  ``channel:T:C`` (a group subject) → ``#channel-name``
      - group    a permission-group id → the group's human ``name``
      - workspace ``T…`` team id → the workspace ``team_name``
    """

    def __init__(self) -> None:
        self._people: dict[str, str] = {}  # handle_key OR email(casefold) -> label
        self._channels: dict[str, str] = {}  # "channel:T:C" -> "#name"
        self._workspaces: dict[str, str] = {}  # team_id -> team_name
        self._groups: dict[str, str] = {}  # group_id -> name
        self._built = False

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        for ident in config_store.list_identities():
            label = (ident.get("display_name") or "").strip() or (ident.get("email") or "").strip()
            if not label:
                continue
            for hk in ident.get("handle_keys") or []:
                self._people.setdefault(hk, label)
            email = (ident.get("email") or "").strip().casefold()
            if email:
                self._people.setdefault(email, label)
        for ch in config_store.list_all_channels():
            name = (ch.get("channel_name") or "").strip()
            if name:
                key = f"channel:{ch.get('team_id', '')}:{ch.get('channel_id', '')}"
                self._channels[key] = name if name.startswith("#") else f"#{name}"
        for ws in config_store.list_slack_workspaces():
            name = (ws.get("team_name") or "").strip()
            if name:
                self._workspaces[ws.get("team_id", "")] = name
        for g in config_store.list_perm_groups():
            name = (g.get("name") or "").strip()
            if name:
                self._groups[g.get("group_id", "")] = name

    def person(self, principal: str) -> str:
        """Friendly name for a user subject/principal, else the raw id."""
        self._build()
        key = (principal or "").strip()
        if not key:
            return key
        return self._people.get(key) or self._people.get(key.casefold()) or key

    def channel(self, subject: str) -> str:
        """``#channel-name`` for a ``channel:T:C`` group subject, else the raw id."""
        self._build()
        return self._channels.get((subject or "").strip(), subject)

    def group(self, group_id: str) -> str:
        self._build()
        return self._groups.get((group_id or "").strip(), group_id)

    def workspace(self, team_id: str) -> str:
        self._build()
        return self._workspaces.get((team_id or "").strip(), team_id)

    def channel_id(self, team_id: str, channel_id: str) -> str:
        """``#name`` for a (team, channel) pair, else the raw channel id."""
        self._build()
        return self._channels.get(f"channel:{team_id}:{channel_id}", channel_id)

    def subject(self, subject_type: str, subject_id: str) -> str:
        """The label for a trigger-rule subject, dispatching on its type. A
        ``group`` subject is either a channel group (``channel:T:C``) or a named
        permission group; a ``user`` subject is a person principal."""
        if subject_type == config_store.RULE_SUBJECT_GROUP:
            if (subject_id or "").startswith("channel:"):
                return self.channel(subject_id)
            return self.group(subject_id)
        return self.person(subject_id)


def _decorate_trigger_rules(rules: list[dict], directory: _LabelDirectory) -> list[dict]:
    """Attach a ``subject_label`` (and, for a Slack rule, ``workspace_label``) to
    each rule so the UI can show ``#channel-name`` / a person's name / a group
    name instead of a raw id. Non-destructive: the raw ids stay on the row (the
    UI keeps them as secondary text / for edits)."""
    out = []
    for r in rules:
        row = dict(r)
        row["subject_label"] = directory.subject(
            r.get("subject_type", ""), r.get("subject_id", "")
        )
        ws = r.get("workspace", "")
        if ws and ws != "*":
            row["workspace_label"] = directory.workspace(ws)
        out.append(row)
    return out


# --- Atlassian connector (atlassian-connector spec §A11) ---------------------


def _atlassian_token_ok(site_url: str, email: str, token: str) -> tuple[dict | None, str]:
    """Verify an Atlassian API token by calling ``/rest/api/3/myself`` (Basic
    auth). Returns ``(user_json, "")`` on success or ``(None, error_message)`` —
    the response carries the service account's ``accountId`` (the bot anchor +
    bot-loop filter) and cloud-agnostic identity. Never logs the token."""
    import base64
    import urllib.error
    import urllib.request as _urlreq

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = _urlreq.Request(
        f"{site_url.rstrip('/')}/rest/api/3/myself",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()), ""
    except urllib.error.HTTPError as exc:
        return None, f"Atlassian rejected the token (HTTP {exc.code})"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"could not reach Atlassian: {exc}"


def _resolve_cloud_id(site_url: str, email: str, token: str) -> str:
    """Resolve the site's cloud id via ``/_edge/tenant_info`` (unauthenticated on
    the site host, but we send auth anyway). Returns "" on failure — the admin can
    also supply it explicitly."""
    import base64
    import urllib.error
    import urllib.request as _urlreq

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = _urlreq.Request(
        f"{site_url.rstrip('/')}/_edge/tenant_info",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            return str(json.loads(resp.read().decode()).get("cloudId", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _connect_atlassian_site(event: dict, body: dict) -> dict:
    """One-click site connect (§A11): verify the token → resolve cloud id + bot
    account → store the SecureString → write the site row (active). The paste-
    token onboarding is the deliberate deviation from the Slack posture — this is
    the only route with ssm:PutParameter, scoped to the atlassian path."""
    site_url = (body.get("site_url") or "").strip().rstrip("/")
    email = (body.get("bot_email") or "").strip()
    token = (body.get("api_token") or "").strip()
    if not re.match(r"^https://[A-Za-z0-9][A-Za-z0-9.-]*$", site_url):
        return error(400, "body.site_url must be https://<your-site>.atlassian.net")
    if not email or not token:
        return error(400, "body.bot_email and body.api_token are required")

    user, err = _atlassian_token_ok(site_url, email, token)
    if user is None:
        return error(400, f"{err} — double-check the service-account email + scoped API token")
    bot_account_id = str(user.get("accountId", "") or "")
    if not config_store.valid_atlassian_account_id(bot_account_id):
        return error(502, f"Atlassian returned an unexpected accountId: {bot_account_id!r}")

    site_id = (body.get("site_id") or "").strip() or _resolve_cloud_id(site_url, email, token)
    if not config_store.valid_atlassian_site_id(site_id):
        return error(
            400,
            "could not resolve the site cloud id automatically — pass body.site_id "
            "(the uuid from <site>/_edge/tenant_info)",
        )

    # Store the token SecureString at the derived path (the ONE Atlassian secret).
    import boto3 as _boto3

    ssm = _boto3.client("ssm")
    param = config_store.atlassian_token_param(STAGE, site_id)
    ssm.put_parameter(Name=param, Value=token, Type="SecureString", Overwrite=True)

    try:
        rec = config_store.put_atlassian_site(
            site_id,
            site_url=site_url,
            site_name=(body.get("site_name") or "").strip(),
            stage=STAGE,
            enabled=True,
            products=body.get("products") or {"jira": False, "confluence": False},
            bot_account_id=bot_account_id,
            bot_email=email,
            token_expires_at=body.get("token_expires_at"),
            onboarded_by=auth.caller_email(event),
            status=config_store.ATLASSIAN_SITE_ACTIVE,
        )
    except ValueError as exc:
        return error(400, str(exc))
    # A container row change renders into the Cedar container forbids — sync so a
    # freshly-connected site's (empty) allowlists are consistent on the gateway.
    _sync_after_write(
        f"policy sync failed after connecting Atlassian site {site_id}",
        "site connected but the gateway policy update failed; retry",
    )
    return ok(rec)


def _bounded_repos(repos) -> list[str]:
    """Bound a container's linked-repo list to the fleet's onboarded repos — a
    project/space can't link a repo the fleet doesn't manage (mirrors the notif
    sub bounding). Shape-checking of the keys happens in config_store."""
    requested = {config_store._normalize_repo(r) for r in (repos or []) if r and str(r).strip()}
    onboarded = {r["repo"] for r in config_store.list_repos()}
    return sorted(requested & onboarded)


def _automation_grant_principal(rule: dict) -> str:
    return config_store.automation_principal(rule["connector"], rule["rule_id"])


def _author_automation_grant(rule: dict, caller: str) -> None:
    """Auto-author the permit trigger_rule for a rule's synthetic principal
    (§A8.4) — a deterministic rule id so create/enable overwrites rather than
    duplicates. The automation dispatch then evaluates through the same AVP path;
    deleting/disabling removes the grant (default-deny backstop)."""
    principal = _automation_grant_principal(rule)
    # The grant's concrete workspace is the connector's workspace id: an
    # Atlassian site for jira/confluence. GitHub has no Slack-team/site-shaped
    # workspace (the repo scoping lives in the rule's match.repo), so the grant
    # is workspace-agnostic ("*") — otherwise put_trigger_rule would validate a
    # repo string against the Slack-team shape and reject it.
    match = rule.get("match") or {}
    if rule["connector"] in ("jira", "confluence"):
        workspace = match.get("site", "*") or "*"
    else:
        workspace = "*"
    config_store.put_trigger_rule(
        connector=rule["connector"],
        subject_type=config_store.RULE_SUBJECT_USER,
        subject_id=principal,
        agent_id=rule["action"]["agent_id"],
        workspace=workspace,
        effect=config_store.RULE_PERMIT,
        created_by=caller,
        rule_id=f"auto-{rule['rule_id']}",
    )


def _remove_automation_grant(rule: dict) -> None:
    config_store.delete_trigger_rule(f"auto-{rule['rule_id']}")


def _route(event: dict) -> dict:
    resource = event.get("resource", "")
    method = event.get("httpMethod", "")
    path_params = event.get("pathParameters") or {}
    body = _parse_body(event)

    if resource == "/admin/repos":
        if method == "GET":
            return ok({"repos": config_store.list_repos()})
        if method == "POST":
            return _onboard_repos(event, body)

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
    if resource == "/admin/tool-catalog":
        if method == "GET":
            # The grantable read/write tool catalog for the authoring UI (§3.5);
            # destructive tools are excluded (never grantable).
            return ok({"tools": fleet_policy.tool_catalog()})

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

    if resource in (
        "/admin/capabilities/{agent_id}/clone",
        "/admin/capabilities/{agent_id+}/clone",
    ):
        agent_id = (path_params.get("agent_id") or path_params.get("agent_id+") or "").strip()
        if method == "POST":
            return _clone_capability(event, agent_id, body)

    if resource in (
        "/admin/capabilities/{agent_id}/approve",
        "/admin/capabilities/{agent_id+}/approve",
    ):
        agent_id = (path_params.get("agent_id") or path_params.get("agent_id+") or "").strip()
        if method == "POST":
            return _approve_capability(event, agent_id)

    # --- Skills (spec §6) ---
    if resource == "/admin/skills":
        import skill_store

        if method == "GET":
            return ok({"skills": skill_store.list_skills()})
        if method == "POST":
            # Upload: body carries either `content` (raw .md) or `zip_base64`
            # (base64-encoded .zip). `scope` defaults to "shared" and is
            # allowlisted HERE (not only in skill_store) because it flows raw
            # into the S3 key prefix and the capability-row scope field —
            # defense in depth at the API boundary (§6.1).
            scope = (body.get("scope") or "shared").strip()
            if scope not in skill_store.VALID_SCOPES:
                return error(
                    400,
                    f"body.scope must be one of {list(skill_store.VALID_SCOPES)}",
                )
            if body.get("zip_base64"):
                # A .zip is untrusted artifact ingestion (§6.3): unpack it in the
                # ISOLATED skill-unpacker Lambda, never here. Stage the raw bytes
                # to the skills bucket and hand the unpacker just the key — the
                # admin Lambda never expands the zip in-process.
                import base64
                try:
                    data = base64.b64decode(body["zip_base64"])
                except (ValueError, TypeError):
                    return error(400, "zip_base64 is not valid base64")
                return _unpack_skill_zip_isolated(data, scope)
            elif body.get("content"):
                # A raw .md carries no archive-expansion risk (no zip-slip/symlink/
                # bomb surface), so it's validated + stored inline.
                try:
                    ref = skill_store.upload_skill_md(body["content"], scope=scope)
                except skill_store.SkillValidationError as exc:
                    return error(400, str(exc))
                return ok(ref)
            else:
                return error(400, "body must include 'content' (markdown) or 'zip_base64'")

    if resource in ("/admin/skills/{key}", "/admin/skills/{key+}"):
        key = (path_params.get("key") or path_params.get("key+") or "").strip()
        if method == "DELETE":
            import skill_store

            parts = key.split("/", 1)
            scope = parts[0] if len(parts) == 2 else "shared"
            name = parts[-1]
            # Both halves become raw S3 key segments in the deleted prefix —
            # shape-check them so a crafted key can't delete outside the
            # skills/<scope>/<name>/ tree. DELETE deliberately accepts any
            # path-safe scope (not just VALID_SCOPES): packages uploaded before
            # the scope allowlist existed may sit under a legacy free-form
            # scope, and they must stay deletable or they orphan forever in the
            # versioned bucket. Uploads remain allowlisted.
            if not skill_store._SKILL_NAME_RE.match(scope):
                return error(400, f"invalid skill scope {scope!r}")
            if not skill_store._SKILL_NAME_RE.match(name):
                return error(400, f"invalid skill name {name!r}")
            deleted = skill_store.delete_skill(scope, name)
            return ok({"name": name, "scope": scope, "deleted": deleted})

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

    if resource == "/admin/github-app/repos" and method == "GET":
        import github_client

        # Everything the App can reach RIGHT NOW, for the onboarding picker:
        # each installation with its accessible repos, plus which of them are
        # already onboarded. Live from GitHub (no cache) — the picker's whole
        # point is reflecting an install/selection change the admin just made.
        if not github_client.app_configured():
            return ok({"configured": False, "installations": []})
        onboarded = {r["repo"] for r in config_store.list_repos()}
        try:
            installations = []
            for inst in github_client.list_installations():
                repos = github_client.list_installation_repos(
                    inst["installation_id"]
                )
                installations.append(
                    {
                        **inst,
                        "repos": [
                            {**r, "onboarded": r["repo"].casefold() in onboarded}
                            for r in repos
                        ],
                    }
                )
        except github_client.GitHubError as exc:
            logger.exception("listing installation repos failed")
            return error(502, f"could not list the App's repositories: {exc}")
        return ok(
            {
                "configured": True,
                "installations": installations,
                "install_url": github_client.install_url(),
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
                    onboarded_by=auth.caller_email(event),
                    status=config_store.SLACK_WS_ACTIVE,
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource == "/admin/slack/workspaces/connect" and method == "POST":
        # One-click onboarding: admin pastes the bot token + signing secret from
        # their freshly-created Slack app. The backend verifies the token via
        # auth.test (extracts team_id + workspace name), stores both secrets to
        # SSM, and onboards the workspace — no CLI, no team-id typing.
        import urllib.error
        import urllib.request as _urlreq

        bot_token = (body.get("bot_token") or "").strip()
        signing_secret = (body.get("signing_secret") or "").strip()
        if not bot_token or not bot_token.startswith("xoxb-"):
            return error(400, "bot_token must be a Slack bot token (starts with xoxb-)")
        if not signing_secret or len(signing_secret) < 16:
            return error(400, "signing_secret is required (32-char hex from Basic Information)")
        # Verify the token by calling auth.test — confirms it works AND gives us
        # team_id + team name so the admin never types them.
        try:
            req = _urlreq.Request(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {bot_token}"},
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=10) as resp:
                slack_resp = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError) as exc:
            return error(502, f"could not reach Slack API: {exc}")
        if not slack_resp.get("ok"):
            return error(
                400,
                f"Slack rejected the bot token: {slack_resp.get('error', 'unknown')} "
                "— double-check you copied the Bot User OAuth Token (starts with xoxb-).",
            )
        team_id = slack_resp.get("team_id", "")
        team_name = slack_resp.get("team", "")
        if not config_store.valid_slack_team(team_id):
            return error(502, f"Slack returned an unexpected team_id format: {team_id}")
        # Store secrets to SSM (the same paths bootstrap_slack.py would write).
        import boto3 as _boto3

        ssm = _boto3.client("ssm")
        signing_param = f"/sdlc-agents/{STAGE}/slack/signing-secret"
        bot_param = config_store._slack_bot_token_param(STAGE, team_id)
        ssm.put_parameter(Name=signing_param, Value=signing_secret, Type="SecureString", Overwrite=True)
        ssm.put_parameter(Name=bot_param, Value=bot_token, Type="SecureString", Overwrite=True)
        # Onboard the workspace row (active immediately).
        try:
            rec = config_store.put_slack_workspace(
                team_id,
                team_name=team_name,
                stage=STAGE,
                enabled=True,
                default_channel_policy=(body.get("default_channel_policy") or config_store.CHANNEL_POLICY_ALLOWLIST),
                onboarded_by=auth.caller_email(event),
                status=config_store.SLACK_WS_ACTIVE,
            )
        except ValueError as exc:
            return error(400, str(exc))
        return ok(rec)

    if resource == "/admin/slack/workspaces/{team_id}/manifest" and method == "GET":
        # Hand the admin the ready-to-paste Slack app manifest (parallels
        # github-app/setup/manifest). The webhook API base is env-supplied
        # (a different API Gateway than this admin API).
        import slack_manifest

        webhook_base = os.environ.get("WEBHOOK_API_BASE", "")
        if not webhook_base:
            return error(500, "WEBHOOK_API_BASE not configured")
        qs = event.get("queryStringParameters") or {}
        app_name = qs.get("app_name") or "SDLC Agent Fleet"
        return ok({"manifest": slack_manifest.build_manifest(webhook_base, app_name)})

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
                    created_by=auth.caller_email(event),
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
            rules = config_store.list_trigger_rules(connector)
            return ok({"rules": _decorate_trigger_rules(rules, _LabelDirectory())})
        if method == "POST":
            try:
                rec = config_store.put_trigger_rule(
                    connector=(body.get("connector") or "").strip(),
                    subject_type=(body.get("subject_type") or "").strip(),
                    subject_id=(body.get("subject_id") or "").strip(),
                    agent_id=(body.get("agent_id") or "*").strip(),
                    workspace=(body.get("workspace") or "*").strip(),
                    effect=(body.get("effect") or config_store.RULE_PERMIT).strip(),
                    created_by=auth.caller_email(event),
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
            requests_ = config_store.list_channel_requests(status)
            directory = _LabelDirectory()
            decorated = []
            for req in requests_:
                row = dict(req)
                row["requested_by_label"] = directory.person(req.get("requested_by", ""))
                row["workspace_label"] = directory.workspace(req.get("team_id", ""))
                if not (req.get("channel_name") or "").strip():
                    row["channel_label"] = directory.channel_id(
                        req.get("team_id", ""), req.get("channel_id", "")
                    )
                decorated.append(row)
            return ok({"requests": decorated})

    if resource in ("/admin/channel-requests/{request_id}/approve",
                    "/admin/channel-requests/{request_id}/deny"):
        if method == "POST":
            request_id = (path_params.get("request_id") or "").strip()
            approve = resource.endswith("/approve")
            return _decide_channel_request(event, request_id, approve, body)

    # --- Identities (cross-source user map, spec §16) ---
    if resource == "/admin/identities":
        if method == "GET":
            return ok({"identities": config_store.list_identities()})
        if method == "POST":
            # Admin proactive-create: born active with the supplied handles/email.
            try:
                rec = config_store.put_identity(
                    email=(body.get("email") or "").strip(),
                    display_name=(body.get("display_name") or "").strip(),
                    handles=body.get("handles") or {},
                    groups=body.get("groups") or [],
                    status=(body.get("status") or config_store.IDENTITY_ACTIVE),
                    onboarded_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource in ("/admin/identities/{identity_id}", "/admin/identities/{identity_id+}"):
        identity_id = (path_params.get("identity_id") or path_params.get("identity_id+") or "").strip()
        if method == "GET":
            rec = config_store.get_identity(identity_id)
            return ok(rec) if rec else error(404, f"no such identity: {identity_id}")
        if method == "PUT":
            # Edit groups / status (the access + lifecycle knobs).
            if "groups" in body:
                try:
                    config_store.set_identity_groups(identity_id, body.get("groups") or [])
                except ValueError as exc:
                    return error(400, str(exc))
            if "status" in body:
                try:
                    config_store.set_identity_status(identity_id, body["status"])
                except ValueError as exc:
                    return error(400, str(exc))
            rec = config_store.get_identity(identity_id)
            return ok(rec) if rec else error(404, f"no such identity: {identity_id}")
        if method == "DELETE":
            return ok({"identity_id": identity_id, "deleted": config_store.delete_identity(identity_id)})

    # --- User-onboarding requests (approve/deny queue, spec §16.4) ---
    if resource == "/admin/user-requests":
        if method == "GET":
            status = (event.get("queryStringParameters") or {}).get("status")
            return ok({"requests": config_store.list_user_requests(status)})

    if resource in ("/admin/user-requests/{request_id}/approve",
                    "/admin/user-requests/{request_id}/deny"):
        if method == "POST":
            request_id = (path_params.get("request_id") or "").strip()
            approve = resource.endswith("/approve")
            return _decide_user_request(event, request_id, approve, body)

    # --- Permission groups (spec §17) ---
    if resource == "/admin/groups":
        if method == "GET":
            groups = config_store.list_perm_groups()
            # Annotate each with its member count (cheap; the directory is small).
            for g in groups:
                g["member_count"] = len(config_store.group_members(g["group_id"]))
            return ok({"groups": groups})
        if method == "POST":
            group_id = (body.get("group_id") or "").strip()
            try:
                rec = config_store.put_perm_group(
                    group_id,
                    name=(body.get("name") or "").strip(),
                    description=(body.get("description") or "").strip(),
                    recommended=bool(body.get("recommended", False)),
                    created_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource in ("/admin/groups/{group_id}", "/admin/groups/{group_id+}"):
        group_id = (path_params.get("group_id") or path_params.get("group_id+") or "").strip()
        if method == "GET":
            rec = config_store.get_perm_group(group_id)
            if not rec:
                return error(404, f"no such group: {group_id}")
            rec["members"] = [
                {"identity_id": m["identity_id"], "email": m.get("email", ""),
                 "display_name": m.get("display_name", "")}
                for m in config_store.group_members(group_id)
            ]
            return ok(rec)
        if method == "DELETE":
            return ok({"group_id": group_id, "deleted": config_store.delete_perm_group(group_id)})

    # --- Notification subscriptions (read/edit; self-served in Slack, spec §18.5) ---
    if resource == "/admin/notif-subs":
        if method == "GET":
            team_id = (event.get("queryStringParameters") or {}).get("team_id")
            subs = config_store.list_notif_subs(team_id)
            directory = _LabelDirectory()
            decorated = []
            for s in subs:
                row = dict(s)
                row["channel_label"] = directory.channel_id(
                    s.get("team_id", ""), s.get("channel_id", "")
                )
                row["workspace_label"] = directory.workspace(s.get("team_id", ""))
                decorated.append(row)
            return ok({"subscriptions": decorated})
        if method == "POST":
            team_id = (body.get("team_id") or "").strip()
            channel_id = (body.get("channel_id") or "").strip()
            # Bound the requested repos to what the channel is actually granted
            # (§18.2) — the store validates shape, the API validates authorization.
            requested = {config_store._normalize_repo(r) for r in (body.get("repos") or []) if r}
            granted = _channel_granted_repos(team_id, channel_id)
            disallowed = requested - granted
            if disallowed:
                return error(403, f"channel not granted repos: {sorted(disallowed)}")
            try:
                rec = config_store.put_notif_sub(
                    team_id, channel_id,
                    repos=sorted(requested),
                    tiers=body.get("tiers") or {},
                    min_severity=(body.get("min_severity") or config_store.NOTIF_TIER_INFORMATIVE),
                    created_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource == "/admin/notif-subs/{team_id}/{channel_id}":
        if method == "DELETE":
            team_id = (path_params.get("team_id") or "").strip()
            channel_id = (path_params.get("channel_id") or "").strip()
            return ok({"deleted": config_store.delete_notif_sub(team_id, channel_id)})

    # --- Atlassian: sites (atlassian-connector spec §A11) ---
    if resource == "/admin/atlassian/sites":
        if method == "GET":
            return ok({"sites": config_store.list_atlassian_sites()})
        if method == "POST":
            # Direct row write (products toggle / metadata) — connect is separate.
            site_id = (body.get("site_id") or "").strip()
            try:
                rec = config_store.put_atlassian_site(
                    site_id,
                    site_url=(body.get("site_url") or "").strip(),
                    site_name=(body.get("site_name") or "").strip(),
                    stage=STAGE,
                    enabled=bool(body.get("enabled", True)),
                    products=body.get("products"),
                    bot_account_id=(body.get("bot_account_id") or "").strip(),
                    bot_email=(body.get("bot_email") or "").strip(),
                    onboarded_by=auth.caller_email(event),
                    status=(body.get("status") or config_store.ATLASSIAN_SITE_ACTIVE),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)

    if resource == "/admin/atlassian/sites/connect" and method == "POST":
        return _connect_atlassian_site(event, body)

    if resource == "/admin/atlassian/forge-status" and method == "GET":
        # The forwarder's app id + private install link, published to SSM by
        # scripts/deploy_forge_atlassian.py (run from deploy_fleet). Lets the
        # Sites tab render the install card without any operator hand-off;
        # deployed=false tells the admin the forwarder still needs its one-time
        # CLI deploy (Forge auth is interactive, so it can't run server-side).
        import boto3 as _boto3

        _ssm = _boto3.client("ssm")
        out = {"deployed": False, "app_id": "", "install_link": ""}
        try:
            out["app_id"] = _ssm.get_parameter(
                Name=f"/sdlc-agents/{STAGE}/atlassian/forge-app-id"
            )["Parameter"]["Value"]
            out["install_link"] = _ssm.get_parameter(
                Name=f"/sdlc-agents/{STAGE}/atlassian/forge-install-link"
            )["Parameter"]["Value"]
            out["deployed"] = bool(out["app_id"])
        except Exception:  # noqa: BLE001 — not yet deployed is a normal state
            pass
        return ok(out)

    if resource in ("/admin/atlassian/sites/{site_id}", "/admin/atlassian/sites/{site_id+}"):
        site_id = (path_params.get("site_id") or path_params.get("site_id+") or "").strip()
        if method == "PUT":
            # Per-product enablement toggles.
            rec = config_store.set_atlassian_products(site_id, body.get("products") or {})
            if rec is None:
                return error(404, f"no such site: {site_id}")
            # Enabling/disabling a product changes which container forbids apply.
            _sync_after_write(
                f"policy sync failed after product toggle for {site_id}",
                "products updated but the gateway policy update failed; retry",
            )
            return ok(rec)
        if method == "DELETE":
            return ok({"site_id": site_id, "deleted": config_store.delete_atlassian_site(site_id)})

    if resource == "/admin/atlassian/sites/{site_id}/products" and method == "PUT":
        site_id = (path_params.get("site_id") or "").strip()
        rec = config_store.set_atlassian_products(site_id, body.get("products") or {})
        if rec is None:
            return error(404, f"no such site: {site_id}")
        _sync_after_write(
            f"policy sync failed after product toggle for {site_id}",
            "products updated but the gateway policy update failed; retry",
        )
        return ok(rec)

    if resource == "/admin/atlassian/sites/{site_id}/verify-webhook" and method == "POST":
        # Per-product delivery liveness — reads receiver-stamped webhook_last_seen.
        site_id = (path_params.get("site_id") or "").strip()
        product = (event.get("queryStringParameters") or {}).get("product", "")
        site = config_store.get_atlassian_site(site_id)
        if site is None:
            return error(404, f"no such site: {site_id}")
        last = (site.get("webhook_last_seen") or {})
        return ok({"site_id": site_id, "product": product,
                   "last_seen": last.get(product) if product else last})

    # --- Atlassian: Jira projects ---
    if resource == "/admin/atlassian/projects":
        if method == "GET":
            site_id = (event.get("queryStringParameters") or {}).get("site_id")
            return ok({"projects": config_store.list_jira_projects(site_id)})
        if method == "POST":
            try:
                repos = _bounded_repos(body.get("repos"))
                rec = config_store.put_jira_project(
                    (body.get("site_id") or "").strip(),
                    (body.get("project_key") or "").strip(),
                    mode=(body.get("mode") or config_store.CONTAINER_MODE_ALLOW).strip(),
                    project_name=(body.get("project_name") or "").strip(),
                    repos=repos,
                    note=(body.get("note") or "").strip(),
                    created_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            # Writes render into sdlc_allowed_projects — sync under the rollback
            # invariant (the container forbid is the gateway-plane boundary).
            failure = _sync_after_write(
                "policy sync failed after Jira project change",
                "project saved but the gateway policy update failed; retry",
            )
            if failure is not None:
                return failure
            return ok(rec)

    if resource == "/admin/atlassian/projects/{site_id}/{key}":
        if method == "DELETE":
            site_id = (path_params.get("site_id") or "").strip()
            key = (path_params.get("key") or "").strip()
            deleted = config_store.delete_jira_project(site_id, key)
            _sync_after_write(
                "policy sync failed after Jira project delete",
                "project removed but the gateway policy update failed; retry",
            )
            return ok({"deleted": deleted})

    # --- Atlassian: Confluence spaces ---
    if resource == "/admin/atlassian/spaces":
        if method == "GET":
            site_id = (event.get("queryStringParameters") or {}).get("site_id")
            return ok({"spaces": config_store.list_confluence_spaces(site_id)})
        if method == "POST":
            try:
                repos = _bounded_repos(body.get("repos"))
                rec = config_store.put_confluence_space(
                    (body.get("site_id") or "").strip(),
                    (body.get("space_key") or "").strip(),
                    mode=(body.get("mode") or config_store.CONTAINER_MODE_ALLOW).strip(),
                    space_name=(body.get("space_name") or "").strip(),
                    write_mode=(body.get("write_mode") or config_store.CONFLUENCE_WRITE_PROPOSE).strip(),
                    write_agents=body.get("write_agents") or [],
                    repos=repos,
                    note=(body.get("note") or "").strip(),
                    created_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            failure = _sync_after_write(
                "policy sync failed after Confluence space change",
                "space saved but the gateway policy update failed; retry",
            )
            if failure is not None:
                return failure
            return ok(rec)

    if resource == "/admin/atlassian/spaces/{site_id}/{key}":
        if method == "DELETE":
            site_id = (path_params.get("site_id") or "").strip()
            key = (path_params.get("key") or "").strip()
            deleted = config_store.delete_confluence_space(site_id, key)
            _sync_after_write(
                "policy sync failed after Confluence space delete",
                "space removed but the gateway policy update failed; retry",
            )
            return ok({"deleted": deleted})

    # --- Automation rules (§A8) ---
    if resource == "/admin/automation-rules":
        if method == "GET":
            connector = (event.get("queryStringParameters") or {}).get("connector")
            return ok({"rules": config_store.list_automation_rules(connector)})
        if method == "POST":
            try:
                rec = config_store.put_automation_rule(
                    connector=(body.get("connector") or "").strip(),
                    event=(body.get("event") or "").strip(),
                    match=body.get("match") or {},
                    agent_id=(body.get("agent_id") or "").strip(),
                    instruction_template=(body.get("instruction_template") or ""),
                    enabled=bool(body.get("enabled", True)),
                    cooldown_seconds=int(body.get("cooldown_seconds", 3600)),
                    created_by=auth.caller_email(event),
                )
            except (ValueError, TypeError) as exc:
                return error(400, str(exc))
            # Create auto-authors the automation grant (§A8.4) when enabled.
            if rec.get("enabled"):
                _author_automation_grant(rec, auth.caller_email(event))
            return ok(rec)

    if resource in ("/admin/automation-rules/{rule_id}", "/admin/automation-rules/{rule_id+}"):
        rule_id = (path_params.get("rule_id") or path_params.get("rule_id+") or "").strip()
        if method == "PUT":
            existing = config_store.get_automation_rule(rule_id)
            if existing is None:
                return error(404, f"no such automation rule: {rule_id}")
            try:
                rec = config_store.put_automation_rule(
                    connector=(body.get("connector") or existing["connector"]).strip(),
                    event=(body.get("event") or existing["event"]).strip(),
                    match=body.get("match") if body.get("match") is not None else existing.get("match", {}),
                    agent_id=(body.get("agent_id") or existing["action"]["agent_id"]).strip(),
                    instruction_template=(body.get("instruction_template")
                                          or existing["action"]["instruction_template"]),
                    enabled=bool(body.get("enabled", existing.get("enabled", True))),
                    cooldown_seconds=int(body.get("cooldown_seconds", existing.get("cooldown_seconds", 3600))),
                    created_by=existing.get("created_by", ""),
                    rule_id=rule_id,
                )
            except (ValueError, TypeError) as exc:
                return error(400, str(exc))
            if rec.get("enabled"):
                _author_automation_grant(rec, auth.caller_email(event))
            else:
                _remove_automation_grant(rec)
            return ok(rec)
        if method == "DELETE":
            existing = config_store.get_automation_rule(rule_id)
            if existing is not None:
                _remove_automation_grant(existing)
            return ok({"rule_id": rule_id, "deleted": config_store.delete_automation_rule(rule_id)})

    if resource in ("/admin/automation-rules/{rule_id}/enable",
                    "/admin/automation-rules/{rule_id}/disable"):
        if method == "POST":
            rule_id = (path_params.get("rule_id") or "").strip()
            enable = resource.endswith("/enable")
            rec = config_store.set_automation_rule_enabled(rule_id, enable)
            if rec is None:
                return error(404, f"no such automation rule: {rule_id}")
            if enable:
                _author_automation_grant(rec, auth.caller_email(event))
            else:
                _remove_automation_grant(rec)
            return ok(rec)

    # --- Per-user DM notification prefs (§A9.2) ---
    if resource in ("/admin/notif-prefs/{identity_id}", "/admin/notif-prefs/{identity_id+}"):
        identity_id = (path_params.get("identity_id") or path_params.get("identity_id+") or "").strip()
        if method == "GET":
            rec = config_store.get_notif_pref(identity_id)
            return ok(rec or {"identity_id": identity_id, "tiers": {}})
        if method == "PUT":
            # Only an active identity with a verified Slack handle may hold prefs.
            ident = config_store.get_identity(identity_id)
            if ident is None or ident.get("status") != config_store.IDENTITY_ACTIVE:
                return error(400, "identity must be active to hold notification prefs")
            if not (ident.get("verified") or {}).get("slack"):
                return error(400, "identity has no verified Slack handle for DMs")
            try:
                rec = config_store.put_notif_pref(
                    identity_id,
                    tiers=body.get("tiers") or {},
                    min_tier=(body.get("min_tier") or config_store.NOTIF_TIER_ACTIONABLE),
                    created_by=auth.caller_email(event),
                )
            except ValueError as exc:
                return error(400, str(exc))
            return ok(rec)
        if method == "DELETE":
            return ok({"identity_id": identity_id, "deleted": config_store.delete_notif_pref(identity_id)})

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
