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
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider

logger = logging.getLogger(__name__)

ASK_USER_TOOL_NAME = "ask_user"
INTERRUPT_NAME = "ask_user"

# The pause status this module writes (see docs/specs/durable-repo-work-and-
# resume-spec.md "Thread ↔ assignment binding" for the full lifecycle; the
# other states — resuming/timed_out — are written by the router and sweeper).
STATUS_AWAITING_INPUT = "awaiting_input"


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


# Marker prefix on the cancel message so the after-hook can recognize OUR
# resume-delivery cancel (vs. any other cancelled tool) and rewrite its status.
_ANSWER_PREFIX = "The user replied: "


class AskUserInterruptHook(HookProvider):
    """Raises a Strands interrupt when the model calls ``ask_user`` (D5).

    ``event.interrupt`` raises ``InterruptException`` internally on first call;
    on resume (the interruptResponse invocation) it RETURNS the human's answer.
    Delivery of that answer needs BOTH hooks:

    - BeforeToolCallEvent sets ``cancel_tool`` so the tool body never runs and
      the answer becomes the tool result's text — but strands packages every
      cancel as ``status: "error"``, which the model reads as a FAILED tool
      call (it may re-ask the answered question or fall back to guessing).
    - AfterToolCallEvent therefore rewrites our answer-carrying cancel result
      to ``status: "success"`` (``result`` is one of the event's writable
      fields), so the model sees a normal, successful ``ask_user`` return.
    """

    def register_hooks(self, registry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != ASK_USER_TOOL_NAME:
            return
        question = str((event.tool_use.get("input") or {}).get("question", ""))
        answer = event.interrupt(INTERRUPT_NAME, reason=question)
        # Resumed: surface the human's reply as the tool result (text only —
        # the after-hook below fixes the status).
        event.cancel_tool = f"{_ANSWER_PREFIX}{answer}"

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        if event.tool_use.get("name") != ASK_USER_TOOL_NAME:
            return
        if not (event.cancel_message or "").startswith(_ANSWER_PREFIX):
            return  # a genuine cancel of ask_user, not our answer delivery
        result = dict(event.result or {})
        result["status"] = "success"
        event.result = result


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
    from shared.assignment import _get_table, timeline_event

    _get_table().update_item(
        Key={"assignment_id": assignment_id},
        UpdateExpression=(
            "SET #s = :s, interrupt_id = :iid, pending_question = :q, "
            "workspace_snapshot = :ws, paused_at = :now, "
            # The question turn lands atomically with the pause itself.
            "timeline = list_append(if_not_exists(timeline, :tl_empty), :tl_evt)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": STATUS_AWAITING_INPUT,
            ":iid": interrupt_id,
            ":q": question[:2000],
            ":ws": workspace_snapshot,
            ":now": int(time.time()),
            ":tl_empty": [],
            ":tl_evt": [timeline_event("question", question)],
        },
    )
    logger.info("Assignment %s paused awaiting input (interrupt %s)", assignment_id, interrupt_id)


def mark_resumed(assignment_id: str) -> None:
    """Flip a resuming assignment back to in-flight and clear the pause fields.

    Two load-bearing details:
    - ``resumed_at`` is written so the durable sweeper's stale-dispatch sweep
      ages this run from the RESUME, not the original ``created_at`` — without
      it, a run resumed hours after creation would be swept to ``failed``
      mid-execution.
    - The write is RETRIED (not fire-and-forget): if it never lands, the row
      stays ``resuming`` and the sweeper's stuck-resume revert would re-open
      the pause while this agent is still running — inviting a second,
      concurrent resume of the same interrupt. After retries exhaust we log
      loudly; the run itself proceeds (a duplicate status is recoverable, a
      dead run is not).
    """
    from shared.assignment import _get_table

    for attempt in range(3):
        try:
            _get_table().update_item(
                Key={"assignment_id": assignment_id},
                UpdateExpression=(
                    "SET #s = :s, resumed_at = :now "
                    "REMOVE interrupt_id, pending_question, paused_at"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "dispatched", ":now": int(time.time())},
            )
            return
        except Exception:
            if attempt == 2:
                logger.exception(
                    "could not mark %s resumed after retries — row remains "
                    "'resuming' and the sweeper may re-open the pause",
                    assignment_id,
                )
            else:
                time.sleep(0.5 * (attempt + 1))


def durable_kit(
    assignment_id: str,
    *,
    agent_id: str,
    origin: str,
    repo_capable: bool = True,
    source: str = "",
    source_context: dict | None = None,
):
    """One-call wiring for an agent entrypoint. Returns
    ``(session_manager, extra_tools, hooks, prompt_suffix)``:

    - the S3 session manager (or None when the bucket isn't deployed),
    - the durable-workspace tools (when ``repo_capable`` and the runtime has
      git + the token vendor),
    - the ``ask_user`` tool + its interrupt hook — offered ONLY when a resume
      trigger actually exists (see ``_resumable``),
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
    if session is not None and _resumable(source, source_context):
        tools.append(ask_user)
        hooks.append(AskUserInterruptHook())
        prompt += (
            "\n\nWhen you are blocked on a decision only the requester can "
            "make, use the ask_user tool — do not guess or fail. If it is "
            "unavailable or errors, make your best assumption and state it "
            "clearly in your answer.\n"
        )
    return session, tools, hooks, prompt


def _resumable(source: str, source_context: dict | None) -> bool:
    """Whether a pause on THIS dispatch could actually be resumed.

    The resume trigger is an in-thread ``@sdlc-agents`` reply resolved via the
    ``thread_binding#`` row — which the router only writes when the dispatch
    carries a Slack thread (workspace + channel + thread_ts). A durable
    session alone is not enough: a Slack dispatch WITHOUT a thread (the legacy
    ``/fleet`` slash command sends thread_ts=None) has no binding, so its
    pause would strand as an apparent hang until the timeout sweep — same for
    GitHub/Asana dispatches. The predicate is therefore the presence of the
    binding's ingredients, not the source name alone.
    """
    if source != "slack":
        return False
    ctx = source_context or {}
    return bool(
        ctx.get("workspace") and ctx.get("channel_id") and ctx.get("thread_ts")
    )


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
