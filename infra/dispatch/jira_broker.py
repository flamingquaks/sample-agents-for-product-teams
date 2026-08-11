"""Jira broker — the AgentCore Gateway's Jira tool target (atlassian-connector
spec §B2).

Same enforced-broker idiom as ``scm_broker`` / ``confluence_broker``: the
gateway invokes this Lambda with GATEWAY_IAM_ROLE, Cedar authorizes per-agent
tool grants at the gateway, and the broker adds what the site-wide token can't:

  - derives ``project_key`` from the issue-key prefix and REQUIRES arg
    consistency (an explicit ``project_key`` must match the issue's prefix),
  - checks the onboarded-project allowlist (reads are container-checked too, but
    Jira's Cedar forbid covers writes — matching GitHub's posture, §C2.4),
  - then calls Jira REST v3 with the site service-account token.

The remote Atlassian MCP is deliberately rejected in favor of this curated
schema (§A4 layer-4 rationale): the vendor MCP exposes deletes, a DYNAMIC-listing
trap, and no container scoping. Destructive ops (any delete, worklog/sprint/board
writes, project/user admin) are ABSENT by construction — no code path here.
"""

import json
import logging
import time

import atlassian_client as ac
import trigger_grants

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TARGET_NAME = "JiraTarget"
_TOOL_DELIM = "___"

ORIGIN_ARG = "_dispatch_origin"
AGENT_ARG = "_dispatch_agent"

_SIGNATURE = "🤖 **[{agent}]** · run {origin}"

CLASS_READ = "read"
CLASS_WRITE = "write"


class BrokerError(Exception):
    """A broker dispatch/credential failure (not a REST 4xx)."""


def _sig_line(agent, origin):
    return _SIGNATURE.format(agent=agent or "agent", origin=origin or "?")


# --- per-tool implementations -------------------------------------------------


def _get_issue(site, token, args, **_):
    status, data = ac.rest(site, "GET", f"/rest/api/3/issue/{args['issue_key']}", token)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"could not fetch issue {args['issue_key']}"}
    fields = data.get("fields") or {}
    return {
        "issue_key": data.get("key", args["issue_key"]),
        "summary": fields.get("summary", ""),
        "status": ((fields.get("status") or {}).get("name")),
        "issue_type": ((fields.get("issuetype") or {}).get("name")),
        "description": ac.adf_to_text((fields.get("description"))),
        "assignee": ((fields.get("assignee") or {}).get("displayName")),
        "reporter": ((fields.get("reporter") or {}).get("displayName")),
    }


def _get_issue_comments(site, token, args, **_):
    status, data = ac.rest(
        site, "GET", f"/rest/api/3/issue/{args['issue_key']}/comment", token
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"could not fetch comments for {args['issue_key']}"}
    return {"comments": [
        {"author": ((c.get("author") or {}).get("displayName")),
         "created": c.get("created"),
         "text": ac.adf_to_text(c.get("body"))}
        for c in (data.get("comments") or [])
    ]}


