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

import json
import logging
import os
import re
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_dynamodb = None
_table = None

# Per-direction prices per 1K tokens for the fleet's default model
# (anthropic.claude-sonnet-5 on Bedrock Mantle: $3/M input, $15/M output).
# Used only for a rough ``cost_estimate_usd`` on the dashboard — it is
# explicitly an estimate, not billing. Override via env for a different model.
# When only a combined total is available (older result shapes), the blended
# fallback assumes the fleet's typical ~85/15 input/output split.
_COST_PER_1K_INPUT_USD = float(os.environ.get("COST_PER_1K_INPUT_USD", "0.003"))
_COST_PER_1K_OUTPUT_USD = float(os.environ.get("COST_PER_1K_OUTPUT_USD", "0.015"))
_BLENDED_COST_PER_1K_USD = float(
    os.environ.get(
        "COST_PER_1K_TOKENS_USD",
        str(round(_COST_PER_1K_INPUT_USD * 0.85 + _COST_PER_1K_OUTPUT_USD * 0.15, 6)),
    )
)


def extract_usage(result) -> dict | None:
    """Best-effort per-direction token usage from a Strands agent result.

    Strands surfaces usage on ``result.metrics.accumulated_usage`` (a dict like
    ``{"inputTokens": N, "outputTokens": M, "totalTokens": T}``). Returns
    ``{"input": N, "output": M, "total": T}`` or None. Tolerates missing
    attributes/keys — token capture must never break a run's completion.
    """
    try:
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None)
        if not isinstance(usage, dict):
            return None
        inp = int(usage.get("inputTokens") or 0)
        out = int(usage.get("outputTokens") or 0)
        total = int(usage.get("totalTokens") or (inp + out))
        if total <= 0:
            return None
        return {"input": inp, "output": out, "total": total}
    except Exception:
        logger.debug("Could not extract token usage from result", exc_info=True)
        return None


def extract_token_usage(result) -> int | None:
    """Back-compat wrapper: the combined total from :func:`extract_usage`."""
    usage = extract_usage(result)
    return usage["total"] if usage else None


# A GitHub PR URL as the agents are instructed to report it (workitems/prompts.py
# requires the full html_url, e.g. https://github.com/owner/repo/pull/45). The
# trailing (?!\d) stops a longer number from being truncated.
_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)(?!\d)")

# GitHub MCP tool names that create a PR or branch, and the input keys that
# carry the branch. Tool names vary across MCP server versions, so match on a
# substring rather than an exact name.
_PR_TOOL_HINT = "pull_request"
_BRANCH_KEYS = ("head", "branch", "head_ref", "branchName")


def _walk_content_blocks(messages):
    """Yield each content block across a Strands conversation (agent.messages).
    Each message is {role, content:[block,...]}; a block may carry toolUse or
    toolResult. Tolerant of odd shapes — yields nothing rather than raising."""
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content or []:
            if isinstance(block, dict):
                yield block


def _first_pr_url(text: str) -> tuple[str, str] | None:
    m = _PR_URL_RE.search(text or "")
    return (m.group(0), m.group(1)) if m else None


def extract_trace_refs_from_messages(messages) -> dict:
    """Mine branch/PR trace refs from the agent's tool-call transcript.

    Strands keeps the full conversation on ``agent.messages``. When an agent
    opens a PR through the GitHub MCP server, the transcript carries:
      - a ``toolUse`` block for the create-PR call whose ``input`` includes the
        head branch (``head``/``branch``/…), and
      - a ``toolResult`` block with the PR object (its text carries the
        ``html_url``).
    This is the reliable, structured source — unlike scraping the final prose —
    so we can capture the *branch* here too, not just the PR number. Returns
    ``{}`` when nothing matches; never raises.
    """
    refs: dict = {}
    try:
        for block in _walk_content_blocks(messages):
            use = block.get("toolUse")
            if isinstance(use, dict) and _PR_TOOL_HINT in str(use.get("name", "")).lower():
                inp = use.get("input")
                if isinstance(inp, dict):
                    for key in _BRANCH_KEYS:
                        val = inp.get(key)
                        if isinstance(val, str) and val.strip():
                            refs.setdefault("branch", val.strip())
                            break
            # PR url can appear in a toolResult's text content or in prose;
            # scan the block's stringified content for it.
            if "pr_url" not in refs:
                found = _first_pr_url(json.dumps(block, default=str))
                if found:
                    refs["pr_url"], refs["pr_number"] = found
    except Exception:
        logger.debug("Could not extract trace refs from messages", exc_info=True)
    return refs


