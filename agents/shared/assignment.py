"""Assignment lifecycle management.

Called by agents to mark assignments as completed or failed in the
dispatch assignments table. This closes the loop that the Dispatch
Router opens when it creates an assignment.

Beyond status, agents enrich the run record the monitoring dashboard reads:

- ``duration_seconds`` is computed at close (``completed_at - created_at``) so
  the dashboard doesn't have to derive it per row.
- ``token_usage`` / ``cost_estimate_usd`` are written from the model's usage
  metrics when the agent passes them (previously always ``None``).
- ``update_trace_refs`` lets an agent add trace dimensions it learns at
  runtime — most importantly the branch it created and the PR it opened — so a
  body of work is traceable across agents even though those identifiers don't
  exist at dispatch time.
"""

import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_dynamodb = None
_table = None

# Approximate blended Bedrock price per 1K tokens for the fleet's default model
# (Claude Opus 4.7). Used only for a rough ``cost_estimate_usd`` on the
# dashboard — it is explicitly an estimate, not billing. Override via env for a
# different model/region. Kept as a single blended rate (not split in/out)
# because the Strands result exposes a combined total; refine if per-direction
# counts become available.
_COST_PER_1K_TOKENS_USD = float(os.environ.get("COST_PER_1K_TOKENS_USD", "0.03"))


def extract_token_usage(result) -> int | None:
    """Best-effort total token count from a Strands agent result.

    Strands surfaces usage on ``result.metrics.accumulated_usage`` (a dict like
    ``{"inputTokens": N, "outputTokens": M, "totalTokens": T}``). Shapes vary
    across SDK versions and some results carry none, so this tolerates missing
    attributes/keys and returns None rather than raising — token capture must
    never break a run's completion.
    """
    try:
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None)
        if not isinstance(usage, dict):
            return None
        total = usage.get("totalTokens")
        if total is None:
            inp = usage.get("inputTokens") or 0
            out = usage.get("outputTokens") or 0
            total = inp + out
        total = int(total)
        return total if total > 0 else None
    except Exception:
        logger.debug("Could not extract token usage from result", exc_info=True)
        return None


def _get_table():
    global _dynamodb, _table
    if _table is None:
        _dynamodb = boto3.resource("dynamodb")
        table_name = os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments-dev")
        _table = _dynamodb.Table(table_name)
    return _table


def _fetch_created_at(table, assignment_id: str) -> int | None:
    """Read ``created_at`` so we can compute duration at close. Best-effort:
    returns None if the item or field is missing (duration is then left unset
    rather than wrong)."""
    try:
        resp = table.get_item(
            Key={"assignment_id": assignment_id},
            ProjectionExpression="created_at",
        )
    except Exception:
        logger.exception(
            "Could not read created_at for %s; duration will be unset", assignment_id
        )
        return None
    created = (resp.get("Item") or {}).get("created_at")
    return int(created) if created is not None else None


def _usage_fields(token_usage: int | None) -> dict:
    """Build the {token_usage, cost_estimate_usd} update fragment from a token
    count. Returns an empty dict when usage is unknown, so we never overwrite a
    real value with None."""
    if not token_usage or token_usage <= 0:
        return {}
    cost = round((token_usage / 1000.0) * _COST_PER_1K_TOKENS_USD, 6)
    return {
        "token_usage": int(token_usage),
        "cost_estimate_usd": Decimal(str(cost)),
    }


