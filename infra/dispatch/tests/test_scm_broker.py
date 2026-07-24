"""Unit tests for the SCM broker Lambda target (scm_broker.py).

The broker is the gateway's GitHub tool target: it dispatches on the gateway's
``bedrockAgentCoreToolName`` context value, mints a per-owner GitHub App token,
and calls GitHub's REST API. Here the token minter (github_app) and the GitHub
HTTP layer (requests) are patched, so these exercise routing, arg handling,
credential minting, and the curated/destructive tool boundary without AWS or
network. A separate test asserts the template's inline tool schema matches the
code's ``tool_definitions()`` so the two can't drift.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scm_broker  # noqa: E402


def _ctx(tool_name: str):
    """A Lambda context carrying the gateway's full ``<Target>___<tool>`` name in
    client_context.custom, the way an AgentCore gateway invokes a Lambda target."""
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": tool_name}
    return ctx


@pytest.fixture(autouse=True)
def _mint(monkeypatch):
    # Stub the scoped token minter (no AWS/JWT) and record the permissions it was
    # asked for, so tests can assert least-privilege scoping. Also treat every
    # repo as cross-repo eligible unless a test overrides it.
    minted = {}

    def fake_scoped(repo, *, permissions):
        minted["repo"] = repo
        minted["permissions"] = permissions
        return f"ghs_for_{repo}"

    monkeypatch.setattr(scm_broker.github_app, "scoped_installation_token", fake_scoped)
    monkeypatch.setattr(
        scm_broker.fleet_config, "is_repo_cross_repo_eligible", lambda repo: True
    )
    # Default: the target repo is co-reachable from the origin (grouping passes).
    monkeypatch.setattr(
        scm_broker.fleet_config,
        "coreachable_repos",
        lambda origin: [origin, "acme/web", "acme/api"],
    )
    scm_broker._last_minted = minted  # test-visible handle
    return minted


def _args(agent="docwriter", origin="acme/web", **extra):
    """Tool arguments as the interceptor hands them to the broker — including the
    injected server-truth origin + agent. Default agent=docwriter (full tier) so
    permission assertions see the per-tool level unless a test picks another."""
    base = {scm_broker.ORIGIN_ARG: origin, scm_broker.AGENT_ARG: agent}
    base.update(extra)
    return base


def _capture_requests(status=200, payload=None, text=None):
    """Patch scm_broker.requests.request, capturing the call and returning a
    canned response. ``payload`` feeds resp.json(); ``text`` feeds resp.text (for
    raw/diff endpoints)."""
    calls = []

    def fake(method, url, headers=None, params=None, json=None, timeout=None):
        calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "json": json}
        )
        resp = MagicMock()
        resp.status_code = status
        resp.content = b"{}"
        resp.json.return_value = payload if payload is not None else {"ok": True}
        resp.text = text if text is not None else "diff --git a/x b/x"
        return resp

    return patch.object(scm_broker.requests, "request", side_effect=fake), calls


def test_tool_name_stripped_from_target_prefix():
    assert scm_broker._tool_name_from_context(_ctx("GitHubTarget___get_issue")) == (
        "get_issue"
    )
    # No prefix → returned as-is (defensive).
    assert scm_broker._tool_name_from_context(_ctx("get_issue")) == "get_issue"


def test_add_issue_comment_hits_expected_endpoint_and_token():
    p, calls = _capture_requests(payload={"id": 1})
    with p:
        out = scm_broker.handler(
            _args(owner="acme", repo="web", issue_number=42, body="hi"),
            _ctx("GitHubTarget___add_issue_comment"),
        )
    assert out == {"id": 1}
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://api.github.com/repos/acme/web/issues/42/comments"
    )
    assert call["json"] == {"body": "hi"}
    # per-owner App token, scoped to the target repo
    assert call["headers"]["Authorization"] == "Bearer ghs_for_acme/web"
    # least-privilege: an issue comment needs only issues:write, NOT contents.
    minted = scm_broker._last_minted
    assert minted["repo"] == "acme/web"
    assert minted["permissions"] == {"issues": "write"}


def test_read_tool_gets_read_only_permission():
    p, calls = _capture_requests(payload={"content": ""})
    with p:
        scm_broker.handler(
            _args(owner="acme", repo="web", path="README.md"),
            _ctx("GitHubTarget___get_file_contents"),
        )
    # a file read mints contents:read — never write.
    assert scm_broker._last_minted["permissions"] == {"contents": "read"}


def test_write_file_gets_contents_write_permission():
    p, calls = _capture_requests(payload={"commit": {}})
    with p:
        scm_broker.handler(
            _args(owner="acme", repo="web", path="x.md", content="y", message="m"),
            _ctx("GitHubTarget___create_or_update_file"),
        )
    # docwriter's tier has contents:write, and the tool needs write → write.
    assert scm_broker._last_minted["permissions"] == {"contents": "write"}


def test_agent_tier_caps_below_tool_need():
    # adr's tier is contents:READ. A code-WRITE tool for adr must be capped to
    # read by the intersection (agent tier ∩ tool need = lower level). Even though
    # Cedar wouldn't grant adr create_or_update_file, the credential itself is the
    # backstop: the token can't write code for adr.
    p, calls = _capture_requests(payload={"commit": {}})
    with p:
        scm_broker.handler(
            _args(agent="adr", owner="acme", repo="web", path="x.md", content="y", message="m"),
            _ctx("GitHubTarget___create_or_update_file"),
        )
    assert scm_broker._last_minted["permissions"] == {"contents": "read"}


def test_rejects_repo_not_cross_repo_eligible(monkeypatch):
    # A tool call against a repo that isn't onboarded + eligible is refused before
    # any token is minted or GitHub is called.
    monkeypatch.setattr(
        scm_broker.fleet_config, "is_repo_cross_repo_eligible", lambda repo: False
    )
    p, calls = _capture_requests()
    with p:
        with pytest.raises(scm_broker.BrokerError):
            scm_broker.handler(
                _args(owner="acme", repo="web", issue_number=1, body="x"),
                _ctx("GitHubTarget___add_issue_comment"),
            )
    assert calls == []  # never reached GitHub


def test_rejects_repo_not_co_reachable_from_origin(monkeypatch):
    # Defense in depth behind the interceptor: even if a call for a repo NOT in
    # the origin's co-reachable set reaches the broker, the broker refuses it.
    monkeypatch.setattr(
        scm_broker.fleet_config, "coreachable_repos", lambda origin: [origin]
    )
    p, calls = _capture_requests()
    with p:
        with pytest.raises(scm_broker.BrokerError):
            scm_broker.handler(
                _args(origin="acme/web", owner="other", repo="secret", issue_number=1, body="x"),
                _ctx("GitHubTarget___add_issue_comment"),
            )
    assert calls == []


def test_rejects_when_no_trusted_origin():
    # A call with no injected origin (interceptor absent/misconfigured) is refused.
    p, calls = _capture_requests()
    with p:
        with pytest.raises(scm_broker.BrokerError):
            scm_broker.handler(
                {"owner": "acme", "repo": "web", "issue_number": 1, "body": "x"},
                _ctx("GitHubTarget___add_issue_comment"),
            )
    assert calls == []


def test_create_or_update_file_base64_encodes_content():
    p, calls = _capture_requests(payload={"commit": {}})
    with p:
        scm_broker.handler(
            _args(
                owner="acme",
                repo="web",
                path="docs/x.md",
                content="hello",
                message="add",
                branch="main",
            ),
            _ctx("GitHubTarget___create_or_update_file"),
        )
    body = calls[0]["json"]
    import base64

    assert base64.b64decode(body["content"]).decode() == "hello"
    assert body["branch"] == "main"
    assert calls[0]["method"] == "PUT"


def test_get_pull_request_diff_requests_diff_media_type():
    # The PR diff is fetched under the diff Accept header and wrapped as JSON so
    # the tool result stays an object. adr reads this to review hunks (Modes 2/3).
    p, calls = _capture_requests()
    with p:
        out = scm_broker.handler(
            _args(agent="adr", owner="acme", repo="web", pull_number=7),
            _ctx("GitHubTarget___get_pull_request_diff"),
        )
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.github.com/repos/acme/web/pulls/7"
    assert call["headers"]["Accept"] == scm_broker._DIFF_ACCEPT
    assert out["pull_number"] == 7 and "diff" in out
    # a diff read is pull_requests:read — never write.
    assert scm_broker._last_minted["permissions"] == {"pull_requests": "read"}


def test_get_pull_request_diff_surfaces_http_error():
    # A non-2xx diff fetch (deleted PR → 404, oversized diff → 406) must be
    # returned as an explicit error, NOT wrapped as if the error body were the
    # diff (else adr would "review" the error string).
    p, calls = _capture_requests(status=404, payload={"message": "Not Found"})
    with p:
        out = scm_broker.handler(
            _args(agent="adr", owner="acme", repo="web", pull_number=7),
            _ctx("GitHubTarget___get_pull_request_diff"),
        )
    assert out.get("isError") is True
    assert "diff" not in out


def test_list_pull_request_files_hits_files_endpoint():
    p, calls = _capture_requests(payload=[{"filename": "x.py", "patch": "@@"}])
    with p:
        scm_broker.handler(
            _args(agent="adr", owner="acme", repo="web", pull_number=7),
            _ctx("GitHubTarget___list_pull_request_files"),
        )
    assert calls[0]["url"] == "https://api.github.com/repos/acme/web/pulls/7/files"
    assert scm_broker._last_minted["permissions"] == {"pull_requests": "read"}


def test_adr_can_post_anchored_review_and_event_forced_to_comment():
    # adr's Mode 2/3 output: a PR review carrying a summary body + per-hunk
    # comments. The broker FORCES event=COMMENT so adr can never approve, request
    # changes, or merge — even if the arguments ask for APPROVE.
    p, calls = _capture_requests(payload={"id": 99, "state": "COMMENTED"})
    with p:
        scm_broker.handler(
            _args(
                agent="adr",
                owner="acme",
                repo="web",
                pull_number=7,
                event="APPROVE",  # must be ignored/overridden
                body="Reviewed against ADR-0012.",
                comments=[{"path": "api.py", "line": 10, "body": "Conflicts with ADR-0012."}],
            ),
            _ctx("GitHubTarget___create_pull_request_review"),
        )
    sent = calls[0]["json"]
    assert calls[0]["url"] == "https://api.github.com/repos/acme/web/pulls/7/reviews"
    assert sent["event"] == "COMMENT"  # forced, not APPROVE
    assert sent["body"] == "Reviewed against ADR-0012."
    assert sent["comments"][0]["path"] == "api.py"
    # adr's tier grants pull_requests:write; the review tool needs write → write.
    assert scm_broker._last_minted["permissions"] == {"pull_requests": "write"}


def test_review_defaults_body_when_only_comments_given():
    # GitHub 422s a COMMENT-event review with no top-level body. When the caller
    # supplies only anchored comments and no summary, the broker must send a
    # non-empty body so the review posts (all the anchored findings survive).
    p, calls = _capture_requests(payload={"id": 1})
    with p:
        scm_broker.handler(
            _args(
                agent="adr",
                owner="acme",
                repo="web",
                pull_number=7,
                comments=[{"path": "api.py", "line": 10, "body": "Conflicts with ADR-0012."}],
            ),
            _ctx("GitHubTarget___create_pull_request_review"),
        )
    sent = calls[0]["json"]
    assert sent["event"] == "COMMENT"
    assert sent.get("body")  # non-empty default, not missing/blank
    assert sent["comments"][0]["path"] == "api.py"


def test_search_code_scopes_query_to_repo():
    p, calls = _capture_requests(payload={"items": []})
    with p:
        scm_broker.handler(
            _args(owner="acme", repo="web", query="TODO"),
            _ctx("GitHubTarget___search_code"),
        )
    assert calls[0]["url"] == "https://api.github.com/search/code"
    assert calls[0]["params"]["q"] == "TODO repo:acme/web"


def test_unknown_tool_raises():
    with pytest.raises(scm_broker.BrokerError):
        scm_broker.handler(_args(owner="a", repo="b"), _ctx("GitHubTarget___nope"))


def test_destructive_tools_have_no_broker_surface():
    # delete_file / merge_pull_request / delete_branch must not be dispatchable —
    # the fleet forbids them and the broker gives them no code path at all.
    for tool in ("delete_file", "merge_pull_request", "delete_branch", "delete_repo"):
        assert tool not in scm_broker._TOOLS
        with pytest.raises(scm_broker.BrokerError):
            scm_broker.handler(
                _args(owner="a", repo="b"), _ctx(f"GitHubTarget___{tool}")
            )


def test_missing_required_arg_raises_before_any_github_call():
    p, calls = _capture_requests()
    with p:
        with pytest.raises(scm_broker.BrokerError):
            # add_issue_comment requires issue_number + body
            scm_broker.handler(
                _args(owner="acme", repo="web", issue_number=42),
                _ctx("GitHubTarget___add_issue_comment"),
            )
    assert calls == []  # never called GitHub


def test_credential_mint_failure_raises_broker_error(monkeypatch):
    def boom(repo, *, permissions):
        raise scm_broker.github_app.GitHubAppError("no installation")

    monkeypatch.setattr(scm_broker.github_app, "scoped_installation_token", boom)
    p, _calls = _capture_requests()
    with p:
        with pytest.raises(scm_broker.BrokerError):
            scm_broker.handler(
                _args(owner="acme", repo="web", issue_number=1, body="x"),
                _ctx("GitHubTarget___add_issue_comment"),
            )


def test_every_tool_requires_owner_and_repo():
    # SECURITY INVARIANT: the interceptor only enforces co-repo grouping + injects
    # the server-truth origin/agent when a tool call carries BOTH owner and repo
    # (scm_interceptor gate). A tool registered without both would take the
    # interceptor's passthrough path — bypassing that control. Keep every broker
    # tool owner+repo-scoped so the interceptor always fires. If this fails, do NOT
    # relax it: give the new tool owner+repo args (and re-derive its scoping), or
    # rework the interceptor gate deliberately.
    for tool, (_impl, required) in scm_broker._TOOLS.items():
        assert {"owner", "repo"} <= required, (
            f"tool '{tool}' must require owner+repo (interceptor co-repo "
            f"enforcement gates on both being present); got {sorted(required)}"
        )


def test_target_name_matches_fleet_policy():
    # The target name is the "<TargetName>" half of every Cedar action id, so it
    # must equal fleet_policy.GITHUB_TARGET or the policy wouldn't bind.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))
    import fleet_policy

    assert scm_broker.TARGET_NAME == fleet_policy.GITHUB_TARGET


def test_tool_set_matches_agent_grants_and_writes():
    # Every GitHub tool an agent's Cedar grant references, and every write tool,
    # must be implemented — and no destructive tool may be.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))
    import fleet_policy

    broker = set(scm_broker._TOOLS)
    prefix = fleet_policy.GITHUB_TARGET + "___"
    granted = {
        t[len(prefix):]
        for tools in fleet_policy.AGENT_TOOL_GRANTS.values()
        for t in tools
        if t.startswith(prefix)
    }
    assert granted <= broker, f"agents grant tools the broker lacks: {granted - broker}"
    assert set(fleet_policy.WRITE_TOOLS) <= broker
    assert not (broker & set(fleet_policy.DESTRUCTIVE_TOOLS))


def test_agent_permission_tiers_match_fleet_policy():
    # The per-agent GitHub permission tier is duplicated (broker in infra/dispatch
    # can't import infra/dashboard). fleet_policy is source of truth; they MUST be
    # equal or the broker would mint the wrong access. This guard fails on drift.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))
    import fleet_policy

    assert scm_broker.AGENT_GITHUB_PERMISSIONS == fleet_policy.AGENT_GITHUB_PERMISSIONS


def test_permission_tiers_enforce_access_differentiation():
    perms = scm_broker.AGENT_GITHUB_PERMISSIONS
    # docwriter: code write + PRs
    assert perms["docwriter"]["contents"] == "write"
    assert perms["docwriter"]["pull_requests"] == "write"
    # workitems: issues only — no code, no PR write
    assert "contents" not in perms["workitems"]
    assert perms["workitems"].get("pull_requests") != "write"
    # adr: read code (never writes code), comments, and posts diff-anchored PR
    # review comments. Its pull_requests:write is comment-only — the broker
    # forces create_pull_request_review to event=COMMENT (no approve/merge),
    # asserted by test_create_pull_request_review_forces_comment_event.
    assert perms["adr"]["contents"] == "read"
    assert perms["adr"].get("pull_requests") == "write"
    # researcher: READ-ONLY GitHub — grounds analysis in the real codebase and
    # issue tracker; no write permission of any kind.
    assert perms["researcher"]
    assert all(level == "read" for level in perms["researcher"].values())


def test_template_inline_schema_matches_source():
    # The template embeds the tool schema inline (self-contained CFN); this guard
    # fails if the template's copy drifts from scm_broker.tool_definitions().
    import yaml

    tmpl_path = Path(__file__).resolve().parents[2] / "foundation" / "template.yaml"

    # A SafeLoader subclass that tolerates CloudFormation's !Sub/!GetAtt tags
    # (SafeLoader alone rejects them). Deliberately NOT yaml.load(): everything
    # constructs through SafeLoader's safe constructors only — no arbitrary
    # object instantiation — and driving the loader directly keeps the code
    # free of the yaml.load() call pattern security scanners flag (ACAT
    # UnsafeYAMLLoad). The input is our own template.yaml, but the parse must
    # be safe regardless of source.
    class _L(yaml.SafeLoader):
        pass

    def _multi(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _L.add_multi_constructor("!", _multi)
    loader = _L(tmpl_path.read_text())
    try:
        doc = loader.get_single_data()
    finally:
        loader.dispose()
    target = doc["Resources"]["GitHubGatewayTarget"]["Properties"]
    inline = target["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]

    # Normalize the template's PascalCase CFN keys to the code's lowercase shape.
    def norm_schema(s):
        out = {"type": s["Type"]}
        if "Properties" in s:
            out["properties"] = {k: norm_schema(v) for k, v in s["Properties"].items()}
        if "Items" in s:
            out["items"] = norm_schema(s["Items"])
        if "Required" in s:
            out["required"] = list(s["Required"])
        return out

    from_tmpl = {
        d["Name"]: {"description": d["Description"], "inputSchema": norm_schema(d["InputSchema"])}
        for d in inline
    }
    from_code = {
        d["name"]: {"description": d["description"], "inputSchema": d["inputSchema"]}
        for d in scm_broker.tool_definitions()
    }
    assert from_tmpl == from_code
