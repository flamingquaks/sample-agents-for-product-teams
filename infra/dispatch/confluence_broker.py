"""Confluence broker — the AgentCore Gateway's Confluence tool target
(atlassian-connector spec §C2).

Same enforced-broker idiom as ``scm_broker`` / ``jira_broker``: the gateway
invokes this Lambda with GATEWAY_IAM_ROLE, Cedar authorizes per-agent tool
grants at the gateway, and the broker adds the enforcement the site-wide
Atlassian token can't express itself:

  1. **Space allowlist on EVERY call including reads** (§C2.4) — unlike GitHub
     (whose per-call App token is already repo-scoped), an Atlassian token is
     site-wide, so a non-onboarded space is invisible to every agent only
     because the broker refuses it. ``search`` is server-side space-pinned and
     ``list_spaces`` is filtered to onboarded+allowed spaces.
  2. **Page↔space cross-check** — a page-level tool's ``page_id`` must actually
     live in the claimed ``space_key`` (an agent can't reach page X in space B
     by naming space A).
  3. **Write-mode enforcement** (§C3.2) — a ``propose`` space rejects page
     writes (the agent proposes via a comment instead); ``write_agents`` narrows
     write tools to listed agents even when others hold Cedar write grants.
  4. **base_version optimistic concurrency** on ``update_page`` (§C2.2) — a
     stale write is rejected with re-read guidance, never clobbering a
     concurrent human edit (T-57).

Bodies cross the boundary as MARKDOWN; the broker owns Markdown↔storage
conversion (``atlassian_client``, macro-free + XML-escaped) so the model never
authors raw storage XHTML (T-54).
"""

import json
import logging
import time

import atlassian_client as ac
import trigger_grants

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TARGET_NAME = "ConfluenceTarget"
_TOOL_DELIM = "___"

# Server-truth args the REQUEST interceptor injects (never model-supplied).
ORIGIN_ARG = "_dispatch_origin"
AGENT_ARG = "_dispatch_agent"

# The signature line every agent-authored comment / version message carries
# (§A6.2) — attribution for audit + rollback.
_SIGNATURE = "🤖 **[{agent}]** · run {origin}"

CLASS_READ = "read"
CLASS_WRITE = "write"


class BrokerError(Exception):
    """A broker dispatch/credential failure (not a REST 4xx)."""


# --- per-tool implementations -------------------------------------------------
# Each takes (site, token, args) and returns the JSON tool result. Absent by
# construction: delete/archive/restore, page moves, attachment writes,
# space/permission/user admin (§C2.2) — no code path exists for them.


