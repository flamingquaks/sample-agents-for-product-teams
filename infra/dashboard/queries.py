"""DynamoDB read queries backing the dashboard API.

Read-only access to the ``dispatch-assignments`` table the Dispatch Router and
agents write. Every function returns plain dicts/lists (Decimal-valued numbers
are serialized by http_responses._DecimalEncoder at the edge).

Access paths:
- ``list_runs`` → ``AllRunsIndex`` GSI (constant PK, SK created_at) for
  fleet-wide newest-first paging, with optional post-filters.
- ``get_run`` → GetItem by assignment_id.
- ``trace`` → ``AllRunsIndex`` scanned newest-first, filtered to rows whose
  ``trace_refs.<dim>`` equals the requested value (the multi-agent work sharing
  a branch / jira key / issue / repo / asana task).
- ``stats`` → ``AllRunsIndex`` aggregated into fleet counts.

The pagination cursor is the DynamoDB ``LastEvaluatedKey`` round-tripped as
opaque base64 JSON.
"""

import base64
import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

# ALL_RUNS_PK / the index name are defined by the dispatch enrichment module and
# the SAM template respectively; duplicated here as constants because the
# dashboard package deploys as its own Lambda and does not import from
# infra/dispatch. Keep in sync with enrichment.ALL_RUNS_PK ("run") and the
# AllRunsIndex GSI in infra/foundation/template.yaml.
ALL_RUNS_PK = "run"
ALL_RUNS_INDEX = "AllRunsIndex"

# Trace dimensions the API will group by, mapped to the trace_refs key they
# live under. The set is intentionally open: the SPA discovers available
# dimensions from the trace_refs actually present on runs, and any key an agent
# or the router writes is groupable here without code change — this list only
# validates the query param so a typo returns 400 rather than an empty result.
TRACE_DIMENSIONS = {
    "repo",
    "branch",
    "pr_number",
    "pr_url",
    "issue_number",
    "jira_key",
    "asana_task_gid",
    "project_gid",
    "project_name",
}

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 25
# Cap on rows scanned for trace/stats aggregation so a huge table can't turn one
# request into an unbounded scan. At sample volume (30-day TTL) this comfortably
# covers the whole fleet; past it, the response notes truncation.
_AGG_SCAN_CAP = 2000

_table = None


