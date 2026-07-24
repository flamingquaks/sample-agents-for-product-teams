"""Workspace token vendor Lambda (durable-repo-work spec, Phase 1).

Agents hold no GitHub credential (T-4). The durable-workspace tool
(``agents/shared/tools/workspace.py``) needs real ``git clone``/``push`` against
GitHub, so it invokes THIS Lambda per git network operation to mint a
short-lived GitHub App installation token scoped to exactly one repo with
exactly the permission the operation needs (``contents:read`` for clone/fetch,
``contents:write`` for push). The token lives only in the agent process's
memory for the one git command — never in env, ``.git/config``, or disk.

Authorization mirrors the SCM broker's boundaries:

- **Who can call it at all**: only the per-agent runtime roles are granted
  ``lambda:InvokeFunction`` on this function (capability deployer + boundary).
- **Repo boundary**: the repo must be onboarded + multi-repo eligible, and
  co-reachable from the dispatch ORIGIN. The origin is stamped by runtime code
  from the dispatch payload — the same trust model as the gateway's
  ``x-dispatch-origin`` header (the model controls tool arguments, never this).
- **Permission ceiling**: the requested contents level is intersected with the
  calling agent's GitHub permission tier (``scm_broker.AGENT_GITHUB_PERMISSIONS``,
  with custom/unknown agents falling back to a read-only tier). An agent whose
  tier has no ``contents`` grant gets nothing — fail closed.

Returns ``{"token": "..."}`` or ``{"error": "..."}`` (never raises to the
caller with a partial body; the workspace tool surfaces the error string to the
model as a tool failure it can react to).
"""

import logging

import fleet_config
import github_app
from scm_broker import AGENT_GITHUB_PERMISSIONS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Tier applied when the calling agent has no built-in tier (custom agents).
# Read-only: a custom agent can clone/build/test out of the box; pushing needs
# a contents:write tier, which today only built-ins carry. Revisit when the
# authoring UI grows a per-capability GitHub tier.
_DEFAULT_TIER = {"contents": "read", "metadata": "read"}

_LEVELS = {"read": 1, "write": 2}


def _err(message: str) -> dict:
    logger.warning("workspace_token_vendor: %s", message)
    return {"error": message}


def handler(event, context=None):
    """Direct-invoke entry point. Event: {repo, agent, origin, write}."""
    event = event if isinstance(event, dict) else {}
    repo = str(event.get("repo", "")).strip()
    agent = str(event.get("agent", "")).strip()
    origin = str(event.get("origin", "")).strip()
    write = bool(event.get("write"))

    if "/" not in repo:
        return _err("repo must be 'owner/name'")

    # Same repo boundary as the broker: onboarded + multi-repo eligible, and
    # co-reachable from the dispatch origin. No origin → no repo reach at all.
    if not fleet_config.is_repo_cross_repo_eligible(repo):
        return _err(f"repo '{repo}' is not onboarded + multi-repo-eligible")
    if not origin:
        return _err("no dispatch origin — workspace credential refused")
    reachable = {r.casefold() for r in fleet_config.coreachable_repos(origin)}
    if repo.casefold() not in reachable:
        return _err(f"repo '{repo}' is not approved to run with '{origin}'")

    # Permission ceiling: requested level ∩ the agent's tier.
    tier = AGENT_GITHUB_PERMISSIONS.get(agent, _DEFAULT_TIER)
    granted = tier.get("contents")
    if not granted:
        return _err(f"agent '{agent}' has no repository-contents access")
    need = "write" if write else "read"
    if _LEVELS[need] > _LEVELS[granted]:
        return _err(f"agent '{agent}' may not {need} repository contents")

    try:
        token = github_app.scoped_installation_token(
            repo, permissions={"contents": need, "metadata": "read"}
        )
    except github_app.GitHubAppError as exc:
        return _err(f"could not mint workspace credential for {repo}: {exc}")

    logger.info(
        "workspace_token_vendor: minted contents:%s token repo=%s agent=%s origin=%s",
        need, repo, agent, origin,
    )
    return {"token": token}
