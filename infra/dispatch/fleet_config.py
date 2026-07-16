"""Fleet configuration reader for the Dispatch Router.

Read side of the runtime config the admin API writes (see
infra/dashboard/config_store.py — the two share this table's schema as a
contract, but the packages deploy independently so neither imports the other).
This replaces the deploy-time ``FLEET_GITHUB_REPO`` single-repo binding: which
repos may dispatch is now runtime-mutable, set by an admin in the dashboard.

Table shape (``FLEET_CONFIG_TABLE`` env var, partition key ``pk``):
  - Repo record:  pk="repo#<owner/repo>", {kind:"repo", repo, enabled(bool),
                  multi_repo_eligible(bool), status: "pending"|"active", ...}
  - Settings:     pk="settings",          {kind:"settings", restrict_repos(bool)}

Dispatch allow rule (``is_repo_allowed``): a GitHub mention's repo is
dispatchable iff it is onboarded, ``enabled``, and ``active`` (a ``pending`` row
whose Gateway policy sync hasn't landed is NOT yet dispatchable — dispatch never
widens ahead of the tool-call policy). When ``restrict_repos`` is on, the repo
must additionally be ``multi_repo_eligible`` — that is the admin's "optionally
restrict which repos are allowed" allowlist.

The config is read through a short-TTL cache so an admin change takes effect
fleet-wide within ``_CACHE_TTL_SECONDS`` (the previous registry cache was
unbounded for the warm-container lifetime — the freshness bug this fixes).
"""

import os
import time

import boto3

_REPO_PK_PREFIX = "repo#"
_SETTINGS_PK = "settings"

# Short TTL so admin onboarding/removal propagates within a minute across warm
# Lambda containers, without a DynamoDB read on every dispatch.
_CACHE_TTL_SECONDS = 30

_table = None
# Cache holds the whole config snapshot (settings + repo map) since it's tiny.
_cache = None  # {"settings": {...}, "repos": {casefolded_repo: record}}
_cache_expires_at = 0.0


def _get_table():
    global _table
    if _table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


def _load_snapshot() -> dict:
    """Read the full config (settings + repo records) from DynamoDB.

    The table is small (a handful of repos + one settings row), so a Scan is
    cheaper than the GSI a constant-PK Query would need.
    """
    resp = _get_table().scan()
    settings = {"restrict_repos": False}
    repos: dict[str, dict] = {}
    for item in resp.get("Items", []):
        pk = str(item.get("pk", ""))
        if pk == _SETTINGS_PK:
            settings = {"restrict_repos": bool(item.get("restrict_repos", False))}
        elif pk.startswith(_REPO_PK_PREFIX):
            repo = str(item.get("repo", "")).strip()
            if repo:
                repos[repo.casefold()] = item
    return {"settings": settings, "repos": repos}


def _snapshot(now: float | None = None) -> dict:
    """Return the cached config snapshot, refreshing when the TTL has expired."""
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


def is_repo_allowed(repo: str) -> bool:
    """Whether a GitHub dispatch from ``repo`` (owner/repo) is allowed.

    Fails closed: an empty/unresolvable repo is never allowed. GitHub owner/repo
    is case-insensitive, so match casefolded.
    """
    normalized = (repo or "").strip()
    if not normalized:
        return False
    snapshot = _snapshot()
    record = snapshot["repos"].get(normalized.casefold())
    if record is None:
        return False
    if not record.get("enabled") or record.get("status") != "active":
        return False
    if snapshot["settings"]["restrict_repos"] and not record.get("multi_repo_eligible"):
        return False
    return True
