"""Assignment-stream notifier Lambda (spec §18.1/§18.3).

Fleet lifecycle notifications that originate on the **agent** side — a run
completing, failing, or entering an awaiting-approval state — can't be emitted by
the agents themselves (agents hold no Slack credential, by design). Instead this
Lambda subscribes to the **DynamoDB stream** on the assignments table and does
two things per status transition:

1. **Reply to origin** — for a Slack-originated dispatch, post the agent's
   RESULT (``result_summary``) back to the thread/channel the request came from,
   under the agent's own display identity. This is the user-facing answer: the
   requester asked in Slack, so the answer lands where they asked, threaded
   under their message. Without this the requester only ever sees the router's
   "on it" ack — work completes silently into the dashboard.
2. **Fan out** — map the transition to a `notify.notify()` tiered fan-out to
   SUBSCRIBED channels (spec §18.3). This is the ops/monitoring surface,
   distinct from (1): subscriptions are opt-in and get the short status line,
   the origin thread gets the actual result.

Only MODIFY records whose ``status`` actually changed are acted on (a token-usage
update to an already-`completed` row is a no-op). The unit-of-work key is the
``assignment_id`` so a run's start/finish thread together in Slack; the actor is
the run's requester, resolved to the right Slack user per workspace by the
identity map. Best-effort: a delivery failure is logged, never retried into a
poison loop (we return success so the shard iterator advances).
"""

import logging

import notify
import reply
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Slack caps chat.postMessage text at ~4000 chars; leave headroom for the
# status prefix + assignment footer.
_REPLY_MAX_CHARS = 3600

_deser = TypeDeserializer()

# Terminal / notable status → (tier, notify-event id, text template). Only
# statuses an agent (or the router's post-dispatch path) writes are mapped; the
# router already notifies its OWN pre-dispatch events (run_started, guardrail),
# so those aren't re-emitted here.
_STATUS_EVENTS = {
    "completed": (notify.TIER_INFORMATIVE, "run_completed", "✅ @{agent} finished (assignment `{id}`)."),
    "failed": (notify.TIER_ERROR, "run_failed", "❌ @{agent} failed (assignment `{id}`): {summary}"),
    "awaiting_approval": (
        notify.TIER_ACTIONABLE,
        "awaiting_approval",
        "⏳ @{agent} needs your approval on assignment `{id}`.",
    ),
    # Durable pause (durable-repo-work spec D6): the agent asked the requester
    # a question and checkpointed. The origin-thread reply carries the question
    # + the how-to-resume copy; this fan-out line is the ops-channel signal.
    # NOTE: the sweeper's stuck-resume revert (resuming -> awaiting_input) is a
    # status change into this same mapping, so the question is automatically
    # re-posted to the thread — the user learns their reply didn't take.
    "awaiting_input": (
        notify.TIER_ACTIONABLE,
        "awaiting_input",
        "⏸️ @{agent} is paused, waiting for the requester's reply (assignment `{id}`).",
    ),
    # Abandoned pause swept by the durable sweeper (Phase 3): wip branches
    # deleted, work must be re-asked from scratch.
    "timed_out": (
        notify.TIER_INFORMATIVE,
        "run_timed_out",
        "⌛ @{agent} timed out waiting for a reply (assignment `{id}`); the paused work was cleaned up.",
    ),
}


def _plain(image: dict) -> dict:
    """Deserialize a DynamoDB stream image ({attr: {S: ...}}) to plain Python."""
    return {k: _deser.deserialize(v) for k, v in (image or {}).items()}


def _actor_from(item: dict) -> dict:
    """The run's requester as a notify actor. ``requester`` is the namespaced
    principal the router recorded (github:<login> / asana:<gid> / slack:<team>:<uid>);
    split it back into (source, handle, workspace) for identity resolution."""
    requester = str(item.get("requester", "") or "")
    if requester.startswith("slack:"):
        parts = requester.split(":", 2)
        if len(parts) == 3:
            return {"source": "slack", "handle": parts[2], "workspace": parts[1]}
    for src in ("github", "asana"):
        if requester.startswith(f"{src}:"):
            return {"source": src, "handle": requester[len(src) + 1 :], "workspace": ""}
    # Bare login (a pre-namespace record) — assume the run's source.
    return {"source": str(item.get("source", "") or ""), "handle": requester, "workspace": ""}


def _run_ref(assignment_id: str) -> str:
    return notify.run_ref(assignment_id)


