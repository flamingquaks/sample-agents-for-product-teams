"""Fleet configuration store — DynamoDB access for onboarded repos + settings.

This is the runtime-mutable config the single-repo → multi-repo transformation
introduced: the admin API writes it, and the Dispatch Router reads it (via its
own minimal reader in infra/dispatch, sharing this table's schema as a
contract). It replaces the deploy-time ``FLEET_GITHUB_REPO`` parameter.

Table shape (single table, ``FLEET_CONFIG_TABLE`` env var). Partition key ``pk``:
  - Repo record:   pk="repo#<owner/repo>",  {kind:"repo", repo, enabled(bool),
                   multi_repo_eligible(bool), onboarded_by, onboarded_at,
                   status: "pending"|"active"}
  - Settings:      pk="settings",           {kind:"settings", restrict_repos(bool)}

``enabled``            — the repo is dispatchable (a mention from it is routed).
``multi_repo_eligible``— the repo may be targeted by cross-repo tool actions
                         (the set the Gateway Cedar policy allows). A repo can be
                         enabled-but-not-eligible: dispatchable, tool-calls denied.
``restrict_repos``     — when False, any enabled repo is allowed; when True, only
                         enabled + eligible repos (the allowlist) are.
"""

import os
import time

import boto3

_REPO_PK_PREFIX = "repo#"
_SETTINGS_PK = "settings"

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
    onboarded_by: str = "",
    status: str = "pending",
) -> dict:
    """Create/replace a repo record. ``status`` defaults to ``pending``; the
    admin onboarding flow instead writes ``active`` and rolls back to ``pending``
    if the Gateway policy sync fails — because ``allowed_repos()`` only returns
    ``active`` rows, so syncing while ``pending`` would omit the very repo being
    onboarded. See admin.py's POST /admin/repos."""
    normalized = _normalize_repo(repo)
    item = {
        "pk": _repo_pk(normalized),
        "kind": "repo",
        "repo": normalized,
        "enabled": bool(enabled),
        "multi_repo_eligible": bool(multi_repo_eligible),
        "onboarded_by": onboarded_by,
        "onboarded_at": int(time.time()),
        "status": status,
    }
    _get_table().put_item(Item=item)
    return item


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


# --- derived -----------------------------------------------------------------


def allowed_repos() -> list[str]:
    """The repos allowed for cross-repo tool actions — the set rendered into the
    Gateway Cedar policy's ``context.input.repo in [...]``. Enabled + eligible."""
    return [
        r["repo"]
        for r in list_repos()
        if r.get("enabled")
        and r.get("multi_repo_eligible")
        and r.get("status") == "active"
    ]