def _get_table():
    global _table
    if _table is None:
        name = os.environ["ASSIGNMENTS_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


# A DynamoDB LastEvaluatedKey holds attribute values whose Python types
# (Decimal for numbers, str for strings) don't all survive a plain JSON
# round-trip and, when handed back as ExclusiveStartKey, must match the stored
# attribute's DynamoDB type exactly. Rather than hand-encode types (a Python
# float is rejected by boto3; a numeric string would be sent as a String-typed
# key and mismatch a Number key), we round-trip through boto3's own wire form:
# serialize each value to its ``{"N": "..."}`` / ``{"S": "..."}`` descriptor
# (all JSON-native strings, lossless), then deserialize back to the exact Python
# types (numbers → Decimal) on the way in. This is invariant-free: it doesn't
# rely on created_at always being an integer.
_ser = TypeSerializer()
_deser = TypeDeserializer()


def _encode_cursor(last_evaluated_key: dict | None) -> str | None:
    if not last_evaluated_key:
        return None
    wire = {k: _ser.serialize(v) for k, v in last_evaluated_key.items()}
    return base64.urlsafe_b64encode(json.dumps(wire).encode()).decode()


def _decode_cursor(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        wire = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        return {k: _deser.deserialize(v) for k, v in wire.items()}
    except Exception:
        # A malformed cursor resets to the first page rather than erroring — the
        # dashboard treats next_token as opaque and a bad one is non-fatal.
        return None


def _clamp_limit(raw: str | None) -> int:
    if not raw:
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


# Post-query filters keyed by query-param name → the run field they match on.
# Applied client-side after the GSI query because the GSI is keyed only on
# (gsi_all, created_at); these are low-cardinality equality filters.
_LIST_FILTERS = ("agent_id", "status", "source", "requester")


def _matches_filters(item: dict, filters: dict) -> bool:
    for field, wanted in filters.items():
        if str(item.get(field, "")) != wanted:
            return False
    return True


def list_runs(params: dict) -> dict:
    """Fleet list, newest-first, paginated. Optional equality filters:
    agent_id, status, source, requester.

    Returns ``{"runs": [...], "next_token": str|None}``.
    """
    table = _get_table()
    limit = _clamp_limit(params.get("limit"))
    filters = {f: params[f] for f in _LIST_FILTERS if params.get(f)}

    runs: list[dict] = []
    cursor = _decode_cursor(params.get("next_token"))
    # ``resume`` is the GSI key of the last item we RETURNED. When the page fills
    # mid-DynamoDB-page we resume exactly after it, so we never skip the matches
    # we collected-but-didn't-return. When filters are present a GSI page may
    # yield fewer than `limit` matches, so we page (bounded) until the page is
    # full or the index is exhausted.
    resume = None
    page_full = False
    for _ in range(20):  # page cap: bounds worst-case fan-out on a sparse filter
        query = {
            "IndexName": ALL_RUNS_INDEX,
            "KeyConditionExpression": Key("gsi_all").eq(ALL_RUNS_PK),
            "ScanIndexForward": False,  # newest first
            "Limit": limit,
        }
        if cursor:
            query["ExclusiveStartKey"] = cursor
        resp = table.query(**query)
        page_full = False
        for item in resp.get("Items", []):
            if _matches_filters(item, filters):
                runs.append(item)
                # The GSI key of this item = where the next page resumes.
                resume = {
                    "assignment_id": item["assignment_id"],
                    "gsi_all": item.get("gsi_all", ALL_RUNS_PK),
                    "created_at": item["created_at"],
                }
                if len(runs) >= limit:
                    page_full = True
                    break
        cursor = resp.get("LastEvaluatedKey")
        # Stop when the page is full, or when the index is drained (no cursor).
        if page_full or not cursor:
            break

    # Continuation token, by why the loop ended:
    #  - page filled: we broke mid-scan, so more rows may follow the last one we
    #    returned — resume just after it (``resume``), NOT ``cursor`` (which is
    #    past the whole DynamoDB page and would skip the unexamined tail).
    #  - page not full but the index still has rows (hit the 20-page cap):
    #    continue from the raw DynamoDB ``cursor`` so a selective filter can't
    #    silently truncate matches beyond the scan window.
    #  - page not full and index drained: genuinely no more pages.
    if page_full:
        next_token = _encode_cursor(resume)
    elif cursor:
        next_token = _encode_cursor(cursor)
    else:
        next_token = None
    return {"runs": runs, "next_token": next_token}


def get_run(assignment_id: str) -> dict | None:
    """Full run record by id, or None if absent."""
    resp = _get_table().get_item(Key={"assignment_id": assignment_id})
    return resp.get("Item")


def _scan_all_runs(cap: int) -> tuple[list[dict], bool]:
    """Read runs newest-first off the fleet index, up to ``cap``. Returns
    (items, truncated) where items has at most ``cap`` entries.

    Truncation is detected by reading one item PAST ``cap``: if the fleet holds
    more than ``cap`` runs we collect ``cap + 1``, report truncated, and trim
    back to ``cap``. Relying on a lingering ``LastEvaluatedKey`` instead would
    false-positive when the fleet holds exactly ``cap`` rows (DynamoDB returns a
    cursor whenever a query hits its ``Limit``, even with nothing left to read).
    """
    table = _get_table()
    probe = cap + 1  # one extra row is the truncation signal
    items: list[dict] = []
    cursor = None
    while len(items) < probe:
        query = {
            "IndexName": ALL_RUNS_INDEX,
            "KeyConditionExpression": Key("gsi_all").eq(ALL_RUNS_PK),
            "ScanIndexForward": False,
            "Limit": min(_MAX_LIMIT, probe - len(items)),
        }
        if cursor:
            query["ExclusiveStartKey"] = cursor
        resp = table.query(**query)
        items.extend(resp.get("Items", []))
        cursor = resp.get("LastEvaluatedKey")
        if not cursor:
            break
    truncated = len(items) > cap
    return items[:cap], truncated


def trace(dimension: str, value: str) -> dict:
    """All runs whose ``trace_refs[dimension] == value``, newest-first.

    This is the multi-agent traceability join: e.g. every agent's run on
    branch X, or on Jira key ABC-123.
    """
    items, truncated = _scan_all_runs(_AGG_SCAN_CAP)
    matched = [
        item
        for item in items
        if str((item.get("trace_refs") or {}).get(dimension, "")) == value
    ]
    return {
        "dimension": dimension,
        "value": value,
        "runs": matched,
        "truncated": truncated,
    }


def stats() -> dict:
    """Fleet rollups for the dashboard header: totals and counts by status,
    agent, and source, plus an active-now count."""
    items, truncated = _scan_all_runs(_AGG_SCAN_CAP)

    active_statuses = {"dispatched"}
    by_status: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    by_source: dict[str, int] = {}
    active = 0
    for item in items:
        status = str(item.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        agent = str(item.get("agent_id", "unknown"))
        by_agent[agent] = by_agent.get(agent, 0) + 1
        source = str(item.get("source", "unknown"))
        by_source[source] = by_source.get(source, 0) + 1
        if status in active_statuses:
            active += 1

    return {
        "total": len(items),
        "active": active,
        "by_status": by_status,
        "by_agent": by_agent,
        "by_source": by_source,
        "truncated": truncated,
    }
