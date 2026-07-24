"""Durable conversation + human-in-the-loop pause/resume (durable-repo-work spec).

Two durability problems, one spine (the assignment):

- **Durable A — conversation**: a Strands ``S3SessionManager`` keyed by
  ``assignment_id`` persists messages + pending interrupts every turn and
  auto-restores them when a new container constructs the agent (D2). Covers
  both the voluntary pause and involuntary stops (idle-stop/crash).
- **Durable B — workspace**: at a voluntary pause every cloned repo is pushed
  clean to its ``wip/<assignment_id>`` branch and verified (D3/D7) — handled by
  ``shared.tools.workspace.push_all_clean``.

Pause protocol (D5): ``ask_user`` is a tool; a ``BeforeToolCallEvent`` hook
intercepts it and raises a Strands interrupt, which stops the event loop with
``stop_reason == "interrupt"``. The caller (the agent entrypoint) then runs the
LOAD-BEARING order: push clean → record workspace_snapshot + interrupt_id +
question → flip status to ``awaiting_input`` (the commit point) → exit. The
status flip comes last so a fast reply can never race a half-pushed branch.

Resume (D6): the router re-invokes the SAME runtime with
``{"resume": {"interrupt_id": ..., "response": <user reply>}}``; the entrypoint
reconstructs the agent with the same session manager (conversation + pending
interrupt restored from S3), restores the workspace from the snapshot, and
feeds the reply as an ``interruptResponse`` payload.
"""

import logging
import os
import time

from strands import tool
from strands.hooks import BeforeToolCallEvent, HookProvider

logger = logging.getLogger(__name__)

ASK_USER_TOOL_NAME = "ask_user"
INTERRUPT_NAME = "ask_user"

# Assignment statuses this module owns (see docs/specs/durable-repo-work-and-
# resume-spec.md "Thread ↔ assignment binding" lifecycle).
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_RESUMING = "resuming"


def session_manager(assignment_id: str):
    """A Strands ``S3SessionManager`` for this assignment, or None when the
    session bucket isn't deployed / there is no real assignment (a run without
    one — local dev, schedules — just runs non-durable, exactly as before).

    ``session_id`` must not contain path separators; assignment ids are UUIDs.
    """
    bucket = os.environ.get("SESSION_BUCKET", "")
    if not bucket or not assignment_id or assignment_id == "default":
        return None
    from strands.session.s3_session_manager import S3SessionManager

    return S3SessionManager(
        session_id=assignment_id,
        bucket=bucket,
        prefix=os.environ.get("SESSION_PREFIX", "sessions"),
    )


@tool
def ask_user(question: str) -> str:
    """Ask the human who dispatched this work a question and wait for their
    answer. Use this when you are blocked on a decision only they can make —
    e.g. an ambiguous requirement, a choice between approaches, or a missing
    credential/value. Your work so far is checkpointed; the conversation
    resumes when they reply.

    Args:
        question: The question to ask. Be specific and give the context they
            need to answer in one reply.

    Returns:
        The human's answer.
    """
    # Never reached in normal operation: the interrupt hook stops the loop at
    # BeforeToolCallEvent. Reached only if the hook wasn't registered.
    return (
        "ERROR: pause/resume is not available in this runtime — make your best "
        "assumption, state it clearly in your final answer, and continue."
    )


class AskUserInterruptHook(HookProvider):
    """Raises a Strands interrupt when the model calls ``ask_user`` (D5).

    ``event.interrupt`` raises ``InterruptException`` internally on first call;
    on resume (the interruptResponse invocation) it RETURNS the human's answer,
    which we hand to the model as the tool's result via ``cancel_tool`` — the
    tool body itself never runs.
    """

    def register_hooks(self, registry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_tool_call)

    def _on_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != ASK_USER_TOOL_NAME:
            return
        question = str((event.tool_use.get("input") or {}).get("question", ""))
        answer = event.interrupt(INTERRUPT_NAME, reason=question)
        # Resumed: surface the human's reply as the tool result. cancel_tool
        # with a string skips execution and returns the string to the model.
        event.cancel_tool = f"The user replied: {answer}"


def pending_ask(result):
    """The pending ask_user interrupt on an agent result, or None.

    ``result.stop_reason == "interrupt"`` with our named interrupt means the
    model asked a question and the loop stopped for a human (D5)."""
    if getattr(result, "stop_reason", None) != "interrupt":
        return None
    for interrupt in getattr(result, "interrupts", None) or []:
        if getattr(interrupt, "name", "") == INTERRUPT_NAME:
            return interrupt
    return None


def resume_payload(interrupt_id: str, response: str) -> list[dict]:
    """The agent-invocation payload that resumes a paused run (Strands
    interruptResponse contract)."""
    return [
        {"interruptResponse": {"interruptId": interrupt_id, "response": response}}
    ]


