"""SCM broker — the AgentCore Gateway's GitHub tool target (spec §3.6).

In **gateway mode** agents don't open a direct bearer client to GitHub's MCP
server; they call one AgentCore Gateway, which enforces Cedar and fans out to
targets. GitHub App installation tokens are per-owner and minted per call, which
a static per-target credential can't express — so instead of pointing the gateway
at ``api.githubcopilot.com`` (which needed the retired shared PAT on the gateway
role), we point it at THIS Lambda target. It:

  - Exposes a curated, provider-agnostic tool schema (``tool_definitions()``) —
    exactly the tools the agents' Cedar grants use (``fleet_policy``), not the
    full GitHub MCP surface.
  - Dispatches on the gateway's ``bedrockAgentCoreToolName`` context value
    (``<Target>___<tool>``), stripping the target prefix to get the tool.
  - Mints the per-call credential IN OUR CODE — the same per-owner GitHub App
    installation-token flow the direct-mode agents/reply Lambda use
    (``github_app.installation_token_for_repo``), resolved from the ``owner``/
    ``repo`` tool args.
  - Calls GitHub's REST API and returns the result as JSON.
  - Logs every tool call (tool, owner/repo, provider, outcome, latency) on top
    of the gateway's native metrics.

Credential model: the gateway invokes this Lambda with ``GATEWAY_IAM_ROLE`` (no
outbound OAuth/API-key provider on the target); the Lambda itself reads the App
private key + mints tokens. Cedar still evaluates every call at the gateway, so
the broker doesn't need the caller identity — the per-agent ``permit`` policies
(keyed on each runtime role's ARN) remain the authoritative per-agent tool scope.

Provider resolution: GitHub only for v1. When GitLab/Bitbucket land, add a
``provider`` field to the owner/repo records and branch here; their credential
models plug in alongside the GitHub App path.
"""

import base64
import json
import logging
import time

import requests

import fleet_config
import github_app

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GITHUB_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_TOOL_DELIM = "___"

# The gateway target Name — the "<TargetName>" half of the tool/action id. MUST
# match infra/foundation/template.yaml's GitHubGatewayTarget Name AND
# infra/dashboard/fleet_policy.GITHUB_TARGET, so the Cedar action names
# (<Target>___<tool>) the policy engine evaluates line up with what agents call.
TARGET_NAME = "GitHubTarget"

# Server-truth args the REQUEST interceptor injects (never model-supplied). The
# broker scopes the credential from THESE, not the model-visible owner/repo.
ORIGIN_ARG = "_dispatch_origin"
AGENT_ARG = "_dispatch_agent"

# Per-agent GitHub App PERMISSION tier — mirror of
# infra/dashboard/fleet_policy.AGENT_GITHUB_PERMISSIONS (a cross-package test
# asserts equality). The broker intersects this agent tier with the per-tool
# least-privilege set (_TOOL_PERMISSIONS) so a token is bounded by BOTH: the
# agent's role ceiling AND what the specific tool needs. e.g. workitems has no
# ``contents`` grant, so even the code tools can't mint a code-write token for it.
AGENT_GITHUB_PERMISSIONS = {
    "workitems": {"issues": "write", "pull_requests": "read", "metadata": "read"},
    "docwriter": {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    },
    "adr": {
        "contents": "read",
        "issues": "write",
        "pull_requests": "read",
        "metadata": "read",
    },
    "researcher": {},
}

# Ordering for intersecting a tool's needed level with an agent's granted level.
_LEVELS = {"read": 1, "write": 2}


class BrokerError(Exception):
    """A broker dispatch/credential failure (not a GitHub 4xx — those are
    returned as structured tool results so the agent can react)."""


# --- GitHub REST helper ------------------------------------------------------


def _gh(method: str, path: str, token: str, *, params=None, body=None):
    """Call the GitHub REST API and return ``(status, parsed_json)``. ``path`` is
    relative to the API base. Never logs the token or the response body (either
    can carry credential material)."""
    resp = requests.request(
        method,
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        },
        params=params or None,
        json=body if body is not None else None,
        timeout=15,
    )
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    return resp.status_code, data


