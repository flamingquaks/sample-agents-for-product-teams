"""Tests for the researcher agent's selectable PM backend (asana|github).

Covers project_config.build_project_context() + prompts.get_system_prompt() in
isolation, and the agent.py wiring (which MCP client connects, which prompt is
selected) via stubbed imports — mirroring agents/workitems/tests/test_agent_wiring.py.

agent.py imports bedrock_agentcore + strands_tools (not installed here) at module
level, so those are stubbed in sys.modules before import.
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

RESEARCHER_DIR = Path(__file__).resolve().parents[1]
AGENTS_DIR = RESEARCHER_DIR.parent
for p in (str(RESEARCHER_DIR), str(AGENTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fresh(name):
    # Evict this agent's modules, any sibling agent's same-named modules (every
    # agent has its own project_config/prompts/tools), AND the shared.* modules
    # that read PM_BACKEND at their import (so a stale cache doesn't ignore this
    # test's patched PM_BACKEND). Then force the researcher dir to the front of
    # sys.path so the reimport resolves here regardless of collection order.
    for m in list(sys.modules):
        if (m in (name, "agent", "project_config", "prompts",
                  "shared.project_config", "shared.prompts")
                or m == "tools" or m.startswith("tools.")):
            sys.modules.pop(m, None)
    sys.path.insert(0, str(RESEARCHER_DIR))
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("PM_BACKEND", "ASANA_PROJECT_GID", "ASANA_WORKSPACE_GID",
                "ASANA_PROJECT_NAME", "GITHUB_REPO", "GITHUB_PROJECT_NUMBER",
                "GITHUB_PROJECT_OWNER", "AGENTCORE_MEMORY_ID"):
        monkeypatch.delenv(var, raising=False)
    yield


# --- project_config ----------------------------------------------------------


def test_github_backend_builds_context_without_asana(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert "project #7" in ctx and "acme/web" in ctx
    assert "Asana" not in ctx


def test_github_backend_no_import_crash_without_asana(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    pc = _fresh("project_config")  # must not raise
    assert pc.PM_BACKEND == "github"


def test_asana_backend_unchanged(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "asana")
    monkeypatch.setenv("ASANA_PROJECT_GID", "111")
    monkeypatch.setenv("ASANA_WORKSPACE_GID", "222")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert "Project GID: 111" in ctx and "Workspace GID: 222" in ctx


def test_github_missing_project_number_raises(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    pc = _fresh("project_config")
    with pytest.raises(RuntimeError, match="GITHUB_PROJECT_NUMBER"):
        pc.build_project_context()


# --- prompts -----------------------------------------------------------------


_CTX = "<<PROJECT-CTX-SENTINEL>>"


def test_github_prompt_creates_issues_for_stories():
    prompts = _fresh("prompts")
    gh = prompts.get_system_prompt("github", _CTX)
    assert "new GitHub issues" in gh
    assert "add_issue_comment" in gh
    # both placeholders fully substituted (no .format downstream)
    assert "{shared_rules}" not in gh and "{project_context}" not in gh
    assert _CTX in gh
    assert "HARD RULE: Source Citation" in gh  # shared rules present


def test_asana_prompt_unchanged():
    prompts = _fresh("prompts")
    az = prompts.get_system_prompt("asana", _CTX)
    assert "within Asana" in az
    assert "HARD RULE: Source Citation" in az
    assert "{shared_rules}" not in az and "{project_context}" not in az
    assert _CTX in az


# --- agent.py wiring ---------------------------------------------------------


def _install_stubs():
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

    # researcher's web_search tool imports tavily at module level.
    tavily = types.ModuleType("tavily")
    tavily.TavilyClient = MagicMock
    sys.modules.setdefault("tavily", tavily)


class _FakeMCP:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tools_sync(self):
        return []


@pytest.fixture
def agent_mod(monkeypatch):
    _install_stubs()

    def load(pm_backend):
        monkeypatch.setenv("PM_BACKEND", pm_backend)
        if pm_backend == "github":
            monkeypatch.setenv("GITHUB_REPO", "acme/web")
            monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
        else:
            monkeypatch.setenv("ASANA_PROJECT_GID", "111")
            monkeypatch.setenv("ASANA_WORKSPACE_GID", "222")

        # Also evict shared.* (read PM_BACKEND at import) so the patched
        # PM_BACKEND for this case isn't masked by a prior case's cached value.
        for m in ("agent", "project_config", "prompts",
                  "shared.project_config", "shared.prompts"):
            sys.modules.pop(m, None)
        sys.path.insert(0, str(RESEARCHER_DIR))
        import agent as mod

        cap = {"streamable_calls": []}

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
        monkeypatch.setattr(mod, "build_model", lambda *a, **k: object())
        monkeypatch.setattr(mod, "get_github_token", lambda: "ghtok")
        monkeypatch.setattr(mod, "get_access_token", lambda: "asanatok")
        agent_ctor = MagicMock(return_value=MagicMock(return_value="ok"))
        monkeypatch.setattr(mod, "Agent", agent_ctor)
        monkeypatch.setattr(mod, "complete_assignment", lambda *a, **k: None)
        monkeypatch.setattr(mod, "fail_assignment", lambda *a, **k: None)
        cap["agent_ctor"] = agent_ctor
        return mod, cap

    return load


def test_github_mode_single_github_client_with_projects_header(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({"prompt": "research X", "assignment_id": "a1", "source": "github"})
    assert len(cap["streamable_calls"]) == 1
    call = cap["streamable_calls"][0]
    assert call["url"] == mod.GITHUB_MCP_URL
    assert call["headers"].get("X-MCP-Toolsets") == "default,projects"


def test_asana_mode_connects_asana_client(agent_mod):
    mod, cap = agent_mod("asana")
    mod.invoke({"prompt": "research X", "assignment_id": "a1", "source": "asana"})
    assert len(cap["streamable_calls"]) == 1
    assert cap["streamable_calls"][0]["url"] == mod.ASANA_MCP_URL
    assert "X-MCP-Toolsets" not in cap["streamable_calls"][0]["headers"]


def test_github_mode_uses_github_prompt(agent_mod):
    mod, cap = agent_mod("github")
    mod.invoke({"prompt": "research X", "assignment_id": "a1", "source": "github"})
    sp = cap["agent_ctor"].call_args.kwargs["system_prompt"]
    assert "new GitHub issues" in sp
    assert "project #7" in sp


def test_project_item_context_reaches_system_prompt(agent_mod):
    # Regression: researcher's project_item dispatch branch must match before
    # the generic github branch (the dead-branch bug we hit in workitems).
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
            "status_change": "Todo → Needs Research",
        },
    })
    sp = cap["agent_ctor"].call_args.kwargs["system_prompt"]
    assert "Projects V2 board" in sp
    assert "PVTI_abc" in sp
    assert "Todo → Needs Research" in sp


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
    assert "content node" in sp.lower() or "I_xyz" in sp
