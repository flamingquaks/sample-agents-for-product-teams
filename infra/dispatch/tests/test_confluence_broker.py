"""Unit tests for the Confluence broker Lambda target (confluence_broker.py).

Exercises the space allowlist (reads INCLUDED — §C2.4), the page↔space
cross-check, write-mode/write-agents enforcement, base_version optimistic
concurrency, and the Markdown→storage boundary — token fetch + HTTP patched.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import confluence_broker as cb  # noqa: E402
import atlassian_client as ac  # noqa: E402

SITE_ID = "22222222-2222-2222-2222-222222222222"


def _ctx(tool):
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": f"ConfluenceTarget___{tool}"}
    return ctx


def _args(agent="docwriter", origin="acme/web", **extra):
    base = {cb.ORIGIN_ARG: origin, cb.AGENT_ARG: agent, "site": SITE_ID}
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    site = {"site_id": SITE_ID, "site_url": "https://acme.atlassian.net",
            "bot_email": "sdlc@acme.com", "api_token_param": "/p"}
    monkeypatch.setattr(ac, "get_site", lambda sid: site if sid == SITE_ID else None)
    monkeypatch.setattr(ac, "fetch_token", lambda s: "tok")
    monkeypatch.setattr(
        cb.trigger_grants, "atlassian_product_enabled",
        lambda sid, p: sid == SITE_ID and p == "confluence",
    )
    monkeypatch.setattr(
        cb.trigger_grants, "container_allowed",
        lambda sid, key, p: key == "DOCS" and p == "confluence",
    )
    # Default: DOCS is a direct-mode space with no write_agents narrowing.
    monkeypatch.setattr(
        cb.trigger_grants, "atlassian_container_row",
        lambda p, sid, key: {"write_mode": "direct", "write_agents": []},
    )
    return site


def _rest(monkeypatch, responder):
    calls = []

    def fake_rest(site, method, path, token, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return responder(method, path, body)

    monkeypatch.setattr(ac, "rest", fake_rest)
    return calls


def test_read_requires_onboarded_space(monkeypatch):
    _rest(monkeypatch, lambda *a: (200, {}))
    with pytest.raises(cb.BrokerError, match="not onboarded"):
        cb.handler(_args(space_key="SECRET", page_id="1"), _ctx("get_page"))


def test_get_page_returns_markdown(monkeypatch):
    def responder(method, path, body):
        return 200, {"id": "5", "title": "T", "spaceId": "900",
                     "version": {"number": 3},
                     "body": {"storage": {"value": "<p>Hello <strong>world</strong></p>"}}}
    _rest(monkeypatch, responder)
    # page↔space check calls get_space (spaceId 900) — make it match.
    monkeypatch.setattr(cb, "_page_in_space", lambda *a: True)
    out = cb.handler(_args(space_key="DOCS", page_id="5"), _ctx("get_page"))
    assert out["version"] == 3
    assert "**world**" in out["body_markdown"]


def test_propose_space_rejects_page_write(monkeypatch):
    monkeypatch.setattr(
        cb.trigger_grants, "atlassian_container_row",
        lambda p, sid, key: {"write_mode": "propose", "write_agents": []},
    )
    _rest(monkeypatch, lambda *a: (200, {}))
    out = cb.handler(
        _args(space_key="DOCS", title="X", body_markdown="hi"), _ctx("create_page")
    )
    assert out["isError"] and "PROPOSE" in out["message"]


def test_propose_space_allows_comment(monkeypatch):
    monkeypatch.setattr(
        cb.trigger_grants, "atlassian_container_row",
        lambda p, sid, key: {"write_mode": "propose", "write_agents": []},
    )
    monkeypatch.setattr(cb, "_page_in_space", lambda *a: True)
    _rest(monkeypatch, lambda m, p, b: (200, {"id": "77"}))
    out = cb.handler(
        _args(space_key="DOCS", page_id="5", body_markdown="proposed"), _ctx("add_comment")
    )
    assert out["comment_id"] == "77"


def test_write_agents_narrowing(monkeypatch):
    monkeypatch.setattr(
        cb.trigger_grants, "atlassian_container_row",
        lambda p, sid, key: {"write_mode": "direct", "write_agents": ["docwriter"]},
    )
    monkeypatch.setattr(cb, "_page_in_space", lambda *a: True)
    _rest(monkeypatch, lambda *a: (200, {}))
    # adr is not in write_agents → refused with an instructive tool error.
    out = cb.handler(
        _args(agent="adr", space_key="DOCS", page_id="5", body_markdown="x"),
        _ctx("add_comment"),
    )
    assert out["isError"] and "restricts writes" in out["message"]


def test_update_page_stale_base_version_rejected(monkeypatch):
    def responder(method, path, body):
        if method == "GET":
            return 200, {"version": {"number": 9}, "title": "T", "spaceId": "900"}
        return 200, {}
    _rest(monkeypatch, responder)
    monkeypatch.setattr(cb, "_page_in_space", lambda *a: True)
    out = cb.handler(
        _args(space_key="DOCS", page_id="5", base_version=3, body_markdown="new"),
        _ctx("update_page"),
    )
    assert out["isError"] and "changed since you read it" in out["message"]


def test_search_pins_space_server_side(monkeypatch):
    calls = _rest(monkeypatch, lambda m, p, b: (200, {"results": []}))
    cb.handler(_args(space_key="DOCS", cql_text="text ~ 'x'"), _ctx("search"))
    cql = calls[0]["params"]["cql"]
    assert cql.startswith('space = "DOCS"') and "text ~ 'x'" in cql


def test_search_post_filters_cql_injection_breakout(monkeypatch):
    # THE security boundary: even if a crafted cql_text breaks out of the space
    # pin (`…) OR (space = SECRET`) and Confluence returns hits from a NON-
    # onboarded space, the broker drops them by the result's server-truth space
    # key — the injection-proof post-filter. DOCS is onboarded; SECRET is not.
    def responder(method, path, body):
        return 200, {"results": [
            {"content": {"id": "1", "title": "ok", "space": {"key": "DOCS"}}, "url": "/1"},
            {"content": {"id": "9", "title": "leak", "space": {"key": "SECRET"}}, "url": "/9"},
            {"content": {"id": "7", "title": "nospace"}, "url": "/7"},  # missing space → dropped
        ]}
    _rest(monkeypatch, responder)
    out = cb.handler(
        _args(space_key="DOCS", cql_text="x) OR (space = SECRET"), _ctx("search")
    )
    ids = {r["page_id"] for r in out["results"]}
    assert ids == {"1"}  # SECRET hit + space-less hit both dropped


def test_no_destructive_tools_in_schema():
    names = {t["name"] for t in cb.tool_definitions()}
    assert not any(x in n for n in names for x in ("delete", "archive", "move", "restore"))
    assert {"create_page", "update_page", "add_comment", "add_label"} <= names