# --- per-tool implementations ------------------------------------------------
#
# Each takes (token, owner, repo, args) and returns the JSON tool result. Tool
# NAMES must match fleet_policy (WRITE_TOOLS ∪ AGENT_TOOL_GRANTS github tools) so
# the Cedar action names stay valid. Destructive tools (delete/merge/close) are
# deliberately ABSENT — the fleet forbids them (fleet_policy.DESTRUCTIVE_TOOLS)
# and the broker gives them no surface at all (defense in depth: even if a Cedar
# forbid were misconfigured, there's no code path here to merge/delete).


def _get_issue(token, owner, repo, args):
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/issues/{args['issue_number']}", token)
    return d


def _list_issues(token, owner, repo, args):
    params = {k: args[k] for k in ("state", "labels", "sort", "direction") if k in args}
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/issues", token, params=params)
    return d


def _get_pull_request(token, owner, repo, args):
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/pulls/{args['pull_number']}", token)
    return d


def _list_pull_requests(token, owner, repo, args):
    params = {k: args[k] for k in ("state", "base", "head", "sort") if k in args}
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/pulls", token, params=params)
    return d


def _list_milestones(token, owner, repo, args):
    params = {k: args[k] for k in ("state", "sort", "direction") if k in args}
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/milestones", token, params=params)
    return d


def _list_commits(token, owner, repo, args):
    params = {k: args[k] for k in ("sha", "path", "since", "until") if k in args}
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/commits", token, params=params)
    return d


def _get_file_contents(token, owner, repo, args):
    params = {"ref": args["ref"]} if args.get("ref") else None
    _s, d = _gh(
        "GET", f"/repos/{owner}/{repo}/contents/{args['path']}", token, params=params
    )
    return d


def _search_code(token, owner, repo, args):
    # GitHub code search is a global endpoint; scope it to this repo so the
    # per-owner token's reach and the query agree.
    query = f"{args['query']} repo:{owner}/{repo}"
    _s, d = _gh("GET", "/search/code", token, params={"q": query})
    return d


def _create_issue(token, owner, repo, args):
    body = {"title": args["title"]}
    for k in ("body", "labels", "assignees", "milestone"):
        if k in args:
            body[k] = args[k]
    _s, d = _gh("POST", f"/repos/{owner}/{repo}/issues", token, body=body)
    return d


def _update_issue(token, owner, repo, args):
    body = {
        k: args[k]
        for k in ("title", "body", "state", "labels", "assignees", "milestone")
        if k in args
    }
    _s, d = _gh(
        "PATCH", f"/repos/{owner}/{repo}/issues/{args['issue_number']}", token, body=body
    )
    return d


def _add_issue_comment(token, owner, repo, args):
    _s, d = _gh(
        "POST",
        f"/repos/{owner}/{repo}/issues/{args['issue_number']}/comments",
        token,
        body={"body": args["body"]},
    )
    return d


def _add_labels_to_issue(token, owner, repo, args):
    _s, d = _gh(
        "POST",
        f"/repos/{owner}/{repo}/issues/{args['issue_number']}/labels",
        token,
        body={"labels": args["labels"]},
    )
    return d


def _create_pull_request(token, owner, repo, args):
    body = {"title": args["title"], "head": args["head"], "base": args["base"]}
    for k in ("body", "draft"):
        if k in args:
            body[k] = args[k]
    _s, d = _gh("POST", f"/repos/{owner}/{repo}/pulls", token, body=body)
    return d


def _create_pull_request_review(token, owner, repo, args):
    body = {"event": args.get("event", "COMMENT")}
    if "body" in args:
        body["body"] = args["body"]
    _s, d = _gh(
        "POST",
        f"/repos/{owner}/{repo}/pulls/{args['pull_number']}/reviews",
        token,
        body=body,
    )
    return d


def _create_or_update_file(token, owner, repo, args):
    # GitHub's Contents API takes base64 content. Callers pass raw text; encode
    # it here so the tool schema stays simple ("content": string).
    content_b64 = base64.b64encode(args["content"].encode()).decode()
    body = {"message": args["message"], "content": content_b64}
    for k in ("branch", "sha"):
        if k in args:
            body[k] = args[k]
    _s, d = _gh(
        "PUT", f"/repos/{owner}/{repo}/contents/{args['path']}", token, body=body
    )
    return d