def _get_page(site, token, args):
    page_id = args["page_id"]
    status, data = ac.rest(
        site, "GET", f"/wiki/api/v2/pages/{page_id}", token,
        params={"body-format": "storage"},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"could not fetch page {page_id} (HTTP {status})"}
    body = (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
    return {
        "page_id": str(data.get("id", page_id)),
        "title": data.get("title", ""),
        "space_id": str(data.get("spaceId", "")),
        "version": (data.get("version") or {}).get("number"),
        "body_markdown": ac.storage_to_markdown(body),
    }


def _get_page_children(site, token, args):
    page_id = args["page_id"]
    status, data = ac.rest(
        site, "GET", f"/wiki/api/v2/pages/{page_id}/children", token,
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"could not fetch children of {page_id}"}
    return {"children": [
        {"page_id": str(c.get("id", "")), "title": c.get("title", "")}
        for c in (data.get("results") or [])
    ]}


def _get_comments(site, token, args):
    page_id = args["page_id"]
    out = []
    for kind in ("footer-comments", "inline-comments"):
        status, data = ac.rest(
            site, "GET", f"/wiki/api/v2/pages/{page_id}/{kind}", token,
            params={"body-format": "atlas_doc_format"},
        )
        if 200 <= status < 300:
            for c in (data.get("results") or []):
                adf = (((c.get("body") or {}).get("atlas_doc_format") or {}).get("value"))
                try:
                    parsed = json.loads(adf) if adf else None
                except (ValueError, TypeError):
                    parsed = None
                out.append({
                    "comment_id": str(c.get("id", "")),
                    "kind": kind,
                    "text": ac.adf_to_text(parsed) if parsed else "",
                })
    return {"comments": out}


def _search(site, token, args):
    # Space scoping is enforced on the RESULTS, not the query (§C2.4). The
    # space_key is already allowlist-checked in the handler; we AND it into the
    # CQL as a first-pass pin, but CQL operator precedence means a crafted
    # model cql_text could break out of that pin (``…) OR (space = SECRET``) —
    # so the security boundary is the post-filter below: every returned result's
    # space key MUST equal the requested (allowed) space, regardless of the CQL.
    space_key = args["space_key"]
    cql_text = str(args.get("cql_text", "")).strip()
    cql = f'space = "{space_key}"'
    if cql_text:
        cql += f" AND ({cql_text})"
    limit = min(int(args.get("max_results", 25) or 25), 25)
    status, data = ac.rest(
        site, "GET", "/wiki/rest/api/search", token,
        # expand the content's space so we can post-filter on the server-truth key.
        params={"cql": cql, "limit": limit, "expand": "content.space"},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"search failed (HTTP {status})"}
    results = []
    for r in (data.get("results") or []):
        content = r.get("content") or {}
        result_space = ((content.get("space") or {}).get("key")) or ""
        # Drop any hit whose space isn't the requested one — the injection-proof
        # boundary. A result missing space info is dropped (fail closed).
        if result_space != space_key:
            continue
        results.append({
            "title": content.get("title", "") or r.get("title", ""),
            "page_id": str(content.get("id", "")),
            "url": r.get("url", ""),
        })
    return {"results": results}


def _list_spaces(site, token, args):
    # Discovery never leaks names: only onboarded+allowed spaces on this site are
    # returned (§C2.4), regardless of what the REST call could see.
    allowed = _allowed_space_keys(site["site_id"])
    status, data = ac.rest(site, "GET", "/wiki/api/v2/spaces", token, params={"limit": 250})
    spaces = []
    if 200 <= status < 300:
        for s in (data.get("results") or []):
            key = s.get("key", "")
            if key in allowed:
                spaces.append({"space_key": key, "name": s.get("name", ""),
                               "space_id": str(s.get("id", ""))})
    return {"spaces": spaces}


def _get_space(site, token, args):
    space_key = args["space_key"]
    status, data = ac.rest(
        site, "GET", "/wiki/api/v2/spaces", token,
        params={"keys": space_key, "limit": 1},
    )
    if not 200 <= status < 300 or not (data.get("results") or []):
        return {"isError": True, "message": f"space {space_key} not found"}
    s = data["results"][0]
    return {"space_key": s.get("key", ""), "name": s.get("name", ""),
            "space_id": str(s.get("id", "")), "homepage_id": str(s.get("homepageId", ""))}


def _space_id_for_key(site, token, space_key):
    status, data = ac.rest(
        site, "GET", "/wiki/api/v2/spaces", token,
        params={"keys": space_key, "limit": 1},
    )
    if 200 <= status < 300 and (data.get("results") or []):
        return str(data["results"][0].get("id", ""))
    return None


def _create_page(site, token, args, *, agent, origin):
    space_id = _space_id_for_key(site, token, args["space_key"])
    if not space_id:
        return {"isError": True, "message": f"space {args['space_key']} not found"}
    storage = ac.markdown_to_storage(args["body_markdown"])
    body = {
        "spaceId": space_id,
        "status": "current",
        "title": args["title"],
        "body": {"representation": "storage", "value": storage},
    }
    if args.get("parent_id"):
        body["parentId"] = str(args["parent_id"])
    status, data = ac.rest(site, "POST", "/wiki/api/v2/pages", token, body=body)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"create_page failed (HTTP {status}): {data.get('message', '')}"}
    page_id = str(data.get("id", ""))
    # Auto-label agent-managed content so it's one CQL query (§C3.1).
    _add_label_raw(site, token, page_id, "sdlc-agents-managed")
    return {"page_id": page_id, "title": data.get("title", ""),
            "version": (data.get("version") or {}).get("number")}