def _reply_to_origin(new: dict, new_status: str) -> None:
    """Post the run's RESULT back to the Slack thread/channel that dispatched it.

    This is the final user-facing response: the requester messaged the agent in
    Slack (mention or modal), the router acked "on it", the agent did the work —
    this closes the loop with the actual answer, threaded under the original
    message, under the agent's display identity. Slack-originated runs only
    (GitHub/Asana requesters see results as comments the agent itself posts).
    Best-effort — a failed post never blocks the fan-out below."""
    if new.get("source") != "slack":
        return
    ctx = new.get("source_context") or {}
    team_id = str(ctx.get("workspace", "") or "")
    channel_id = str(ctx.get("channel_id", "") or "")
    if not team_id or not channel_id:
        return
    agent_id = str(new.get("agent_id", "") or "")
    summary = str(new.get("result_summary") or "").strip()
    assignment_id = str(new.get("assignment_id", "") or "")
    run_ref = _run_ref(assignment_id)
    if new_status == "completed":
        body = summary or "Done — but I have no result text to share."
        if len(body) > _REPLY_MAX_CHARS:
            body = body[:_REPLY_MAX_CHARS] + "…"
        text = f"{body}\n\n_✅ {run_ref}_"
    elif new_status == "failed":
        detail = summary[:600] or "no error detail recorded"
        text = f"❌ I couldn't complete this request: {detail}\n\n_{run_ref}_"
    elif new_status == "awaiting_input":
        # The durable pause (D6): deliver the agent's question and tell the
        # requester HOW to resume — an in-thread @sdlc-agents reply. The thread
        # is bound to the assignment, so the reply needn't name the agent.
        question = str(new.get("pending_question") or "").strip() or (
            "I need more information to continue."
        )
        if len(question) > _REPLY_MAX_CHARS:
            question = question[:_REPLY_MAX_CHARS] + "…"
        text = (
            f"❓ {question}\n\n"
            f"_Reply in this thread with `@sdlc-agents <your answer>` and I'll "
            f"pick the work back up where I left off. Progress so far is "
            f"saved. ({run_ref})_"
        )
    elif new_status == "timed_out":
        text = (
            f"⌛ I stopped waiting for a reply and cleaned up the paused work "
            f"({run_ref}). Mention me again with the "
            f"request if you still need it."
        )
    else:  # awaiting_approval
        text = f"⏳ I need an approval to continue ({run_ref}) — an admin can approve it in the fleet dashboard."
    try:
        reply.post_slack_message(
            team_id, channel_id, text,
            thread_ts=ctx.get("thread_ts") or None,
            agent_id=agent_id,
        )
    except Exception:
        logger.exception("origin reply failed for %s", assignment_id)


def _handle_record(record: dict) -> None:
    if record.get("eventName") != "MODIFY":
        return  # INSERT is the router's create (it notifies run_started itself)
    ddb = record.get("dynamodb", {}) or {}
    new = _plain(ddb.get("NewImage"))
    old = _plain(ddb.get("OldImage"))
    # Skip our own dedup/thread bookkeeping rows (they aren't real assignments).
    if new.get("kind") in ("slack_event_dedup", "notif_thread", "thread_binding"):
        return
    new_status = new.get("status")
    if not new_status or new_status == old.get("status"):
        return  # no status transition → nothing to notify
    mapping = _STATUS_EVENTS.get(new_status)
    if not mapping:
        return
    # 1. The user-facing answer, back to the thread the request came from.
    _reply_to_origin(new, new_status)
    # 2. The ops fan-out to subscribed channels.
    tier, event, template = mapping
    ctx = new.get("source_context") or {}
    notify.notify(
        tier=tier,
        event=event,
        text=template.format(
            agent=new.get("agent_id", "?"),
            id=new.get("assignment_id", "?"),
            summary=str(new.get("result_summary") or "")[:200],
        ),
        repo=str(ctx.get("repo", "") or ""),
        # Same conversation-keyed threading as the router's run_started post —
        # this completion/pause line must land in that thread, and follow-up
        # assignments in the same Slack thread must continue it.
        unit=notify.unit_for(str(new.get("assignment_id", "") or ""), ctx),
        actor=_actor_from(new),
    )


def handler(event, context=None):
    """DynamoDB stream entry point. Processes each record best-effort; a single
    bad record never fails the batch (which would replay the whole batch and
    re-notify the good ones)."""
    for record in event.get("Records", []):
        try:
            _handle_record(record)
        except Exception:
            logger.exception("assignment-notifier record failed")
    return {"statusCode": 200}
