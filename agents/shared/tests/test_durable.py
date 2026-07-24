"""Tests for durable conversation + pause/resume (shared/durable.py).

The pause protocol's LOAD-BEARING ordering (push clean BEFORE the status flip,
fail loud when the push can't land) is the core contract here; the interrupt
hook and session gating are covered with fakes — no AWS, no real model loop.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shared.assignment as asg
from shared import durable


class FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)

    def get_item(self, **kwargs):
        return {}


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(asg, "_get_table", lambda: table)
    return table


def _interrupt_result(interrupt_id="int-1", question="Which region?"):
    return SimpleNamespace(
        stop_reason="interrupt",
        interrupts=[SimpleNamespace(id=interrupt_id, name="ask_user", reason=question)],
        metrics=SimpleNamespace(accumulated_usage={"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}),
    )


class FakeWorkspace:
    def __init__(self, snapshot=None, fail=False):
        self.snapshot_out = snapshot or [{"repo": "acme/web", "branch": "wip/a-1", "sha": "abc"}]
        self.fail = fail
        self.pushed = False

    def push_all_clean(self):
        if self.fail:
            raise RuntimeError("push refused")
        self.pushed = True
        return self.snapshot_out


# --- session gating ------------------------------------------------------------


def test_session_manager_none_without_bucket(monkeypatch):
    monkeypatch.delenv("SESSION_BUCKET", raising=False)
    assert durable.session_manager("a-1") is None


def test_session_manager_none_for_sentinel_ids(monkeypatch):
    monkeypatch.setenv("SESSION_BUCKET", "bkt")
    assert durable.session_manager("") is None
    assert durable.session_manager("default") is None


def test_session_manager_builds_with_bucket(monkeypatch):
    monkeypatch.setenv("SESSION_BUCKET", "bkt")
    captured = {}

    class FakeSM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import strands.session.s3_session_manager as sm_mod

    monkeypatch.setattr(sm_mod, "S3SessionManager", FakeSM)
    mgr = durable.session_manager("a-1")
    assert isinstance(mgr, FakeSM)
    assert captured["session_id"] == "a-1"
    assert captured["bucket"] == "bkt"


# --- pending_ask / resume payload -----------------------------------------------


def test_pending_ask_matches_named_interrupt():
    result = _interrupt_result()
    interrupt = durable.pending_ask(result)
    assert interrupt is not None and interrupt.id == "int-1"


def test_pending_ask_none_for_normal_stop():
    assert durable.pending_ask(SimpleNamespace(stop_reason="end_turn", interrupts=[])) is None


def test_pending_ask_ignores_foreign_interrupts():
    result = SimpleNamespace(
        stop_reason="interrupt",
        interrupts=[SimpleNamespace(id="x", name="other_hook", reason="?")],
    )
    assert durable.pending_ask(result) is None


def test_resume_payload_shape():
    assert durable.resume_payload("int-9", "us-east-1") == [
        {"interruptResponse": {"interruptId": "int-9", "response": "us-east-1"}}
    ]


# --- pause protocol ------------------------------------------------------------


def test_handle_result_pauses_with_snapshot(fake_table):
    workspace = FakeWorkspace()
    pause = durable.handle_agent_result(_interrupt_result(), "a-1", workspace=workspace)
    assert workspace.pushed
    assert pause["interrupt_id"] == "int-1"
    assert pause["question"] == "Which region?"
    # The row write carries snapshot + interrupt + awaiting_input in ONE update.
    flip = fake_table.updates[0]
    vals = flip["ExpressionAttributeValues"]
    assert vals[":s"] == "awaiting_input"
    assert vals[":iid"] == "int-1"
    assert vals[":ws"] == workspace.snapshot_out
    assert vals[":q"] == "Which region?"


def test_pause_push_failure_raises_before_status_flip(fake_table):
    """D7: if the clean push can't land, NOTHING is written — the caller fails
    the assignment loud instead of pausing with unpushed work."""
    workspace = FakeWorkspace(fail=True)
    with pytest.raises(RuntimeError, match="push refused"):
        durable.handle_agent_result(_interrupt_result(), "a-1", workspace=workspace)
    assert fake_table.updates == []


def test_pause_records_segment_usage(fake_table):
    durable.handle_agent_result(_interrupt_result(), "a-1", workspace=FakeWorkspace())
    add_updates = [u for u in fake_table.updates if u["UpdateExpression"].startswith("ADD ")]
    assert add_updates, "pause must accumulate the segment's token usage"
    assert add_updates[0]["ExpressionAttributeValues"][":token_usage"] == 15


def test_handle_result_none_when_no_interrupt(fake_table):
    out = durable.handle_agent_result(
        SimpleNamespace(stop_reason="end_turn", interrupts=[]), "a-1",
        workspace=FakeWorkspace(),
    )
    assert out is None
    assert fake_table.updates == []


def test_mark_resumed_clears_pause_fields(fake_table):
    durable.mark_resumed("a-1")
    upd = fake_table.updates[0]
    assert "REMOVE interrupt_id" in upd["UpdateExpression"]
    assert upd["ExpressionAttributeValues"][":s"] == "dispatched"


# --- durable_kit ----------------------------------------------------------------


def test_durable_kit_no_bucket_no_vendor(monkeypatch):
    monkeypatch.delenv("SESSION_BUCKET", raising=False)
    monkeypatch.delenv("WORKSPACE_TOKEN_FUNCTION", raising=False)
    session, tools, hooks, prompt = durable.durable_kit(
        "a-1", agent_id="docwriter", origin="acme/web"
    )
    assert session is None
    assert tools == []
    assert hooks == []
    assert prompt == ""


def test_durable_kit_full(monkeypatch):
    monkeypatch.setenv("SESSION_BUCKET", "bkt")
    monkeypatch.setenv("WORKSPACE_TOKEN_FUNCTION", "fn")

    class FakeSM:
        def __init__(self, **kwargs):
            pass

    import strands.session.s3_session_manager as sm_mod

    monkeypatch.setattr(sm_mod, "S3SessionManager", FakeSM)
    import shared.tools.workspace as ws

    monkeypatch.setattr(ws, "enabled", lambda: True)
    session, tools, hooks, prompt = durable.durable_kit(
        "a-1", agent_id="docwriter", origin="acme/web", source="slack"
    )
    assert session is not None
    tool_names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert "clone_repo" in tool_names and "ask_user" in tool_names
    assert len(hooks) == 1
    assert "Durable workspace" in prompt and "ask_user" in prompt
    # And the workspace got bound to this dispatch.
    assert ws._state.assignment_id == "a-1"
    assert ws._state.agent_id == "docwriter"
    assert ws._state.origin == "acme/web"


def test_durable_kit_repo_incapable_agent(monkeypatch):
    monkeypatch.setenv("SESSION_BUCKET", "bkt")
    monkeypatch.setenv("WORKSPACE_TOKEN_FUNCTION", "fn")

    class FakeSM:
        def __init__(self, **kwargs):
            pass

    import strands.session.s3_session_manager as sm_mod

    monkeypatch.setattr(sm_mod, "S3SessionManager", FakeSM)
    import shared.tools.workspace as ws

    monkeypatch.setattr(ws, "enabled", lambda: True)
    _session, tools, _hooks, _prompt = durable.durable_kit(
        "a-1", agent_id="workitems", origin="", repo_capable=False, source="slack"
    )
    tool_names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert "clone_repo" not in tool_names
    assert "ask_user" in tool_names


def test_durable_kit_no_ask_user_without_resume_trigger(monkeypatch):
    """A GitHub/Asana dispatch has no in-thread resume trigger — offering
    ask_user there would strand the run in awaiting_input. Session + workspace
    still wire; only the pause tool is withheld."""
    monkeypatch.setenv("SESSION_BUCKET", "bkt")
    monkeypatch.setenv("WORKSPACE_TOKEN_FUNCTION", "fn")

    class FakeSM:
        def __init__(self, **kwargs):
            pass

    import strands.session.s3_session_manager as sm_mod

    monkeypatch.setattr(sm_mod, "S3SessionManager", FakeSM)
    import shared.tools.workspace as ws

    monkeypatch.setattr(ws, "enabled", lambda: True)
    session, tools, hooks, prompt = durable.durable_kit(
        "a-1", agent_id="docwriter", origin="acme/web", source="github"
    )
    assert session is not None  # conversation durability still on (crash safety)
    tool_names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert "clone_repo" in tool_names
    assert "ask_user" not in tool_names
    assert hooks == []


# --- interrupt hook --------------------------------------------------------------


def test_hook_raises_interrupt_for_ask_user():
    from strands.interrupt import InterruptException

    hook = durable.AskUserInterruptHook()

    class FakeEvent:
        tool_use = {"name": "ask_user", "input": {"question": "Deploy to prod?"}}
        cancel_tool = None

        def interrupt(self, name, reason=None, response=None):
            raise InterruptException(
                SimpleNamespace(id="i-1", name=name, reason=reason, response=None)
            )

    with pytest.raises(InterruptException):
        hook._on_tool_call(FakeEvent())


def test_hook_passes_through_other_tools():
    hook = durable.AskUserInterruptHook()

    class FakeEvent:
        tool_use = {"name": "get_issue", "input": {}}
        cancel_tool = None

        def interrupt(self, *a, **k):
            raise AssertionError("must not interrupt other tools")

    hook._on_tool_call(FakeEvent())


def test_hook_returns_answer_on_resume():
    hook = durable.AskUserInterruptHook()

    class FakeEvent:
        tool_use = {"name": "ask_user", "input": {"question": "Deploy?"}}
        cancel_tool = None

        def interrupt(self, name, reason=None, response=None):
            return "yes, go ahead"

    event = FakeEvent()
    hook._on_tool_call(event)
    assert event.cancel_tool == "The user replied: yes, go ahead"