def _update_page(site, token, args, *, agent, origin):
    page_id = args["page_id"]
    base_version = int(args["base_version"])
    # Optimistic concurrency: re-read the current version and refuse a stale
    # write (T-57) — never clobber a concurrent human edit.
    status, current = ac.rest(
        site, "GET", f"/wiki/api/v2/pages/{page_id}", token,
        params={"body-format": "storage"},
    )
    if not 200 <= status < 300:
        return {"isError": True, "message": f"could not read page {page_id} before update"}
    cur_version = (current.get("version") or {}).get("number")
    if cur_version != base_version:
        return {"isError": True, "message":
                f"page {page_id} changed since you read it (current version "
                f"{cur_version}, you passed base_version {base_version}) — re-read "
                "with get_page and retry against the new version."}
    storage = ac.markdown_to_storage(args["body_markdown"])
    body = {
        "id": page_id,
        "status": "current",
        "title": args.get("title") or current.get("title", ""),
        "body": {"representation": "storage", "value": storage},
        "version": {"number": base_version + 1,
                    "message": _SIGNATURE.format(agent=agent or "agent", origin=origin or "?")},
    }
    status, data = ac.rest(site, "PUT", f"/wiki/api/v2/pages/{page_id}", token, body=body)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"update_page failed (HTTP {status})"}
    return {"page_id": page_id, "version": (data.get("version") or {}).get("number")}


def _add_comment(site, token, args, *, agent, origin):
    page_id = args["page_id"]
    md = args["body_markdown"]
    signed = f"{md}\n\n{_SIGNATURE.format(agent=agent or 'agent', origin=origin or '?')}"
    body = {"pageId": page_id,
            "body": {"representation": "storage", "value": ac.markdown_to_storage(signed)}}
    if args.get("parent_comment_id"):
        body["parentCommentId"] = str(args["parent_comment_id"])
    status, data = ac.rest(site, "POST", "/wiki/api/v2/footer-comments", token, body=body)
    if not 200 <= status < 300:
        return {"isError": True, "message": f"add_comment failed (HTTP {status})"}
    return {"comment_id": str(data.get("id", ""))}


def _add_label_raw(site, token, page_id, label):
    # v1 label endpoint (v2 has no label-add); best-effort.
    ac.rest(site, "POST", f"/wiki/rest/api/content/{page_id}/label", token,
            body=[{"prefix": "global", "name": label}])


def _add_label(site, token, args, *, agent, origin):
    _add_label_raw(site, token, args["page_id"], args["label"])
    return {"page_id": str(args["page_id"]), "label": args["label"]}


# tool → (impl, {required args}, class, is_page_tool)
_TOOLS = {
    "get_page": (_get_page, {"site", "space_key", "page_id"}, CLASS_READ, True),
    "get_page_children": (_get_page_children, {"site", "space_key", "page_id"}, CLASS_READ, True),
    "get_comments": (_get_comments, {"site", "space_key", "page_id"}, CLASS_READ, True),
    "search": (_search, {"site", "space_key", "cql_text"}, CLASS_READ, False),
    "list_spaces": (_list_spaces, {"site"}, CLASS_READ, False),
    "get_space": (_get_space, {"site", "space_key"}, CLASS_READ, False),
    "create_page": (_create_page, {"site", "space_key", "title", "body_markdown"}, CLASS_WRITE, False),
    "update_page": (_update_page, {"site", "space_key", "page_id", "base_version", "body_markdown"}, CLASS_WRITE, True),
    "add_comment": (_add_comment, {"site", "space_key", "page_id", "body_markdown"}, CLASS_WRITE, True),
    "add_label": (_add_label, {"site", "space_key", "page_id", "label"}, CLASS_WRITE, True),
}

_WRITE_IMPLS = {"create_page", "update_page", "add_comment", "add_label"}
# Page-body writers gated by write_mode (propose rejects these; add_comment is
# always allowed as the propose channel, add_label is a tag not a body write).
_PAGE_BODY_WRITES = {"create_page", "update_page"}
_PROP_TYPES = {"base_version": "integer", "max_results": "integer"}


def _allowed_space_keys(site_id: str) -> set[str]:
    """Onboarded + ALLOW-mode space keys for a site (via the shared snapshot)."""
    keys = set()
    from trigger_grants import _snapshot

    for (product, sid, key), row in _snapshot().get("atlassian_containers", {}).items():
        if product == "confluence" and sid == site_id:
            if trigger_grants.container_allowed(site_id, key, "confluence"):
                keys.add(key)
    return keys


def _tool_name_from_context(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "")
    if _TOOL_DELIM in full:
        return full.split(_TOOL_DELIM, 1)[1]
    return full