def extract_trace_refs_from_result(result) -> dict:
    """Best-effort branch/PR trace refs for a completed run.

    Prefers the structured tool-call transcript (``result.message`` history via
    the agent, passed as ``messages``) but this convenience wrapper works from
    the result's final text alone: it scans for a GitHub PR ``html_url`` (which
    the workitems/docwriter prompts require agents to report) → ``{pr_url,
    pr_number}`` so the run is traceable by PR. Branch is *not* recoverable from
    final text (no stable form there) — callers that have the conversation
    should use ``extract_trace_refs_from_messages`` to also capture the branch.
    Returns ``{}`` when nothing is found; never raises.
    """
    try:
        found = _first_pr_url(str(result))
        return {"pr_url": found[0], "pr_number": found[1]} if found else {}
    except Exception:
        logger.debug("Could not extract trace refs from result", exc_info=True)
        return {}


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


def _usage_fields(token_usage: int | dict | None) -> dict:
    """Build the {token_usage, cost_estimate_usd, ...} update fragment.

    Accepts either the per-direction dict from :func:`extract_usage`
    (``{"input": N, "output": M, "total": T}``) — preferred, prices each
    direction at its real rate — or a bare combined total (back-compat, priced
    at the blended rate). Returns an empty dict when usage is unknown, so we
    never overwrite a real value with None.

    These fields are applied with ``ADD`` (accumulate), never ``SET``: Strands
    ``EventLoopMetrics`` resets per invocation, so a paused-and-resumed run
    reports only its last segment's usage — overwriting would silently drop
    every earlier segment's tokens/cost (durable-repo-work spec, "Cost
    accounting fix")."""
    if isinstance(token_usage, dict):
        total = int(token_usage.get("total") or 0)
        if total <= 0:
            return {}
        inp = int(token_usage.get("input") or 0)
        out = int(token_usage.get("output") or 0)
        if inp or out:
            cost = round(
                (inp / 1000.0) * _COST_PER_1K_INPUT_USD
                + (out / 1000.0) * _COST_PER_1K_OUTPUT_USD,
                6,
            )
        else:
            cost = round((total / 1000.0) * _BLENDED_COST_PER_1K_USD, 6)
        return {
            "token_usage": total,
            "input_tokens": inp,
            "output_tokens": out,
            "cost_estimate_usd": Decimal(str(cost)),
        }
    if not token_usage or token_usage <= 0:
        return {}
    cost = round((token_usage / 1000.0) * _BLENDED_COST_PER_1K_USD, 6)
    return {
        "token_usage": int(token_usage),
        "cost_estimate_usd": Decimal(str(cost)),
    }


def _update_with_usage(
    table,
    assignment_id: str,
    set_parts: list[str],
    usage_fields: dict,
    attr_names: dict,
    attr_values: dict,
) -> None:
    """Apply the status/summary SET update, then ACCUMULATE usage with ADD.

    Two separate writes on purpose:

    - The SET update (status flip, summary, duration) is the run's completion
      record — it must land, and a failure raises to the caller.
    - The usage update uses ``ADD`` so a resumed run's segments accumulate
      (``EventLoopMetrics`` resets per invocation — overwriting would drop all
      but the last segment). It is best-effort: token capture must never break
      a run's completion. Legacy rows carry ``token_usage: NULL`` (the router
      used to write None at create), which ``ADD`` rejects — for those we fall
      back to SET, which is safe precisely because a NULL row has no
      accumulated value to lose.
    """
    if set_parts:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )
    if not usage_fields:
        return
    add_values = {f":{key}": value for key, value in usage_fields.items()}
    add_expr = "ADD " + ", ".join(f"{key} :{key}" for key in usage_fields)
    try:
        table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression=add_expr,
            ExpressionAttributeValues=add_values,
        )
    except ClientError as exc:
        # The SET fallback exists ONLY for legacy rows whose token_usage was
        # created as an explicit NULL (pre-accumulation router) — ADD rejects
        # a NULL-typed attribute with a ValidationException, and overwriting a
        # NULL loses nothing. Any OTHER ClientError (throttle, access denied)
        # must NOT fall back: a SET there would replace the accumulated
        # multi-segment total with just this segment — the exact overwrite the
        # ADD design prevents. Those are logged and dropped (usage capture is
        # best-effort; a lost segment is better than corrupted totals).
        code = (exc.response.get("Error") or {}).get("Code", "")
        if code != "ValidationException":
            logger.warning(
                "usage ADD failed for %s (%s); segment dropped rather than "
                "risk overwriting accumulated totals",
                assignment_id,
                code or "unknown error",
            )
            return
        logger.warning(
            "usage ADD failed for %s (legacy NULL fields); falling back to SET",
            assignment_id,
        )
        try:
            table.update_item(
                Key={"assignment_id": assignment_id},
                UpdateExpression="SET "
                + ", ".join(f"{key} = :{key}" for key in usage_fields),
                ExpressionAttributeValues=add_values,
            )
        except Exception:
            logger.exception("usage capture failed for %s", assignment_id)
    except Exception:
        logger.exception("usage capture failed for %s", assignment_id)


