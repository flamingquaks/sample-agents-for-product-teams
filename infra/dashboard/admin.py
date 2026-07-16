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

import auth
import config_store
from http_responses import error, ok

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PolicySyncError(Exception):
    """Raised when the Gateway Cedar policy could not be updated to match the
    intended repo allowlist. Callers surface this as a 5xx and do NOT report the
    config change as complete."""


def _sync_repo_policy() -> None:
    """Regenerate + push the single fleet Cedar policy from the current allowed
    set. No-op seam until WS5 (Gateway migration) wires the policy engine.

    WS5 will: compute config_store.allowed_repos(), render the forbid-unless-in
    Cedar statement, and CreatePolicy/update the named fleet policy via
    bedrock-agentcore-control, raising PolicySyncError on failure / Cedar
    Analysis rejection / not reaching the intended enforcement mode."""
    logger.info(
        "policy sync (no-op until WS5): allowed=%s", config_store.allowed_repos()
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
    """Cheap owner/repo shape check — exactly one slash, non-empty halves, no
    whitespace. Not a GitHub existence check."""
    if not isinstance(repo, str) or repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    return bool(owner.strip()) and bool(name.strip()) and " " not in repo


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
            # 1) write the intent as pending, 2) sync the tool-call policy,
            # 3) only then mark active. If the policy sync fails we leave the row
            # pending and report an error — dispatch won't treat it as allowed.
            config_store.put_repo(
                repo,
                enabled=enabled,
                multi_repo_eligible=eligible,
                onboarded_by=auth.caller_sub(event),
                status="pending",
            )
            try:
                _sync_repo_policy()
            except PolicySyncError:
                logger.exception("policy sync failed for %s; left pending", repo)
                return error(
                    502,
                    f"repo recorded but policy update failed for {repo}; left pending, retry",
                )
            config_store.set_repo_status(repo, "active")
            return ok(config_store.get_repo(repo))

    if resource == "/admin/repos/{repo}":
        if method == "DELETE":
            repo = path_params.get("repo")
            if not repo:
                return error(400, "missing repo")
            config_store.delete_repo(repo)
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
            return ok({"repo": repo, "deleted": True})

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
