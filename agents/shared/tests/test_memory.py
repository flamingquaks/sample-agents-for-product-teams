"""Unit tests for `agents.shared.memory` — the AgentCore Memory tool provider.

The only thing worth pinning here is the REGION, because the library's default is
wrong for this fleet: ``AgentCoreMemoryToolProvider`` resolves ``region or
DEFAULT_REGION`` with ``DEFAULT_REGION = "us-west-2"`` hard-coded, ignoring
``AWS_REGION``. Omitting it silently addresses another region's endpoint and every
memory call fails AccessDenied (the per-role grant names the one Memory in the
stack's region) — observed as the reviewer's review ledger never persisting.

strands_tools isn't installed outside the agent container, so the provider class
is injected as a fake module (same approach as test_bedrock's AnthropicModel).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load(monkeypatch, region, *, tools=("mem_tool",)):
    """(Re)import shared.memory with AWS_REGION set and the provider faked.
    Returns ``(module, provider_class_mock)``."""
    if region is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", region)
    cls = MagicMock(return_value=MagicMock(tools=list(tools)))
    fake_module = MagicMock()
    fake_module.AgentCoreMemoryToolProvider = cls
    monkeypatch.setitem(sys.modules, "strands_tools.agent_core_memory", fake_module)
    sys.modules.pop("shared.memory", None)
    import shared.memory as mod

    return mod, cls


def test_region_comes_from_aws_region(monkeypatch):
    mod, cls = _load(monkeypatch, "eu-west-1")
    tools = mod.memory_tools(
        memory_id="mem-1", actor_id="reviewer", session_id="s1",
        namespace="/agents/reviewer/s1",
    )
    assert tools == ["mem_tool"]
    assert cls.call_args.kwargs["region"] == "eu-west-1"


def test_region_never_falls_through_to_the_library_default(monkeypatch):
    """With AWS_REGION unset the helper still passes an explicit region — the
    library would otherwise use us-west-2, where the fleet Memory doesn't exist,
    and every call would come back AccessDenied on a us-west-2 ARN."""
    mod, cls = _load(monkeypatch, None)
    mod.memory_tools(
        memory_id="mem-1", actor_id="adr", session_id="s1", namespace="/agents/adr/s1",
    )
    region = cls.call_args.kwargs["region"]
    assert region
    assert region != "us-west-2"


def test_identity_args_passed_through(monkeypatch):
    mod, cls = _load(monkeypatch, "us-east-1")
    mod.memory_tools(
        memory_id="mem-9", actor_id="workitems", session_id="sess-7",
        namespace="/agents/workitems/sess-7",
    )
    kwargs = cls.call_args.kwargs
    assert kwargs["memory_id"] == "mem-9"
    assert kwargs["actor_id"] == "workitems"
    assert kwargs["session_id"] == "sess-7"
    assert kwargs["namespace"] == "/agents/workitems/sess-7"