def _get_transitions(site, token, args, **_):
    status, data = ac.rest(
        site, "GET", f"/rest/api/3/issue/{args['issue_key']}/transitions", token
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": "could not fetch transitions"}
    return {"transitions": [
        {"id": t.get("id"), "name": t.get("name"),
         "to_status": ((t.get("to") or {}).get("name"))}
        for t in (data.get("transitions") or [])
    ]}


def _search_issues(site, token, args, **_):
    # Container scoping is enforced on the RESULTS, not the query (§B2.4). We
    # still AND the model's JQL under a ``project in (...)`` pin as a first-pass
    # filter, but JQL operator precedence means a crafted model clause could
    # break out of that pin (``…) OR (project = SECRET``) — so the security
    # boundary is the post-filter below: every returned issue's project (the key
    # prefix, server truth) MUST be in the allowlist, regardless of what JQL ran.
    # No onboarded project ⇒ nothing is searchable.
    allowed = _allowed_project_keys(site["site_id"])
    if not allowed:
        return {"isError": True, "message":
                "no Jira projects are onboarded for this site — nothing to search"}
    pin = "project in ({})".format(", ".join(f'"{k}"' for k in sorted(allowed)))
    model_jql = str(args.get("jql", "")).strip()
    jql = f"{pin} AND ({model_jql})" if model_jql else pin
    limit = min(int(args.get("max_results", 50) or 50), 50)
    status, data = ac.rest(
        site, "GET", "/rest/api/3/search", token,
        params={"jql": jql, "maxResults": limit},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"search failed (HTTP {status})"}
    return {"issues": [
        {"issue_key": i.get("key"),
         "summary": ((i.get("fields") or {}).get("summary", "")),
         "status": (((i.get("fields") or {}).get("status") or {}).get("name"))}
        for i in (data.get("issues") or [])
        # Drop any issue outside the allowlist — the injection-proof boundary.
        if _project_of_issue(str(i.get("key", ""))) in allowed
    ]}


def _list_projects(site, token, args, **_):
    # Discovery never leaks non-onboarded projects: the REST result is filtered
    # to the onboarded+allowed set (§B2.4), matching the Confluence list_spaces
    # posture, regardless of what the site-wide token could enumerate.
    allowed = _allowed_project_keys(site["site_id"])
    status, data = ac.rest(site, "GET", "/rest/api/3/project/search", token,
                           params={"maxResults": 100})
    if not 200 <= status < 300:
        return {"isError": True, "message": "could not list projects"}
    return {"projects": [
        {"project_key": p.get("key"), "name": p.get("name")}
        for p in (data.get("values") or [])
        if p.get("key") in allowed
    ]}


def _get_project(site, token, args, **_):
    status, data = ac.rest(site, "GET", f"/rest/api/3/project/{args['project_key']}", token)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"project {args['project_key']} not found"}
    return {"project_key": data.get("key"), "name": data.get("name"),
            "description": data.get("description", "")}


def _add_comment(site, token, args, *, agent, origin):
    signed = f"{args['body']}\n\n{_sig_line(agent, origin)}"
    status, data = ac.rest(
        site, "POST", f"/rest/api/3/issue/{args['issue_key']}/comment", token,
        body={"body": ac.text_to_adf(signed)},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"add_comment failed (HTTP {status})"}
    return {"comment_id": str(data.get("id", ""))}


def _create_issue(site, token, args, *, agent, origin):
    fields = {
        "project": {"key": args["project_key"]},
        "summary": args["summary"],
        "issuetype": {"name": args["issue_type"]},
    }
    if args.get("description"):
        fields["description"] = ac.text_to_adf(args["description"])
    if args.get("labels"):
        fields["labels"] = args["labels"]
    status, data = ac.rest(site, "POST", "/rest/api/3/issue", token, body={"fields": fields})
    if not 200 <= status < 300:
        return {"isError": True, "message": f"create_issue failed (HTTP {status}): {data}"}
    return {"issue_key": data.get("key", "")}


def _update_issue(site, token, args, *, agent, origin):
    fields = {}
    incoming = args.get("fields") or {}
    if "summary" in incoming:
        fields["summary"] = incoming["summary"]
    if "description" in incoming:
        fields["description"] = ac.text_to_adf(incoming["description"])
    if "labels" in incoming:
        fields["labels"] = incoming["labels"]
    if "priority" in incoming:
        fields["priority"] = {"name": incoming["priority"]}
    if not fields:
        return {"isError": True, "message": "update_issue: no supported fields to set"}
    status, _ = ac.rest(site, "PUT", f"/rest/api/3/issue/{args['issue_key']}", token,
                        body={"fields": fields})
    if not 200 <= status < 300:
        return {"isError": True, "message": f"update_issue failed (HTTP {status})"}
    return {"issue_key": args["issue_key"], "updated": sorted(fields.keys())}


