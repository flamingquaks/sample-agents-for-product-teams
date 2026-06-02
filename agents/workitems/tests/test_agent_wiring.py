"""Tests for workitems agent.py PM-backend WIRING.

test_pm_backend.py covers project_config/prompts in isolation; this file covers
the part agent.invoke() actually does differently per backend:
  - github mode: ONE GitHub MCP client, created with the X-MCP-Toolsets:
    default,projects header, no Asana client, reconcile_sync NOT loaded.
  - asana mode: BOTH Asana + GitHub clients, reconcile_sync loaded.
  - _run_agent records complete_assignment on success / fail_assignment on error.

agent.py imports bedrock_agentcore and strands_tools (not installed here) at
module level, so we stub those in sys.modules before importing, then replace the
MCP/Agent/token symbols on the imported module with capturing fakes.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

WORKITEMS_DIR = Path(__file__).resolve().parents[1]
AGENTS_DIR = WORKITEMS_DIR.parent
for p in (str(WORKITEMS_DIR), str(AGENTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _install_stubs():
    """Stub the two uninstalled module trees agent.py imports at load time."""
    bedrock = types.ModuleType("bedrock_agentcore")
    runtime = types.ModuleType("bedrock_agentcore.runtime")

    class _App:
        def entrypoint(self, fn):
            return fn

        def run(self):
            pass

    runtime.BedrockAgentCoreApp = _App
    bedrock.runtime = runtime
    sys.modules.setdefault("bedrock_agentcore", bedrock)
    sys.modules.setdefault("bedrock_agentcore.runtime", runtime)

    st = types.ModuleType("strands_tools")
    acm = types.ModuleType("strands_tools.agent_core_memory")

    class _MemProv:
        def __init__(self, **kw):
            self.tools = []

    acm.AgentCoreMemoryToolProvider = _MemProv
    st.agent_core_memory = acm
    sys.modules.setdefault("strands_tools", st)
    sys.modules.setdefault("strands_tools.agent_core_memory", acm)


class FakeMCPClient:
    """Records the (url, headers) the factory thunk passes to streamablehttp_client."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tools_sync(self):
        return []  # no MCP tools — keeps all_tools == the custom tool list


@pytest.fixture
def agent_mod(monkeypatch):
    """Import workitems agent.py fresh with stubs + capturing fakes installed.

    Returns a factory: load(pm_backend) -> (module, captures dict).
    """
    _install_stubs()

    def load(pm_backend):
        monkeypatch.setenv("PM_BACKEND", pm_backend)
        monkeypatch.setenv("GITHUB_REPO", "acme/web")
        if pm_backend == "github":
            monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
        else:
            monkeypatch.setenv("ASANA_PROJECT_GID", "111")
            monkeypatch.setenv("ASANA_WORKSPACE_GID", "222")
        monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)

        # Evict this agent's own modules AND any `tools`/`tools.*` packages a
        # sibling agent's test may have cached — every agent has its own
        # top-level `tools` package, so a stale cache entry would make
        # `from tools.status_report import ...` resolve to the wrong agent.
        for m in list(sys.modules):
            if m in ("agent", "project_config", "prompts") or m == "tools" or m.startswith("tools."):
                sys.modules.pop(m, None)
        # Ensure workitems is first on sys.path so its `tools`/`prompts`/
        # `project_config` win regardless of earlier test import order.
        sys.path.insert(0, str(WORKITEMS_DIR))
        import agent as mod

        captures = {"streamable_calls": [], "tokens": []}

        # streamablehttp_client(url, headers=...) — record every call
        def fake_shc(url, headers=None, **kw):
            captures["streamable_calls"].append({"url": url, "headers": headers or {}})
            return MagicMock()

        # MCPClient(thunk) — invoke the thunk so fake_shc records, return a fake
        def fake_mcp(thunk):
            try:
                thunk()
            except Exception:
                pass
            return FakeMCPClient()

        monkeypatch.setattr(mod, "streamablehttp_client", fake_shc)
        monkeypatch.setattr(mod, "MCPClient", fake_mcp)
        monkeypatch.setattr(mod, "build_model", lambda *a, **k: object())
        monkeypatch.setattr(mod, "get_github_token", lambda: captures["tokens"].append("gh") or "ghtok")
        monkeypatch.setattr(mod, "get_access_token", lambda: captures["tokens"].append("asana") or "asanatok")

        fake_agent = MagicMock(return_value="agent-result")
        agent_ctor = MagicMock(return_value=fake_agent)
        monkeypatch.setattr(mod, "Agent", agent_ctor)
        captures["agent_ctor"] = agent_ctor

        completes, fails = [], []
        monkeypatch.setattr(mod, "complete_assignment", lambda aid, **k: completes.append((aid, k)))
        monkeypatch.setattr(mod, "fail_assignment", lambda aid, **k: fails.append((aid, k)))
        captures["completes"] = completes
        captures["fails"] = fails
        return mod, captures

    return load


