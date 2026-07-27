"""Durable-work sweeper Lambda (durable-repo-work spec, Phase 3).

Scheduled (rate: 30 minutes). Two sweeps over the assignments table:

1. **Abandoned pauses** — ``awaiting_input`` rows whose ``paused_at`` is older
   than ``PAUSE_TIMEOUT_HOURS`` (default 48h) flip to ``timed_out`` and their
   ``wip/<assignment_id>`` branches are deleted (the work was never resumed;
   the branch would otherwise linger forever). The status flip streams through
   the assignment notifier, which posts the "timed out — re-ask to restart"
   note to the origin thread; the S3 session is left to the session bucket's
   lifecycle expiry (no coupling to the session manager's object layout).

2. **Stuck resumes** — ``resuming`` rows older than ``RESUME_STUCK_MINUTES``
   (default 30m) revert to ``awaiting_input``: the resume invoke was accepted
   but the agent never started (cold-start death, throttle). The pause fields
   (interrupt_id / snapshot / question) are still on the row — the revert
   makes the thread resumable again, and the notifier re-posts the question so
   the user knows to re-send their reply.

3. **Stale dispatches** — ``dispatched`` rows older than
   ``DISPATCH_TIMEOUT_HOURS`` (default 4h; runtimes cap invocations well under
   that) flip to ``failed``: the runtime died without writing a terminal
   status. Without this, a crashed run pins one of the agent's concurrency
   slots until the row's 30-day TTL. The notifier posts the failure to the
   origin thread so the requester isn't left waiting on a ghost.

Both flips are CONDITIONAL on the row still being in the swept status, so a
reply that lands mid-sweep wins the race and the sweep backs off.

The scan-with-filter is deliberate: statuses live on the agent_id-status GSI
(agent_id is the hash key), so there is no query-by-status-alone path, and the
table is small (30-day TTL, sample volume). Revisit with a sparse status GSI
if row counts grow.
"""

import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Attr

import github_app

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PAUSE_TIMEOUT_HOURS = float(os.environ.get("PAUSE_TIMEOUT_HOURS", "48"))
RESUME_STUCK_MINUTES = float(os.environ.get("RESUME_STUCK_MINUTES", "30"))
DISPATCH_TIMEOUT_HOURS = float(os.environ.get("DISPATCH_TIMEOUT_HOURS", "4"))

_ddb = None


def _table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(
            os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments")
        )
    return _ddb


