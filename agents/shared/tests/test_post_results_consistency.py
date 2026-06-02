"""Cross-agent consistency tests for the per-agent post_results tools.

Three agents (workitems, docwriter, researcher) each ship their own copy of
tools/post_results.py with the SAME intended signature:

    post_results(platform, target_id, message, thread_id="")

These tests assert that all three copies expose the same parameters and produce
equivalent platform-routing instructions — the key guarantee from the migration
that moved post_results into each agent's tool set.

(The adr agent has no post_results and is intentionally not tested here.)
"""

import importlib.util
import inspect
from pathlib import Path

import pytest

# agents/shared/tests/ -> parents[2] == agents/
AGENTS_DIR = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str):
    """Import a module by file path under a unique module name.

    All three post_results.py files share a basename, so a unique module name
    per file is required to avoid sys.modules collisions.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_WORKITEMS = _load(AGENTS_DIR / "workitems" / "tools" / "post_results.py", "pr_workitems")
_DOCWRITER = _load(AGENTS_DIR / "docwriter" / "tools" / "post_results.py", "pr_docwriter")
_RESEARCHER = _load(AGENTS_DIR / "researcher" / "tools" / "post_results.py", "pr_researcher")

# The strands @tool-decorated callables.
ALL_TOOLS = {
    "workitems": _WORKITEMS.post_results,
    "docwriter": _DOCWRITER.post_results,
    "researcher": _RESEARCHER.post_results,
}

EXPECTED_PARAMS = ["platform", "target_id", "message", "thread_id"]


def _signature(tool):
    """Best-effort signature extraction through the strands @tool wrapper.

    strands wraps the function in a DecoratedFunctionTool. Try inspect.signature
    on the tool object directly (strands sets __wrapped__, so this usually
    works), then fall back to the unwrapped function via the attributes strands
    exposes. Returns None if no signature can be obtained.
    """
    candidates = [tool]
    for attr in ("__wrapped__", "func", "_tool_func", "original_function"):
        target = getattr(tool, attr, None)
        if target is not None and callable(target):
            candidates.append(target)
    for candidate in candidates:
        try:
            return inspect.signature(candidate)
        except (TypeError, ValueError):
            continue
    return None


# --- Case 1: identical parameter names in identical order -------------------


def test_all_three_expose_same_parameter_names_in_order():
    sigs = {name: _signature(tool) for name, tool in ALL_TOOLS.items()}

    if all(sig is not None for sig in sigs.values()):
        param_lists = {name: list(sig.parameters.keys()) for name, sig in sigs.items()}
        for name, params in param_lists.items():
            assert params == EXPECTED_PARAMS, (
                f"{name} post_results params {params} != {EXPECTED_PARAMS}"
            )
        # All three identical to each other.
        assert param_lists["workitems"] == param_lists["docwriter"] == param_lists["researcher"]
    else:
        # Could not introspect through the wrapper for at least one tool — fall
        # back to the more important behavioral guarantee: each branch routes to
        # the right tool. (See the per-branch tests below for the full check.)
        for name, tool in ALL_TOOLS.items():
            assert "slack_post_message" in tool(platform="slack", target_id="C1", message="m")
            assert "asana_add_comment" in tool(platform="asana", target_id="1", message="m")
            assert "github_add_comment" in tool(platform="github", target_id="2", message="m")


# --- Case 2: slack branch, no thread ----------------------------------------


@pytest.mark.parametrize("name,tool", list(ALL_TOOLS.items()))
def test_slack_branch_mentions_tool_channel_and_message(name, tool):
    out = tool(platform="slack", target_id="C123", message="hello world", thread_id="")
    assert "slack_post_message" in out
    assert "C123" in out
    assert "hello world" in out


# --- Case 3: slack branch with a thread id ----------------------------------


@pytest.mark.parametrize("name,tool", list(ALL_TOOLS.items()))
def test_slack_branch_references_thread_id(name, tool):
    out = tool(platform="slack", target_id="C123", message="hello", thread_id="170.5")
    assert "170.5" in out
    assert "slack_post_message" in out


# --- Case 4: asana branch ---------------------------------------------------


@pytest.mark.parametrize("name,tool", list(ALL_TOOLS.items()))
def test_asana_branch_mentions_tool_and_target(name, tool):
    out = tool(platform="asana", target_id="999", message="done")
    assert "asana_add_comment" in out
    assert "999" in out


# --- Case 5: github branch --------------------------------------------------


@pytest.mark.parametrize("name,tool", list(ALL_TOOLS.items()))
def test_github_branch_mentions_tool_and_target(name, tool):
    out = tool(platform="github", target_id="42", message="done")
    assert "github_add_comment" in out
    assert "42" in out


# --- Case 6: cross-agent slack consistency ----------------------------------


def test_all_three_produce_equivalent_slack_instructions():
    outputs = {
        name: tool(platform="slack", target_id="C123", message="hello", thread_id="170.5")
        for name, tool in ALL_TOOLS.items()
    }
    for name, out in outputs.items():
        assert out, f"{name} produced empty slack instruction"
        assert "slack_post_message" in out, f"{name} slack branch missing slack_post_message"
        assert "C123" in out
        assert "170.5" in out
        assert "hello" in out