def record_pause(
    assignment_id: str,
    *,
    interrupt_id: str,
    question: str,
    workspace_snapshot: list[dict],
) -> None:
    """Pause protocol steps 2–4 (AFTER the clean push): record the snapshot +
    interrupt on the row, then flip status → ``awaiting_input`` — one write, so
    the commit point and the resume data land atomically. Raises on failure
    (the caller must then fail loud, not exit silently paused)."""
    from shared.assignment import _get_table

    _get_table().update_item(
        Key={"assignment_id": assignment_id},
        UpdateExpression=(
            "SET #s = :s, interrupt_id = :iid, pending_question = :q, "
            "workspace_snapshot = :ws, paused_at = :now"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": STATUS_AWAITING_INPUT,
            ":iid": interrupt_id,
            ":q": question[:2000],
            ":ws": workspace_snapshot,
            ":now": int(time.time()),
        },
    )
    logger.info("Assignment %s paused awaiting input (interrupt %s)", assignment_id, interrupt_id)


def mark_resumed(assignment_id: str) -> None:
    """Flip a resuming assignment back to in-flight and clear the pause fields.
    Best-effort — the resume itself already holds the ``resuming`` lock."""
    from shared.assignment import _get_table

    try:
        _get_table().update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression=(
                "SET #s = :s REMOVE interrupt_id, pending_question, paused_at"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "dispatched"},
        )
    except Exception:
        logger.exception("could not mark %s resumed", assignment_id)


def durable_kit(
    assignment_id: str,
    *,
    agent_id: str,
    origin: str,
    repo_capable: bool = True,
    source: str = "",
):
    """One-call wiring for an agent entrypoint. Returns
    ``(session_manager, extra_tools, hooks, prompt_suffix)``:

    - the S3 session manager (or None when the bucket isn't deployed),
    - the durable-workspace tools (when ``repo_capable`` and the runtime has
      git + the token vendor),
    - the ``ask_user`` tool + its interrupt hook — offered ONLY when a resume
      trigger actually exists: the session must be durable (a pause without
      conversation durability would lose the run) AND the dispatch must be
      Slack-originated (the in-thread reply is the only resume trigger today,
      D6 — a GitHub/Asana run that paused would be stranded until the
      timeout sweep, which reads as a hang to the requester),
    - the system-prompt addendum describing what was wired.

    Also binds the workspace to this dispatch (assignment/agent/origin) so the
    clone/push credentials scope from server truth.
    """
    from shared.tools import workspace

    workspace.configure(
        assignment_id=assignment_id, agent_id=agent_id, origin=origin
    )
    tools: list = []
    hooks: list = []
    prompt = ""
    if repo_capable and workspace.enabled():
        tools.extend(workspace.MODEL_TOOLS)
        prompt += (
            "\n\n## Durable workspace\n\n"
            "For multi-file repo work, clone_repo gives you a real working "
            "tree; run builds/tests with workspace_run. Commit and push at "
            "every milestone with commit_and_push — pushed work survives "
            "anything; unpushed work is lost if this session ends. For a "
            "single file read, keep using the get_file_contents tool.\n"
        )
    session = session_manager(assignment_id)
    if session is not None and source == "slack":
        tools.append(ask_user)
        hooks.append(AskUserInterruptHook())
        prompt += (
            "\n\nWhen you are blocked on a decision only the requester can "
            "make, use the ask_user tool — do not guess or fail. If it is "
            "unavailable or errors, make your best assumption and state it "
            "clearly in your answer.\n"
        )
    return session, tools, hooks, prompt


def handle_agent_result(result, assignment_id: str, *, workspace=None):
    """Post-run bridge for an agent entrypoint: if the run stopped on an
    ``ask_user`` interrupt, execute the pause protocol and return the pause
    descriptor; otherwise return None (the caller completes normally).

    Load-bearing order (spec, "The ask_user pause protocol"):
      1. push every cloned repo clean (D7 — raises if a push can't land),
      2+3+4. record snapshot + interrupt_id + question and flip
             ``awaiting_input`` in one conditional-free write.

    Raises WorkspaceError / ClientError upward on failure — the caller's
    except-path fails the assignment loud (never exit-and-lose).
    """
    interrupt = pending_ask(result)
    if interrupt is None:
        return None
    if workspace is None:
        from shared.tools import workspace as workspace_mod

        workspace = workspace_mod
    snapshot = workspace.push_all_clean()
    question = str(getattr(interrupt, "reason", "") or "")
    record_pause(
        assignment_id,
        interrupt_id=interrupt.id,
        question=question,
        workspace_snapshot=snapshot,
    )
    # This segment's tokens must land now — the resumed segment's metrics
    # start from zero (EventLoopMetrics resets per invocation).
    from shared.assignment import extract_usage, record_usage

    record_usage(assignment_id, extract_usage(result))
    return {"interrupt_id": interrupt.id, "question": question, "snapshot": snapshot}