def _tool_names(agent_ctor):
    tools = agent_ctor.call_args.kwargs["tools"]
    names = set()
    for t in tools:
        names.add(getattr(t, "tool_name", getattr(t, "__name__", repr(t))))
    return names


# --- github mode -------------------------------------------------------------


def test_github_mode_single_client_with_projects_toolset_header(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "github"})

    # exactly one MCP connection, to the GitHub MCP URL, with the projects toolset
    assert len(cap["streamable_calls"]) == 1
    call = cap["streamable_calls"][0]
    assert call["url"] == mod.GITHUB_MCP_URL
    assert call["headers"].get("X-MCP-Toolsets") == "default,projects"
    # no Asana token fetched in github mode
    assert "asana" not in cap["tokens"]


def test_github_mode_does_not_load_reconcile_sync(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "github"})
    assert "reconcile_sync" not in _tool_names(cap["agent_ctor"])
    # but the always-on tools are present
    names = _tool_names(cap["agent_ctor"])
    assert "slack_post_message" in names
    assert "post_results" in names


# --- asana mode --------------------------------------------------------------


def test_asana_mode_connects_both_clients(agent_mod):
    mod, cap = agent_mod("asana")
    mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "asana"})

    urls = [c["url"] for c in cap["streamable_calls"]]
    assert mod.ASANA_MCP_URL in urls
    assert mod.GITHUB_MCP_URL in urls
    assert len(urls) == 2
    # github client in asana mode must NOT request the projects toolset
    for c in cap["streamable_calls"]:
        assert "X-MCP-Toolsets" not in c["headers"]


def test_asana_mode_loads_reconcile_sync(agent_mod):
    mod, cap = agent_mod("asana")
    mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "asana"})
    assert "reconcile_sync" in _tool_names(cap["agent_ctor"])


# --- _run_agent lifecycle ----------------------------------------------------


def test_success_records_complete_assignment(agent_mod):
    mod, cap = agent_mod("github")
    out = mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "github"})
    assert out == {"result": "agent-result"}
    assert cap["completes"] and cap["completes"][0][0] == "a1"
    assert not cap["fails"]


def test_agent_error_records_fail_assignment_and_reraises(agent_mod):
    mod, cap = agent_mod("github")
    cap["agent_ctor"].return_value.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        mod.invoke({"prompt": "hi", "assignment_id": "a1", "source": "github"})
    assert cap["fails"] and cap["fails"][0][0] == "a1"
    assert not cap["completes"]


# --- project_item dispatch context (regression for the dead-branch bug) ------


def test_project_item_context_reaches_system_prompt(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({
        "prompt": "review the board",
        "assignment_id": "a1",
        "source": "github",
        "source_context": {
            "trigger_type": "project_item",
            "repo": "acme/web",
            "project_number": "7",
            "item_id": "PVTI_abc",
            "issue_number": "42",
            "status_change": "Todo → In Progress",
        },
    })
    system_prompt = cap["agent_ctor"].call_args.kwargs["system_prompt"]
    # The project_item branch must win over the generic github branch:
    assert "Projects V2 board" in system_prompt
    assert "PVTI_abc" in system_prompt
    assert "Todo → In Progress" in system_prompt


def test_project_item_without_issue_number_degrades_gracefully(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({
        "prompt": "review the board",
        "assignment_id": "a1",
        "source": "github",
        "source_context": {
            "trigger_type": "project_item",
            "repo": "",
            "project_number": "7",
            "item_id": "PVTI_abc",
            "issue_number": "",
            "content_node_id": "I_xyz",
            "status_change": "Todo → Done",
        },
    })
    sp = cap["agent_ctor"].call_args.kwargs["system_prompt"]
    assert "Projects V2 board" in sp
    # falls back to content-node guidance + project status update, no "#" issue
    assert "content node" in sp.lower() or "I_xyz" in sp
    assert "project status update" in sp.lower()
