"""Unit tests for the SCM co-repo REQUEST interceptor (scm_interceptor.py).

The interceptor runs at the gateway before the broker. It reads the trusted
``x-dispatch-origin`` header, enforces co-repo grouping, and injects server-truth
origin/agent into the tool args. ``fleet_config.coreachable_repos`` is stubbed so
these exercise the interceptor logic without DynamoDB.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scm_interceptor  # noqa: E402


def _event(*, origin=None, agent=None, tool="add_issue_comment", args=None):
    headers = {}
    if origin is not None:
        headers[scm_interceptor.ORIGIN_HEADER] = origin
    if agent is not None:
        headers[scm_interceptor.AGENT_HEADER] = agent
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": f"GitHubTarget___{tool}",
            "arguments": args if args is not None else {"owner": "acme", "repo": "web"},
        },
    }
    return {"mcp": {"gatewayRequest": {"headers": headers, "body": json.dumps(body)}}}


@pytest.fixture(autouse=True)
def _reach(monkeypatch):
    # acme/web is grouped with acme/api + bob/tool (spans owners); nothing else.
    monkeypatch.setattr(
        scm_interceptor.fleet_config,
        "coreachable_repos",
        lambda origin: [origin, "acme/api", "bob/tool"] if origin == "acme/web" else [origin],
    )


def _out_body(result):
    tr = result["mcp"].get("transformedGatewayRequest")
    if tr is not None:
        body = tr["body"]
        return (json.loads(body) if isinstance(body, str) else body), "pass"
    gw_body = result["mcp"]["gatewayResponse"]["body"]
    return (json.loads(gw_body) if isinstance(gw_body, str) else gw_body), "reject"


def test_in_group_call_passes_and_injects_origin_agent():
    res = _event(origin="acme/web", agent="workitems", args={"owner": "acme", "repo": "api", "issue_number": 1, "body": "x"})
    body, kind = _out_body(scm_interceptor.handler(res))
    assert kind == "pass"
    injected = body["params"]["arguments"]
    # server-truth origin + agent injected for the broker
    assert injected[scm_interceptor.ORIGIN_ARG] == "acme/web"
    assert injected[scm_interceptor.AGENT_ARG] == "workitems"


def test_cross_group_call_rejected():
    # secret/repo is NOT co-reachable from acme/web → rejected with an MCP TOOL
    # error (isError result), not a protocol error — a protocol error kills the
    # client session; a tool error lets the model adapt and keep working.
    res = _event(origin="acme/web", agent="docwriter", args={"owner": "secret", "repo": "repo", "issue_number": 1, "body": "x"})
    result = scm_interceptor.handler(res)
    body, kind = _out_body(result)
    assert kind == "reject"
    assert body["result"]["isError"] is True
    assert "not approved to run with" in body["result"]["content"][0]["text"]
    assert body["id"] == 7  # echoes request id
    # gatewayResponse must be the full {statusCode, headers, body} shape — a
    # partial shape is rejected by the gateway and 500s the client.
    gw_resp = result["mcp"]["gatewayResponse"]
    assert gw_resp["statusCode"] == 200
    assert "Content-Type" in gw_resp["headers"]


def test_group_spans_owners():
    # bob/tool is a DIFFERENT owner but shares acme/web's group → allowed.
    res = _event(origin="acme/web", agent="workitems", args={"owner": "bob", "repo": "tool", "issue_number": 1, "body": "x"})
    _body, kind = _out_body(scm_interceptor.handler(res))
    assert kind == "pass"


def test_missing_origin_rejected():
    # No trusted origin header → no cross-repo authority, fail closed.
    res = _event(origin=None, agent="workitems", args={"owner": "acme", "repo": "api", "issue_number": 1, "body": "x"})
    body, kind = _out_body(scm_interceptor.handler(res))
    assert kind == "reject"
    assert "no dispatch origin" in body["result"]["content"][0]["text"]


def test_non_tool_call_passes_through():
    # tools/list, initialize, etc. carry no repo — forwarded with the ORIGINAL
    # body, and the transform carries EXACTLY {headers, body}: extra echoed
    # fields (path/httpMethod/context) are rejected by the gateway with
    # "Received invalid response from interceptor".
    ev = {"mcp": {"gatewayRequest": {"headers": {"a": "b"}, "path": "/mcp", "httpMethod": "POST",
                                     "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})}}}
    result = scm_interceptor.handler(ev)
    body, kind = _out_body(result)
    assert kind == "pass"
    assert body["method"] == "tools/list"
    tr = result["mcp"]["transformedGatewayRequest"]
    assert set(tr) == {"headers", "body"}
    assert tr["headers"] == {"a": "b"}


def test_self_repo_always_reachable():
    # A dispatch acting on its OWN origin repo is always allowed (origin is always
    # in its own co-reachable set).
    res = _event(origin="acme/web", agent="workitems", args={"owner": "acme", "repo": "web", "issue_number": 1, "body": "x"})
    _body, kind = _out_body(scm_interceptor.handler(res))
    assert kind == "pass"


def test_tool_call_without_owner_repo_skips_enforcement_and_injection():
    # Documents the failure mode the broker's owner+repo INVARIANT protects
    # against: a tool call missing owner/repo takes the passthrough path — the
    # interceptor neither enforces co-repo grouping NOR injects the server-truth
    # origin/agent, even though a trusted origin header is present. This is why
    # every broker tool must require owner+repo (test_scm_broker
    # .test_every_tool_requires_owner_and_repo). If a future tool lacked them, a
    # model-supplied _dispatch_origin would survive to the broker here.
    res = _event(origin="acme/web", agent="workitems", args={"query": "TODO"})
    body, kind = _out_body(scm_interceptor.handler(res))
    assert kind == "pass"
    injected = body["params"]["arguments"]
    assert scm_interceptor.ORIGIN_ARG not in injected
    assert scm_interceptor.AGENT_ARG not in injected