def _default_branch(token, owner, repo):
    _s, d = _gh("GET", f"/repos/{owner}/{repo}", token)
    return d.get("default_branch", "main")


def _ref_sha(token, owner, repo, branch):
    _s, d = _gh("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
    return (d.get("object") or {}).get("sha")


def _create_branch(token, owner, repo, args):
    branch = args["branch"]
    source = args.get("from_branch") or _default_branch(token, owner, repo)
    sha = _ref_sha(token, owner, repo, source)
    if not sha:
        return {"isError": True, "message": f"could not resolve source ref '{source}'"}
    _s, d = _gh(
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        token,
        body={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    return d


def _push_files(token, owner, repo, args):
    """Commit multiple files to a branch in one commit via the git data API:
    resolve the branch tip → build a tree on top of it → create a commit →
    advance the ref. ``files`` is ``[{path, content}]`` (raw text)."""
    branch = args["branch"]
    base_sha = _ref_sha(token, owner, repo, branch)
    if not base_sha:
        return {"isError": True, "message": f"branch '{branch}' not found"}
    _s, commit = _gh("GET", f"/repos/{owner}/{repo}/git/commits/{base_sha}", token)
    base_tree = (commit.get("tree") or {}).get("sha")

    tree = []
    for f in args["files"]:
        _s, blob = _gh(
            "POST",
            f"/repos/{owner}/{repo}/git/blobs",
            token,
            body={"content": f["content"], "encoding": "utf-8"},
        )
        tree.append(
            {"path": f["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]}
        )
    _s, new_tree = _gh(
        "POST",
        f"/repos/{owner}/{repo}/git/trees",
        token,
        body={"base_tree": base_tree, "tree": tree},
    )
    _s, new_commit = _gh(
        "POST",
        f"/repos/{owner}/{repo}/git/commits",
        token,
        body={
            "message": args["message"],
            "tree": new_tree["sha"],
            "parents": [base_sha],
        },
    )
    _s, ref = _gh(
        "PATCH",
        f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
        token,
        body={"sha": new_commit["sha"]},
    )
    return ref


# Per-tool LEAST-PRIVILEGE GitHub App permission set. The broker mints a token
# scoped to exactly the repo being called AND exactly the permission the specific
# tool needs — so a read tool's token can't write, and an issue-comment token
# can't touch code. metadata:read is implicit for every installation token.
#   contents  — file/branch/commit reads + writes (code)
#   issues    — issues + issue comments + labels
#   pull_requests — PR reads + creates + reviews
_READ = "read"
_WRITE = "write"
_TOOL_PERMISSIONS = {
    "get_issue": {"issues": _READ},
    "list_issues": {"issues": _READ},
    "get_pull_request": {"pull_requests": _READ},
    "list_pull_requests": {"pull_requests": _READ},
    "list_milestones": {"issues": _READ},
    "list_commits": {"contents": _READ},
    "get_file_contents": {"contents": _READ},
    "search_code": {"contents": _READ},
    "create_issue": {"issues": _WRITE},
    "update_issue": {"issues": _WRITE},
    "add_issue_comment": {"issues": _WRITE},
    "add_labels_to_issue": {"issues": _WRITE},
    "create_pull_request": {"pull_requests": _WRITE, "contents": _READ},
    "create_pull_request_review": {"pull_requests": _WRITE},
    "create_or_update_file": {"contents": _WRITE},
    "push_files": {"contents": _WRITE},
    "create_branch": {"contents": _WRITE},
}

# Registry: tool name → (implementation, {required arg names}). The required set
# is what tool_definitions() marks required in the exposed input schema.
#
# INVARIANT — every tool MUST require both ``owner`` and ``repo``. This is
# load-bearing for security, not just ergonomics: the REQUEST interceptor
# (scm_interceptor.py) only enforces co-repo grouping and injects the server-truth
# ``_dispatch_origin``/``_dispatch_agent`` when BOTH ``owner`` and ``repo`` are
# present in the tool args (its gate at scm_interceptor.py `handler`). A tool
# registered without both would slip through the interceptor's passthrough path
# with the model-visible args untouched — the broker's origin check would then no
# longer be gated by the interceptor at all. The broker still fails closed on a
# missing/absent origin, but the interceptor is the primary control; keep every
# tool owner+repo-scoped so that control always fires. ``test_every_tool_requires
# _owner_and_repo`` guards this.
_TOOLS = {
    "get_issue": (_get_issue, {"owner", "repo", "issue_number"}),
    "list_issues": (_list_issues, {"owner", "repo"}),
    "get_pull_request": (_get_pull_request, {"owner", "repo", "pull_number"}),
    "list_pull_requests": (_list_pull_requests, {"owner", "repo"}),
    "list_milestones": (_list_milestones, {"owner", "repo"}),
    "list_commits": (_list_commits, {"owner", "repo"}),
    "get_file_contents": (_get_file_contents, {"owner", "repo", "path"}),
    "search_code": (_search_code, {"owner", "repo", "query"}),
    "create_issue": (_create_issue, {"owner", "repo", "title"}),
    "update_issue": (_update_issue, {"owner", "repo", "issue_number"}),
    "add_issue_comment": (_add_issue_comment, {"owner", "repo", "issue_number", "body"}),
    "add_labels_to_issue": (
        _add_labels_to_issue,
        {"owner", "repo", "issue_number", "labels"},
    ),
    "create_pull_request": (
        _create_pull_request,
        {"owner", "repo", "title", "head", "base"},
    ),
    "create_pull_request_review": (
        _create_pull_request_review,
        {"owner", "repo", "pull_number"},
    ),
    "create_or_update_file": (
        _create_or_update_file,
        {"owner", "repo", "path", "content", "message"},
    ),
    "push_files": (_push_files, {"owner", "repo", "branch", "files", "message"}),
    "create_branch": (_create_branch, {"owner", "repo", "branch"}),
}


def _tool_name_from_context(context) -> str:
    """Extract the tool name the gateway invoked, stripping the ``<Target>___``
    prefix. The gateway puts the full name in the Lambda client-context custom
    map (``bedrockAgentCoreToolName``)."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "")
    if _TOOL_DELIM in full:
        return full.split(_TOOL_DELIM, 1)[1]
    return full


def _effective_permissions(tool: str, agent: str | None) -> dict[str, str]:
    """The token permissions for this call: the per-tool least-privilege set
    INTERSECTED with the agent's tier — bounded by BOTH. A permission is granted
    only if the tool needs it AND the agent's tier allows it, at the lower level.
    An agent with no GitHub tier (e.g. researcher) gets nothing (fail closed)."""
    tool_perms = _TOOL_PERMISSIONS[tool]
    if agent is None:
        # No agent identity → can't bound to a tier; the tool-level least
        # privilege still applies (the interceptor normally injects the agent).
        return dict(tool_perms)
    tier = AGENT_GITHUB_PERMISSIONS.get(agent)
    if not tier:
        raise BrokerError(f"agent '{agent}' has no GitHub access tier")
    out = {}
    for perm, need in tool_perms.items():
        granted = tier.get(perm)
        if not granted:
            continue  # agent's tier doesn't include this permission at all
        # take the lower of {tool needs, agent granted}
        level = "read" if min(_LEVELS[need], _LEVELS[granted]) == 1 else "write"
        out[perm] = level
    return out


def handler(event, context):
    """AgentCore Gateway Lambda-target entry point. ``event`` is the tool's input
    arguments (a flat map of the input-schema properties); the tool name comes
    from the gateway context. Returns the tool result as JSON.

    The trusted dispatch origin + agent are injected by the REQUEST interceptor
    (``_dispatch_origin`` / ``_dispatch_agent``) — server truth, not model input.
    The broker re-checks co-repo grouping (defense in depth behind the
    interceptor) and mints a token scoped to the repo + the per-agent∩per-tool
    permission set."""
    tool = _tool_name_from_context(context)
    args = event if isinstance(event, dict) else {}
    owner = str(args.get("owner", "")).strip()
    repo = str(args.get("repo", "")).strip()
    origin = str(args.get(ORIGIN_ARG, "")).strip()
    agent = str(args.get(AGENT_ARG, "")).strip() or None

    entry = _TOOLS.get(tool)
    if entry is None:
        # Unknown/destructive tool — no surface here. (Cedar also forbids it.)
        logger.warning("scm_broker: unknown tool %r", tool)
        raise BrokerError(f"unknown tool '{tool}'")

    impl, required = entry
    missing = [a for a in required if not args.get(a) and args.get(a) != 0]
    if missing:
        raise BrokerError(f"tool '{tool}' missing required args: {sorted(missing)}")

    # The target repo must be onboarded, enabled, active, AND multi-repo-eligible.
    if not fleet_config.is_repo_cross_repo_eligible(f"{owner}/{repo}"):
        logger.warning("scm_broker: repo %s/%s not cross-repo eligible", owner, repo)
        raise BrokerError(
            f"repo '{owner}/{repo}' is not onboarded + multi-repo-eligible"
        )

    # Re-check co-repo grouping from the trusted origin (the interceptor already
    # enforced it; this is defense in depth so a misconfigured/absent interceptor
    # can't let a cross-group call through). No origin → no cross-repo authority.
    if not origin:
        raise BrokerError(
            "no trusted dispatch origin on the call — cross-repo action refused"
        )
    reachable = {r.casefold() for r in fleet_config.coreachable_repos(origin)}
    if f"{owner}/{repo}".casefold() not in reachable:
        raise BrokerError(
            f"repo '{owner}/{repo}' is not approved to run with '{origin}'"
        )

    permissions = _effective_permissions(tool, agent)

    start = time.time()
    outcome = "error"
    try:
        # Mint a token scoped to EXACTLY this repo with the per-agent tier ∩ the
        # per-tool least privilege (bounded by BOTH). Same per-owner GitHub App
        # the reply Lambda uses (threat-model T-11), narrowed per call.
        token = github_app.scoped_installation_token(
            f"{owner}/{repo}", permissions=permissions
        )
        result = impl(token, owner, repo, args)
        outcome = "error" if isinstance(result, dict) and result.get("isError") else "ok"
        return result
    except github_app.GitHubAppError as exc:
        outcome = "auth_error"
        logger.error("scm_broker: token mint failed for %s/%s: %s", owner, repo, exc)
        raise BrokerError(f"could not mint GitHub credential for {owner}/{repo}") from exc
    except requests.RequestException as exc:
        outcome = "network_error"
        logger.error("scm_broker: GitHub call failed for %s (%s/%s): %s", tool, owner, repo, exc)
        raise BrokerError(f"GitHub call failed for {tool}") from None
    finally:
        logger.info(
            "scm_broker tool=%s provider=github repo=%s/%s outcome=%s latency_ms=%d",
            tool,
            owner,
            repo,
            outcome,
            int((time.time() - start) * 1000),
        )


# --- tool schema (for the gateway target ToolSchema.InlinePayload) -----------


def _prop(name: str) -> dict:
    """The schema for a single tool property. Array types carry an ``items``
    schema (AgentCore SchemaDefinition requires it for arrays)."""
    ptype = _PROP_TYPES.get(name, "string")
    if ptype == "array":
        # labels → array of strings; files → array of {path, content} objects.
        if name == "files":
            return {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            }
        return {"type": "array", "items": {"type": "string"}}
    return {"type": ptype}


def _schema(required: set[str]) -> dict:
    """A JSON-schema-ish input schema for a tool. We keep the property set small
    and permissive (string owner/repo + the tool's required params); the broker
    passes optional args through when present. This is the single source of truth
    for the tool schema — tests assert the template's inline copy matches."""
    props = {name: _prop(name) for name in sorted(required)}
    return {
        "type": "object",
        "properties": props,
        "required": sorted(required),
    }


_PROP_TYPES = {
    "issue_number": "integer",
    "pull_number": "integer",
    "labels": "array",
    "files": "array",
    "draft": "boolean",
}


def tool_definitions() -> list[dict]:
    """The gateway target ToolSchema InlinePayload: one ToolDefinition per tool.
    Deterministic (sorted) so the template's inline copy can be diffed/verified.
    """
    return [
        {
            "name": name,
            "description": f"GitHub {name.replace('_', ' ')} (per-owner App token).",
            "inputSchema": _schema(required),
        }
        for name, (_impl, required) in sorted(_TOOLS.items())
    ]


if __name__ == "__main__":  # pragma: no cover — regenerate the template schema
    print(json.dumps(tool_definitions(), indent=2))