def complete_assignment(
    assignment_id: str,
    result_summary: str = "",
    token_usage: int | None = None,
):
    """Mark an assignment as completed. Raises on DynamoDB failure.

    :param token_usage: total tokens the run consumed (from the model result's
        usage metrics). When provided, ``token_usage`` and a derived
        ``cost_estimate_usd`` are recorded; when omitted they are left unset.
    """
    if not assignment_id or assignment_id == "default":
        logger.info(
            "Skipping assignment update (no real assignment_id: %r)", assignment_id
        )
        return

    table = _get_table()
    now = int(time.time())

    set_parts = ["#s = :s", "completed_at = :ca"]
    attr_names = {"#s": "status"}
    attr_values = {":s": "completed", ":ca": now}

    created_at = _fetch_created_at(table, assignment_id)
    if created_at is not None:
        set_parts.append("duration_seconds = :dur")
        attr_values[":dur"] = max(0, now - created_at)

    if result_summary:
        set_parts.append("result_summary = :rs")
        attr_values[":rs"] = result_summary[:1000]

    for key, value in _usage_fields(token_usage).items():
        set_parts.append(f"{key} = :{key}")
        attr_values[f":{key}"] = value

    table.update_item(
        Key={"assignment_id": assignment_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )
    logger.info("Assignment %s marked completed", assignment_id)


def fail_assignment(
    assignment_id: str, error: str = "", token_usage: int | None = None
):
    """Mark an assignment as failed. Raises on DynamoDB failure.

    Records duration and any token usage consumed before the failure so a
    crashed run still shows cost on the dashboard.
    """
    if not assignment_id or assignment_id == "default":
        logger.info(
            "Skipping fail_assignment (no real assignment_id: %r)", assignment_id
        )
        return

    table = _get_table()
    now = int(time.time())

    set_parts = ["#s = :s", "completed_at = :ca", "result_summary = :rs"]
    attr_values = {":s": "failed", ":ca": now, ":rs": str(error)[:1000]}

    created_at = _fetch_created_at(table, assignment_id)
    if created_at is not None:
        set_parts.append("duration_seconds = :dur")
        attr_values[":dur"] = max(0, now - created_at)

    for key, value in _usage_fields(token_usage).items():
        set_parts.append(f"{key} = :{key}")
        attr_values[f":{key}"] = value

    table.update_item(
        Key={"assignment_id": assignment_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=attr_values,
    )
    logger.info("Assignment %s marked failed", assignment_id)


def update_trace_refs(assignment_id: str, **refs):
    """Merge additional trace references onto an assignment.

    Agents call this when they learn a traceable identifier at runtime that the
    router couldn't know at dispatch — e.g. the branch they created or the PR
    they opened::

        update_trace_refs(assignment_id, branch="feat/123-fix", pr_url="https://…", pr_number="45")

    Only non-empty values are written; existing keys under ``trace_refs`` are
    preserved and merged (DynamoDB nested-map SET on individual keys), so this
    never clobbers refs the router already stamped. No-ops on a sentinel id.
    """
    if not assignment_id or assignment_id == "default":
        logger.info(
            "Skipping update_trace_refs (no real assignment_id: %r)", assignment_id
        )
        return

    clean = {
        k: str(v).strip() for k, v in refs.items() if v is not None and str(v).strip()
    }
    if not clean:
        return

    table = _get_table()
    conditional_exc = table.meta.client.exceptions.ConditionalCheckFailedException

    # Both writes are gated on ``attribute_exists(assignment_id)`` so a stale or
    # bogus id is a clean no-op rather than an upsert: update_item is an upsert
    # by default, and without this guard calling update_trace_refs on an id the
    # router never wrote would create a junk stub row that pollutes the
    # dashboard. The condition is cheap and makes the function safe to call
    # speculatively.
    #
    # We can't SET both ``trace_refs`` and ``trace_refs.<key>`` in one
    # expression (overlapping document paths are rejected), so we first ensure
    # the parent map exists (only when the item exists and the map is absent),
    # then set the individual keys.
    try:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression="SET trace_refs = :empty",
            ConditionExpression="attribute_exists(assignment_id) AND attribute_not_exists(trace_refs)",
            ExpressionAttributeValues={":empty": {}},
        )
    except conditional_exc:
        # Either the item doesn't exist (nothing to enrich) or the map is
        # already present (the common router-stamped case). Both are fine — the
        # keyed write below carries the same attribute_exists guard.
        pass

    set_parts = []
    attr_names = {"#tr": "trace_refs"}
    attr_values = {}
    for key, value in clean.items():
        set_parts.append(f"#tr.#{key} = :{key}")
        attr_names[f"#{key}"] = key
        attr_values[f":{key}"] = value

    try:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(assignment_id)",
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )
    except conditional_exc:
        # Item vanished between the two writes (e.g. TTL expiry) — nothing to
        # enrich; drop silently.
        logger.info(
            "update_trace_refs: assignment %s no longer exists; skipped", assignment_id
        )
        return
    except ClientError:
        # Trace enrichment is best-effort and must never break the agent run
        # that called it (e.g. a ValidationException if a legacy item's
        # trace_refs was somehow stored as a non-map). Log and move on.
        logger.warning(
            "update_trace_refs: could not merge refs for %s",
            assignment_id,
            exc_info=True,
        )
        return
    logger.info("Assignment %s trace_refs updated: %s", assignment_id, list(clean))