def _transition_issue(site, token, args, *, agent, origin):
    # Resolve the named transition to its id.
    _s, tdata = ac.rest(site, "GET", f"/rest/api/3/issue/{args['issue_key']}/transitions", token)
    want = str(args["transition_name"]).casefold()
    tid = next((t.get("id") for t in (tdata.get("transitions") or [])
                if str(t.get("name", "")).casefold() == want), None)
    if not tid:
        return {"isError": True, "message":
                f"no transition named '{args['transition_name']}' is available on "
                f"{args['issue_key']}"}
    body = {"transition": {"id": tid}}
    if args.get("resolution"):
        body["fields"] = {"resolution": {"name": args["resolution"]}}
    status, _ = ac.rest(
        site, "POST", f"/rest/api/3/issue/{args['issue_key']}/transitions", token, body=body
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"transition failed (HTTP {status})"}
    return {"issue_key": args["issue_key"], "transitioned_to": args["transition_name"]}


def _assign_issue(site, token, args, *, agent, origin):
    status, _ = ac.rest(
        site, "PUT", f"/rest/api/3/issue/{args['issue_key']}/assignee", token,
        body={"accountId": args["account_id"]},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"assign failed (HTTP {status})"}
    return {"issue_key": args["issue_key"], "assignee": args["account_id"]}


def _link_issues(site, token, args, *, agent, origin):
    body = {
        "type": {"name": args["link_type"]},
        "inwardIssue": {"key": args["inward_key"]},
        "outwardIssue": {"key": args["outward_key"]},
    }
    status, _ = ac.rest(site, "POST", "/rest/api/3/issueLink", token, body=body)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"link failed (HTTP {status})"}
    return {"linked": [args["inward_key"], args["outward_key"]]}


def _add_remote_link(site, token, args, *, agent, origin):
    body = {"object": {"url": args["url"], "title": args["title"]}}
    status, data = ac.rest(
        site, "POST", f"/rest/api/3/issue/{args['issue_key']}/remotelink", token, body=body
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"add_remote_link failed (HTTP {status})"}
    return {"issue_key": args["issue_key"], "link_id": str(data.get("id", ""))}


# tool → (impl, {required}, class). Writes require project_key (the allowlist +
# arg-consistency axis); reads take the issue_key/jql they need.
_TOOLS = {
    "get_issue": (_get_issue, {"site", "issue_key"}, CLASS_READ),
    "get_issue_comments": (_get_issue_comments, {"site", "issue_key"}, CLASS_READ),
    "get_transitions": (_get_transitions, {"site", "issue_key"}, CLASS_READ),
    "search_issues": (_search_issues, {"site", "jql"}, CLASS_READ),
    "list_projects": (_list_projects, {"site"}, CLASS_READ),
    "get_project": (_get_project, {"site", "project_key"}, CLASS_READ),
    "add_comment": (_add_comment, {"site", "issue_key", "project_key", "body"}, CLASS_WRITE),
    "create_issue": (_create_issue, {"site", "project_key", "issue_type", "summary"}, CLASS_WRITE),
    "update_issue": (_update_issue, {"site", "issue_key", "project_key", "fields"}, CLASS_WRITE),
    "transition_issue": (_transition_issue, {"site", "issue_key", "project_key", "transition_name"}, CLASS_WRITE),
    "assign_issue": (_assign_issue, {"site", "issue_key", "project_key", "account_id"}, CLASS_WRITE),
    "link_issues": (_link_issues, {"site", "inward_key", "outward_key", "project_key", "link_type"}, CLASS_WRITE),
    "add_remote_link": (_add_remote_link, {"site", "issue_key", "project_key", "url", "title"}, CLASS_WRITE),
}

_PROP_TYPES = {"max_results": "integer", "fields": "object", "labels": "array"}


def _project_of_issue(issue_key: str) -> str:
    """The project key prefix of an issue key (``ENG-142`` → ``ENG``)."""
    return (issue_key or "").split("-", 1)[0]


def _allowed_project_keys(site_id: str) -> set[str]:
    """Onboarded + ALLOW-mode Jira project keys for a site (via the shared
    snapshot) — the set the project-less reads (search_issues/list_projects) are
    scoped to, mirroring the Confluence broker's _allowed_space_keys."""
    keys = set()
    from trigger_grants import _snapshot

    for (product, sid, key), _row in _snapshot().get("atlassian_containers", {}).items():
        if product == "jira" and sid == site_id:
            if trigger_grants.container_allowed(site_id, key, "jira"):
                keys.add(key)
    return keys