def record_usage(assignment_id: str, token_usage: int | dict | None) -> None:
    """Accumulate a run segment's usage WITHOUT touching status — used when a
    run pauses (awaiting input): the segment's tokens must land now because
    the next segment's metrics start from zero. Best-effort; never raises."""
    if not assignment_id or assignment_id == "default":
        return
    usage_fields = _usage_fields(token_usage)
    if not usage_fields:
        return
    try:
        _update_with_usage(_get_table(), assignment_id, [], usage_fields, {}, {})
    except Exception:
        logger.exception("record_usage failed for %s", assignment_id)


def timeline_event(kind: str, text: str, actor: str = "agent") -> dict:
    """One turn in the run's ``timeline`` — the dashboard's conversation view.

    Kinds in use: ``dispatched`` (router, at create), ``question`` (agent
    pauses via ask_user), ``reply`` (router, the resume answer), ``result``
    (agent completes), ``error`` (agent fails). Append-only; text capped so a
    long transcript can't blow past the 400KB item limit."""
    return {
        "ts": int(time.time()),
        "kind": kind,
        "actor": actor,
        "text": (text or "")[:2000],
    }


def _timeline_append_parts(event: dict, attr_values: dict) -> str:
    """The SET fragment + values that append ``event`` to the timeline in the
    same write as a status flip (if_not_exists covers pre-feature rows)."""
    attr_values[":tl_evt"] = [event]
    attr_values[":tl_empty"] = []
    return "timeline = list_append(if_not_exists(timeline, :tl_empty), :tl_evt)"


def record_commit(
    assignment_id: str,
    *,
    repo: str,
    branch: str,
    sha: str,
    message: str,
    files: list[str],
) -> None:
    """Append a pushed commit (with its changed files) to the run's ``commits``
    list — the dashboard's "what did this run change" record. Best-effort:
    the push already succeeded; bookkeeping must never fail the run."""
    if not assignment_id or assignment_id == "default":
        return
    try:
        _get_table().update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression=(
                "SET commits = list_append(if_not_exists(commits, :empty), :c)"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":c": [
                    {
                        "ts": int(time.time()),
                        "repo": repo,
                        "branch": branch,
                        "sha": sha,
                        "message": (message or "")[:500],
                        # Cap the file list; a huge generated-code commit still
                        # records (count preserved via files_total).
                        "files": list(files or [])[:100],
                        "files_total": len(files or []),
                    }
                ],
            },
        )
    except Exception:
        logger.exception("record_commit failed for %s (%s@%s)", assignment_id, repo, sha[:12])


def complete_assignment(
    assignment_id: str,
    result_summary: str = "",
    token_usage: int | None = None,
):
    """Mark an assignment as completed. Raises on DynamoDB failure.

    :param token_usage: total tokens the run consumed (from the model result's
        usage metrics). When provided, ``token_usage`` and a derived
        ``cost_estimate_usd`` are ACCUMULATED onto the row (ADD — a resumed
        run's segments sum up); when omitted they are left unset.
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
        attr_values[":rs"] = result_summary[:4000]

    # The result turn lands in the same write as the status flip.
    set_parts.append(
        _timeline_append_parts(
            timeline_event("result", result_summary or "(no result text)"),
            attr_values,
        )
    )

    _update_with_usage(
        table, assignment_id, set_parts, _usage_fields(token_usage),
        attr_names, attr_values,
    )
    logger.info("Assignment %s marked completed", assignment_id)


def fail_assignment(
    assignment_id: str, error: str = "", token_usage: int | None = None
):
    """Mark an assignment as failed. Raises on DynamoDB failure.

    Records duration and ACCUMULATES any token usage consumed before the
    failure (ADD — a resumed run's earlier segments are preserved) so a
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

    set_parts.append(
        _timeline_append_parts(
            timeline_event("error", str(error) or "(no error detail)"), attr_values
        )
    )

    _update_with_usage(
        table, assignment_id, set_parts, _usage_fields(token_usage),
        {"#s": "status"}, attr_values,
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
