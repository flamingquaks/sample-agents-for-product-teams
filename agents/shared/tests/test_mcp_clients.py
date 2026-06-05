"""Unit tests for shared.mcp_clients — the MCP-client construction + run loop.

run_with_pm_backend is also exercised through the agents' wiring tests; this
covers the shared helpers directly, including run_single_github_agent (the path
docwriter/adr use) and the run_agent assignment lifecycle.

strands is importable; bedrock_agentcore is not — but mcp_clients doesn't import
it, so we can import the module directly with boto3 mocked.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# agents/ on sys.path so `import shared.mcp_clients` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _FakeMCP:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tools_sync(self):
        return []


@pytest.fixture
def wiring(monkeypatch):
    import shared.mcp_clients as mod

    cap = {"streamable_calls": [], "agent_ctor": None, "completes": [], "fails": []}

    def fake_shc(url, headers=None, **kw):
        cap["streamable_calls"].append({"url": url, "headers": headers or {}})
        return MagicMock()

    def fake_mcp(thunk):
        try:
            thunk()
        except Exception:
            pass
        return _FakeMCP()

    monkeypatch.setattr(mod, "streamablehttp_client", fake_shc)
    monkeypatch.setattr(mod, "MCPClient", fake_mcp)
    monkeypatch.setattr(mod, "get_github_token", lambda: "ghtok")
    monkeypatch.setattr(mod, "get_access_token", lambda: "asanatok")
    agent_ctor = MagicMock(return_value=MagicMock(return_value="result"))
    monkeypatch.setattr(mod, "Agent", agent_ctor)
    cap["agent_ctor"] = agent_ctor
    monkeypatch.setattr(mod, "complete_assignment", lambda aid, **k: cap["completes"].append((aid, k)))
    monkeypatch.setattr(mod, "fail_assignment", lambda aid, **k: cap["fails"].append((aid, k)))
    return mod, cap


_SENTINEL_TOOL = object()


def test_run_single_github_agent_one_client_default_toolset(wiring):
    mod, cap = wiring
    out = mod.run_single_github_agent(
        model="M", system_prompt="SP", custom_tools=[_SENTINEL_TOOL],
        user_input="hi", assignment_id="a1",
    )
    assert str(out) == "result"
    assert len(cap["streamable_calls"]) == 1
    call = cap["streamable_calls"][0]
    assert call["url"] == mod.GITHUB_MCP_URL
    # single-github path must NOT request the projects toolset
    assert "X-MCP-Toolsets" not in call["headers"]
    # custom tools are passed through to the Agent
    assert _SENTINEL_TOOL in cap["agent_ctor"].call_args.kwargs["tools"]
    assert cap["completes"] == [("a1", {"result_summary": "result"})]


def test_github_pm_backend_adds_projects_toolset(wiring):
    mod, cap = wiring
    mod.run_with_pm_backend(
        "github", model="M", system_prompt="SP", custom_tools=[],
        user_input="hi", assignment_id="a1", github_in_asana_mode=True,
    )
    assert len(cap["streamable_calls"]) == 1
    assert cap["streamable_calls"][0]["headers"].get("X-MCP-Toolsets") == "default,projects"


def test_asana_mode_with_github_connects_both_and_dedupes(wiring, monkeypatch):
    mod, cap = wiring

    def tool(name):
        t = MagicMock()
        t.tool_name = name
        return t

    # First MCPClient(...) call is the asana client, second is github (matches
    # the order in run_with_pm_backend). Give them a colliding tool name.
    asana_fake = _FakeMCP()
    asana_fake.list_tools_sync = lambda: [tool("get_me"), tool("asana_only")]
    github_fake = _FakeMCP()
    github_fake.list_tools_sync = lambda: [tool("get_me"), tool("github_only")]
    clients = iter([asana_fake, github_fake])
    monkeypatch.setattr(mod, "MCPClient", lambda thunk: next(clients))

    mod.run_with_pm_backend(
        "asana", model="M", system_prompt="SP", custom_tools=[],
        user_input="hi", assignment_id="a1", github_in_asana_mode=True,
    )
    names = [t.tool_name for t in cap["agent_ctor"].call_args.kwargs["tools"]]
    assert names.count("get_me") == 1            # collision deduped (asana wins)
    assert names.index("asana_only") < names.index("github_only")  # asana first
    assert "github_only" in names


def test_asana_only_when_github_in_asana_mode_false(wiring):
    mod, cap = wiring
    mod.run_with_pm_backend(
        "asana", model="M", system_prompt="SP", custom_tools=[],
        user_input="hi", assignment_id="a1", github_in_asana_mode=False,
    )
    assert len(cap["streamable_calls"]) == 1
    assert cap["streamable_calls"][0]["url"] == mod.ASANA_MCP_URL


def test_run_agent_records_fail_and_reraises(wiring):
    mod, cap = wiring
    cap["agent_ctor"].return_value.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        mod.run_single_github_agent(
            model="M", system_prompt="SP", custom_tools=[],
            user_input="hi", assignment_id="a1",
        )
    assert cap["fails"] == [("a1", {"error": "boom"})]
    assert cap["completes"] == []