def _tool_name_from_context(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "")
    if _TOOL_DELIM in full:
        return full.split(_TOOL_DELIM, 1)[1]
    return full


def handler(event, context):
    """Gateway Lambda-target entry point. Derives the project from the issue-key
    prefix, requires arg-consistency, checks the onboarded-project allowlist,
    then calls Jira REST v3 with the site token."""
    tool = _tool_name_from_context(context)
    args = event if isinstance(event, dict) else {}
    site_id = str(args.get("site", "")).strip()
    origin = str(args.get(ORIGIN_ARG, "")).strip()
    agent = str(args.get(AGENT_ARG, "")).strip() or None

    entry = _TOOLS.get(tool)
    if entry is None:
        logger.warning("jira_broker: unknown tool %r", tool)
        raise BrokerError(f"unknown tool '{tool}'")
    impl, required, klass = entry
    missing = [a for a in required if not args.get(a) and args.get(a) != 0]
    if missing:
        raise BrokerError(f"tool '{tool}' missing required args: {sorted(missing)}")

    site = ac.get_site(site_id)
    if not site or not trigger_grants.atlassian_product_enabled(site_id, "jira"):
        raise BrokerError(f"site '{site_id}' is not an onboarded, jira-enabled Atlassian site")

    # Resolve the project. Prefer the issue-key prefix (server truth); when an
    # explicit project_key is also supplied it MUST match (arg-consistency —
    # §B2.1) so an agent can't claim project A while acting on an issue in B.
    issue_key = str(args.get("issue_key", "")).strip()
    project_key = str(args.get("project_key", "")).strip()
    if issue_key:
        derived = _project_of_issue(issue_key)
        if project_key and project_key != derived:
            raise BrokerError(
                f"project_key '{project_key}' does not match issue {issue_key}'s "
                f"project '{derived}'"
            )
        project_key = derived
    if not project_key:
        # Reads like search_issues/list_projects have no single project — allow
        # them only when the site has at least one onboarded project (they're
        # further bounded server-side: list_projects returns all, search runs the
        # model's JQL). get_project names its own project_key (required arg).
        if tool in ("search_issues", "list_projects"):
            project_key = None
        else:
            raise BrokerError(f"tool '{tool}' requires a project (issue_key or project_key)")

    if project_key is not None:
        if not trigger_grants.container_allowed(site_id, project_key, "jira"):
            raise BrokerError(f"project '{project_key}' is not onboarded for this site")

    start = time.time()
    outcome = "error"
    try:
        token = ac.fetch_token(site)
        if klass == CLASS_WRITE:
            result = impl(site, token, args, agent=agent, origin=origin)
        else:
            result = impl(site, token, args)
        outcome = "error" if isinstance(result, dict) and result.get("isError") else "ok"
        return result
    except ac.AtlassianError as exc:
        outcome = "auth_error"
        logger.error("jira_broker: %s", exc)
        raise BrokerError(str(exc)) from exc
    finally:
        logger.info(
            "jira_broker tool=%s site=%s project=%s agent=%s origin=%s outcome=%s latency_ms=%d",
            tool, site_id, project_key, agent, origin, outcome,
            int((time.time() - start) * 1000),
        )


# --- tool schema --------------------------------------------------------------


def _prop(name: str) -> dict:
    ptype = _PROP_TYPES.get(name, "string")
    if ptype == "array":
        return {"type": "array", "items": {"type": "string"}}
    if ptype == "object":
        return {"type": "object"}
    return {"type": ptype}


def _schema(required: set[str]) -> dict:
    props = {name: _prop(name) for name in sorted(required)}
    return {"type": "object", "properties": props, "required": sorted(required)}


def tool_definitions() -> list[dict]:
    return [
        {
            "name": name,
            "description": f"Jira {name.replace('_', ' ')} (site service-account token).",
            "inputSchema": _schema(required),
        }
        for name, (_impl, required, _klass) in sorted(_TOOLS.items())
    ]


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(tool_definitions(), indent=2))
