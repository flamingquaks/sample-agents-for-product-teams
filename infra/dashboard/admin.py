"""Dashboard admin API Lambda — fleet configuration (write surface).

Admin-only counterpart to the read-only query API (api.py). Members of the
``admins`` Cognito group onboard/enable/disable repos and toggle the
restrict-to-allowlist setting. Every route requires auth.is_admin (fails
closed); the read API's operators can view but not configure.

Routes (all admin-only):
    GET    /admin/repos                  list onboarded repos
    POST   /admin/repos                  onboard/update a repo (body: repo, enabled?, multi_repo_eligible?)
    DELETE /admin/repos/{repo}           remove a repo
    GET    /admin/settings               get fleet settings
    PUT    /admin/settings               update settings (body: restrict_repos)

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
import re
from urllib.parse import unquote

import auth
import config_store
from http_responses import error, ok

# GitHub owner/repo segment: letters, digits, hyphen, underscore, dot. This is
# stricter than GitHub's own rules but a safe superset for real repos, and it is
# the security boundary that keeps repo names out of the Cedar policy as
# metacharacters — the owner/repo strings are interpolated into the generated
# Cedar statement (fleet_policy.py), so a name containing a quote/pipe/newline
# could otherwise inject into or break the policy.
_REPO_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PolicySyncError(Exception):
    """Raised when the Gateway Cedar policy could not be updated to match the
    intended repo allowlist. Callers surface this as a 5xx and do NOT report the
    config change as complete."""


def _sync_repo_policy() -> None:
    """Regenerate + push the single fleet Cedar policy from the current allowed
    set to the Gateway policy engine.

    Delegates to policy_sync.sync_fleet_policy, which renders the forbid-unless-in
    Cedar statement from config_store.allowed_repos() and CreatePolicy/updates the
    named fleet policy via bedrock-agentcore-control — raising PolicySyncError on
    failure, Cedar-analysis rejection, or not reaching the intended enforcement
    mode. A no-op (logged) until the gateway is provisioned (POLICY_ENGINE_ID
    unset), so the dispatch allowlist still works before WS5's gateway lands.

    Imported lazily so the admin API's config routes don't hard-depend on the
    control-plane client (and tests can monkeypatch this seam directly)."""
    import policy_sync

    policy_sync.sync_fleet_policy()


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
            # 1) write the row active, 2) sync the tool-call policy from the
            # allowed set (which now INCLUDES this repo — allowed_repos() only
            # returns active rows, so syncing while pending would omit the very
            # repo being onboarded), 3) roll back to pending if the sync fails.
            # A repo is only ever left active once its policy sync succeeded, so
            # dispatch never treats as allowed a repo the tool-call policy still
            # denies. During the brief window before the sync lands the policy
            # still denies (the safe direction), and the dispatch config cache is
            # stale anyway, so dispatch does not widen ahead of the policy.
            config_store.put_repo(
                repo,
                enabled=enabled,
                multi_repo_eligible=eligible,
                onboarded_by=auth.caller_sub(event),
                status="active",
            )
            try:
                _sync_repo_policy()
            except PolicySyncError:
                logger.exception("policy sync failed for %s; rolling back", repo)
                config_store.set_repo_status(repo, "pending")
                return error(
                    502,
                    f"repo recorded but policy update failed for {repo}; left pending, retry",
                )
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
            # Removing a repo narrows the allowlist — sync the policy so its tool
            # calls stop being allowed. A sync failure here is non-fatal to the
            # delete (the repo is already gone from dispatch) but is reported.
            try:
                _sync_repo_policy()
            except PolicySyncError:
                logger.exception("policy sync failed after deleting %s", repo)
                return error(
                    502, "repo removed from dispatch but policy update failed; retry"
                )
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
            try:
                _sync_repo_policy()
            except PolicySyncError:
                logger.exception("policy sync failed after settings change")
                return error(502, "settings saved but policy update failed; retry")
            return ok(settings)

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