def handler(event, context):
    """Gateway Lambda-target entry point. Enforces the space allowlist (reads
    included), page↔space cross-check, write-mode/write-agents, and base_version,
    then calls the Atlassian REST API with the site token."""
    tool = _tool_name_from_context(context)
    args = event if isinstance(event, dict) else {}
    site_id = str(args.get("site", "")).strip()
    space_key = str(args.get("space_key", "")).strip()
    origin = str(args.get(ORIGIN_ARG, "")).strip()
    agent = str(args.get(AGENT_ARG, "")).strip() or None

    entry = _TOOLS.get(tool)
    if entry is None:
        logger.warning("confluence_broker: unknown tool %r", tool)
        raise BrokerError(f"unknown tool '{tool}'")
    impl, required, klass, is_page_tool = entry
    missing = [a for a in required if not args.get(a) and args.get(a) != 0]
    if missing:
        raise BrokerError(f"tool '{tool}' missing required args: {sorted(missing)}")

    site = ac.get_site(site_id)
    if not site or not trigger_grants.atlassian_product_enabled(site_id, "confluence"):
        raise BrokerError(f"site '{site_id}' is not an onboarded, confluence-enabled Atlassian site")

    # Space allowlist on EVERY call including reads (§C2.4). list_spaces has no
    # space_key (it self-filters); every other tool must name an allowed space.
    if tool != "list_spaces":
        if not trigger_grants.container_allowed(site_id, space_key, "confluence"):
            raise BrokerError(f"space '{space_key}' is not onboarded for this site")

    start = time.time()
    outcome = "error"
    try:
        token = ac.fetch_token(site)
        # Page↔space cross-check: the page must live in the claimed space.
        if is_page_tool and args.get("page_id"):
            if not _page_in_space(site, token, str(args["page_id"]), space_key):
                raise BrokerError(
                    f"page {args['page_id']} is not in space {space_key} — refusing"
                )
        # Write-mode + write_agents enforcement for writes (§C3.2).
        if tool in _WRITE_IMPLS:
            err = _write_guard(site_id, space_key, tool, agent)
            if err:
                return {"isError": True, "message": err}
            result = impl(site, token, args, agent=agent, origin=origin)
        else:
            result = impl(site, token, args)
        outcome = "error" if isinstance(result, dict) and result.get("isError") else "ok"
        return result
    except ac.AtlassianError as exc:
        outcome = "auth_error"
        logger.error("confluence_broker: %s", exc)
        raise BrokerError(str(exc)) from exc
    finally:
        logger.info(
            "confluence_broker tool=%s site=%s space=%s page=%s agent=%s origin=%s outcome=%s latency_ms=%d",
            tool, site_id, space_key, args.get("page_id", ""), agent, origin, outcome,
            int((time.time() - start) * 1000),
        )


def _page_in_space(site, token, page_id: str, space_key: str) -> bool:
    status, data = ac.rest(site, "GET", f"/wiki/api/v2/pages/{page_id}", token)
    if not 200 <= status < 300:
        return False
    page_space_id = str(data.get("spaceId", ""))
    return page_space_id == _space_id_for_key(site, token, space_key)


def _write_guard(site_id: str, space_key: str, tool: str, agent: str | None) -> str | None:
    """Enforce the space's write_mode + write_agents (§C3.2). Returns an
    instructive error string to surface as a tool result, or None to proceed."""
    row = trigger_grants.atlassian_container_row("confluence", site_id, space_key) or {}
    write_agents = row.get("write_agents") or []
    if write_agents and agent and agent not in write_agents:
        return (f"space '{space_key}' restricts writes to {write_agents}; "
                f"'{agent}' is not permitted to write here.")
    write_mode = row.get("write_mode", "propose")
    if write_mode == "propose" and tool in _PAGE_BODY_WRITES:
        return (f"space '{space_key}' is in PROPOSE mode: direct page writes are "
                "disabled. Post your proposed content with add_comment (rendered "
                "markdown + a link to this run) for a human to apply, or ask an "
                "admin to switch the space to direct mode.")
    return None


# --- tool schema (for the gateway target ToolSchema.InlinePayload) -----------


def _prop(name: str) -> dict:
    return {"type": _PROP_TYPES.get(name, "string")}


def _schema(required: set[str]) -> dict:
    props = {name: _prop(name) for name in sorted(required)}
    return {"type": "object", "properties": props, "required": sorted(required)}


def tool_definitions() -> list[dict]:
    """The gateway target ToolSchema InlinePayload — one ToolDefinition per tool,
    deterministic (sorted) so the template's inline copy can be diffed/verified."""
    return [
        {
            "name": name,
            "description": f"Confluence {name.replace('_', ' ')} (site service-account token).",
            "inputSchema": _schema(required),
        }
        for name, (_impl, required, _klass, _pg) in sorted(_TOOLS.items())
    ]


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(tool_definitions(), indent=2))
