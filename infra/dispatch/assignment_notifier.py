"""Assignment-stream notifier Lambda (spec §18.1/§18.3).

Fleet lifecycle notifications that originate on the **agent** side — a run
completing, failing, or entering an awaiting-approval state — can't be emitted by
the agents themselves (agents hold no Slack credential, by design). Instead this
Lambda subscribes to the **DynamoDB stream** on the assignments table and maps
each status transition to a `notify.notify()` fan-out. This keeps ALL fleet-event
notifications flowing through the one `notify` seam (the Router emits the
dispatch-time events directly; this covers the completion-time ones) without
widening any agent's IAM.

Only MODIFY records whose ``status`` actually changed are acted on (a token-usage
update to an already-`completed` row is a no-op). The unit-of-work key is the
``assignment_id`` so a run's start/finish thread together in Slack; the actor is
the run's requester, resolved to the right Slack user per workspace by the
identity map. Best-effort: a delivery failure is logged, never retried into a
poison loop (we return success so the shard iterator advances).
"""

import logging

from boto3.dynamodb.types import TypeDeserializer

import notify

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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


def _handle_record(record: dict) -> None:
    if record.get("eventName") != "MODIFY":
        return  # INSERT is the router's create (it notifies run_started itself)
    ddb = record.get("dynamodb", {}) or {}
    new = _plain(ddb.get("NewImage"))
    old = _plain(ddb.get("OldImage"))
    # Skip our own dedup/thread bookkeeping rows (they aren't real assignments).
    if new.get("kind") in ("slack_event_dedup", "notif_thread"):
        return
    new_status = new.get("status")
    if not new_status or new_status == old.get("status"):
        return  # no status transition → nothing to notify
    mapping = _STATUS_EVENTS.get(new_status)
    if not mapping:
        return
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
        unit=str(new.get("assignment_id", "") or ""),
        actor=_actor_from(new),
    )


def handler(event, context=None):
    """DynamoDB stream entry point. Processes each record best-effort; a single
    bad record never fails the batch (which would replay the whole batch and
    re-notify the good ones)."""
    for record in event.get("Records", []):
        try:
            _handle_record(record)
        except Exception:  # noqa: BLE001 — notifications are best-effort
            logger.exception("assignment-notifier record failed")
    return {"statusCode": 200}