def _scan_status(status: str) -> list[dict]:
    """All real assignment rows currently in ``status`` (paginated scan)."""
    table = _table()
    items: list[dict] = []
    kwargs = {"FilterExpression": Attr("status").eq(status) & Attr("agent_id").exists()}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _conditional_flip(assignment_id: str, from_status: str, to_status: str, note: str) -> bool:
    """Flip status only if the row is still in ``from_status`` (a reply landing
    mid-sweep wins). Returns True when this sweep did the flip. The note also
    lands as a timeline turn so the run's conversation view records how the
    run actually ended."""
    table = _table()
    try:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression=(
                "SET #s = :to, result_summary = :note, completed_at = :now, "
                "timeline = list_append(if_not_exists(timeline, :tl_empty), :tl_evt)"
            ),
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":to": to_status,
                ":from": from_status,
                ":note": note,
                ":now": int(time.time()),
                ":tl_empty": [],
                ":tl_evt": [
                    {
                        "ts": int(time.time()),
                        "kind": "error" if to_status == "failed" else to_status,
                        "actor": "sweeper",
                        "text": note[:2000],
                    }
                ],
            },
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _revert_to_awaiting(assignment_id: str) -> bool:
    """Stuck-resume revert: ``resuming → awaiting_input`` (pause fields are
    still on the row, so the thread becomes resumable again)."""
    table = _table()
    try:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression="SET #s = :to",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":to": "awaiting_input", ":from": "resuming"},
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _delete_wip_branches(row: dict) -> None:
    """Delete each snapshot repo's wip branch (abandoned work cleanup). Best
    effort per branch — a failure is logged, never blocks the sweep. Token is
    contents:write scoped to the one repo, minted per delete."""
    for entry in row.get("workspace_snapshot") or []:
        repo = str(entry.get("repo", ""))
        branch = str(entry.get("branch", ""))
        if not repo or not branch.startswith("wip/"):
            continue
        try:
            import requests

            token = github_app.scoped_installation_token(
                repo, permissions={"contents": "write", "metadata": "read"}
            )
            resp = requests.delete(
                f"{github_app.GITHUB_API_BASE}/repos/{repo}/git/refs/heads/{branch}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            # 204 deleted; 422 = ref already gone — both fine.
            if resp.status_code not in (204, 422):
                logger.warning(
                    "wip cleanup: delete %s@%s returned %s", repo, branch, resp.status_code
                )
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            logger.exception("wip cleanup failed for %s@%s", repo, branch)


def sweep_abandoned_pauses(now: float | None = None) -> int:
    """awaiting_input older than PAUSE_TIMEOUT_HOURS → timed_out + wip cleanup."""
    current = time.time() if now is None else now
    cutoff = current - PAUSE_TIMEOUT_HOURS * 3600
    flipped = 0
    for row in _scan_status("awaiting_input"):
        paused_at = row.get("paused_at") or row.get("created_at") or 0
        if float(paused_at) > cutoff:
            continue
        assignment_id = str(row["assignment_id"])
        note = (
            f"Timed out waiting for a reply ({PAUSE_TIMEOUT_HOURS:g}h). "
            "The paused work was cleaned up — mention the agent again to restart."
        )
        if _conditional_flip(assignment_id, "awaiting_input", "timed_out", note):
            _delete_wip_branches(row)
            flipped += 1
            logger.info("swept abandoned pause %s -> timed_out", assignment_id)
    return flipped


def sweep_stuck_resumes(now: float | None = None) -> int:
    """resuming older than RESUME_STUCK_MINUTES → back to awaiting_input."""
    current = time.time() if now is None else now
    cutoff = current - RESUME_STUCK_MINUTES * 60
    reverted = 0
    for row in _scan_status("resuming"):
        # paused_at is untouched by the resume lock; it plus the stuck window
        # bounds how long a resume may sit without the agent starting.
        marker = row.get("resume_started_at") or row.get("paused_at") or row.get("created_at") or 0
        if float(marker) > cutoff:
            continue
        assignment_id = str(row["assignment_id"])
        if _revert_to_awaiting(assignment_id):
            reverted += 1
            logger.info("reverted stuck resume %s -> awaiting_input", assignment_id)
    return reverted


def sweep_stale_dispatches(now: float | None = None) -> int:
    """dispatched older than DISPATCH_TIMEOUT_HOURS → failed (dead runtime).

    Frees the concurrency slot and tells the requester via the notifier's
    run_failed path instead of silence until the row's TTL.

    Age is measured from the run's LAST (re)start: ``resumed_at`` when the row
    was resumed after a pause (mark_resumed writes it), else ``created_at``.
    Keying on created_at alone would sweep an actively-running resumed
    assignment to ``failed`` whenever the human's reply arrived more than the
    window after the original dispatch — a legal pause can last 48h."""
    current = time.time() if now is None else now
    cutoff = current - DISPATCH_TIMEOUT_HOURS * 3600
    failed = 0
    for row in _scan_status("dispatched"):
        started = row.get("resumed_at") or row.get("created_at") or 0
        if float(started) > cutoff:
            continue
        assignment_id = str(row["assignment_id"])
        note = (
            f"The run went silent for over {DISPATCH_TIMEOUT_HOURS:g}h (runtime "
            "died without reporting). Re-ask to retry."
        )
        if _conditional_flip(assignment_id, "dispatched", "failed", note):
            failed += 1
            logger.info("swept stale dispatch %s -> failed", assignment_id)
    return failed


def handler(event=None, context=None):
    timed_out = sweep_abandoned_pauses()
    reverted = sweep_stuck_resumes()
    failed = sweep_stale_dispatches()
    logger.info(
        "durable sweep: %d timed out, %d resumes reverted, %d stale dispatches failed",
        timed_out, reverted, failed,
    )
    return {"timed_out": timed_out, "resumes_reverted": reverted, "stale_failed": failed}
