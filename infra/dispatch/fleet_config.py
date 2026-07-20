"""Fleet configuration reader for the Dispatch Router.

Read side of the runtime config the admin API writes (see
infra/dashboard/config_store.py — the two share this table's schema as a
contract, but the packages deploy independently so neither imports the other).
This replaces the deploy-time ``FLEET_GITHUB_REPO`` single-repo binding: which
repos may dispatch is now runtime-mutable, set by an admin in the dashboard.

Table shape (``FLEET_CONFIG_TABLE`` env var, partition key ``pk``):
  - Repo record:  pk="repo#<owner/repo>", {kind:"repo", repo, enabled(bool),
                  multi_repo_eligible(bool), co_repo_mode, repo_group?,
                  status: "pending"|"active", ...}
  - Settings:     pk="settings",          {kind:"settings", restrict_repos(bool)}

Dispatch allow rule (``is_repo_allowed``): a GitHub mention's repo is
dispatchable iff it is onboarded, ``enabled``, and ``active`` (a ``pending`` row
whose Gateway policy sync hasn't landed is NOT yet dispatchable — dispatch never
widens ahead of the tool-call policy). When ``restrict_repos`` is on, the repo
must additionally be ``multi_repo_eligible`` — that is the admin's "optionally
restrict which repos are allowed" allowlist.

Co-repo reach (``coreachable_repos``): which OTHER repos a dispatch that
ORIGINATES in a given repo may operate on — none (``co_repo_mode="isolated"``),
its ``repo_group`` peers (``"group"``), or every eligible repo (``"all"``). This
is the read-side mirror of ``config_store.coreachable_repos`` (same schema
contract); the token minters use it to scope the minted GitHub App credential to
exactly the co-approved set, so a dispatch physically cannot touch a repo it isn't
approved to run with.

The config is read through a short-TTL cache so an admin change takes effect
fleet-wide within ``_CACHE_TTL_SECONDS`` (the previous registry cache was
unbounded for the warm-container lifetime — the freshness bug this fixes).
"""

import os
import time

import boto3

_REPO_PK_PREFIX = "repo#"
_SETTINGS_PK = "settings"

# Co-repo reach modes (mirror config_store.CO_REPO_*).
CO_REPO_ISOLATED = "isolated"
CO_REPO_GROUP = "group"
CO_REPO_ALL = "all"

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
    cheaper than the GSI a constant-PK Query would need. We page on
    LastEvaluatedKey regardless: a single scan() returns only the first 1MB
    page, and a repo (or the settings row) beyond it would silently drop out of
    the allowlist — a legitimately onboarded repo would then be rejected as "not
    onboarded", or a missed settings row would default restrict_repos to False.
    """
    table = _get_table()
    settings = {"restrict_repos": False}
    repos: dict[str, dict] = {}
    start_key = None
    while True:
        resp = table.scan(ExclusiveStartKey=start_key) if start_key else table.scan()
        for item in resp.get("Items", []):
            pk = str(item.get("pk", ""))
            if pk == _SETTINGS_PK:
                settings = {"restrict_repos": bool(item.get("restrict_repos", False))}
            elif pk.startswith(_REPO_PK_PREFIX):
                repo = str(item.get("repo", "")).strip()
                if repo:
                    repos[repo.casefold()] = item
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
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


def mantle_project_for(repo: str) -> str | None:
    """The Bedrock Mantle project id onboarded for ``repo`` (owner/repo), or None.
    The router injects this into the dispatch context so the agent attributes its
    model cost/usage to the repo's project. Fails soft: unknown repo → None (the
    agent then uses the account's default project)."""
    norm = (repo or "").strip().casefold()
    if not norm:
        return None
    rec = _snapshot()["repos"].get(norm)
    return rec.get("mantle_project") if rec else None


def _is_eligible_active(record: dict) -> bool:
    return bool(
        record.get("enabled")
        and record.get("multi_repo_eligible")
        and record.get("status") == "active"
    )


def is_repo_cross_repo_eligible(repo: str) -> bool:
    """Whether ``repo`` (owner/repo) may participate in cross-repo tool actions —
    onboarded + enabled + active + multi_repo_eligible. Used by the SCM broker to
    reject a gateway tool call against a repo that isn't cleared for cross-repo
    use. Fails closed on an empty/unknown repo."""
    norm = (repo or "").strip().casefold()
    if not norm:
        return False
    rec = _snapshot()["repos"].get(norm)
    return bool(rec and _is_eligible_active(rec))


def coreachable_repos(origin: str) -> list[str]:
    """The repos a dispatch that ORIGINATES in ``origin`` (owner/repo) may operate
    on — the enforced "approved to run with" set. Read-side mirror of
    ``config_store.coreachable_repos``:

      - always includes ``origin`` when it's onboarded/enabled/active;
      - isolated → just ``origin``;
      - group    → + eligible repos sharing ``origin``'s ``repo_group`` (mutual);
      - all      → + every eligible repo in the fleet (any owner/org).

    Fails closed: an origin that isn't onboarded/enabled/active returns ``[]``.
    Spans owners — a group or ``all`` may freely mix individual and org repos."""
    norm = (origin or "").strip().casefold()
    if not norm:
        return []
    snapshot = _snapshot()
    repos = snapshot["repos"]
    rec = repos.get(norm)
    if not rec or not rec.get("enabled") or rec.get("status") != "active":
        return []

    reachable = {norm}
    if rec.get("multi_repo_eligible"):
        mode = rec.get("co_repo_mode", CO_REPO_ISOLATED)
        if mode == CO_REPO_ALL:
            reachable.update(
                r["repo"] for r in repos.values() if _is_eligible_active(r)
            )
        elif mode == CO_REPO_GROUP:
            group = rec.get("repo_group")
            if group:
                reachable.update(
                    r["repo"]
                    for r in repos.values()
                    if _is_eligible_active(r)
                    and r.get("co_repo_mode") == CO_REPO_GROUP
                    and r.get("repo_group") == group
                )
    return sorted(reachable, key=lambda r: (r != norm, r))
