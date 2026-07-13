"""DynamoDB read queries backing the dashboard API.

Read-only access to the ``dispatch-assignments`` table the Dispatch Router and
agents write. Every function returns plain dicts/lists (Decimal-valued numbers
are serialized by responses._DecimalEncoder at the edge).

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


def _encode_cursor(last_evaluated_key: dict | None) -> str | None:
    if not last_evaluated_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key).encode()).decode()


def _decode_cursor(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode()).decode())
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
    # ``resume`` is the cursor the caller should send to fetch the next page. We
    # advance it to the key of each item as we accept it, so if the page fills
    # mid-DynamoDB-page we resume exactly after the last item we returned — never
    # skipping the rows we collected-but-didn't-return. When filters are present
    # a GSI page may yield fewer than `limit` matches, so we page (bounded) until
    # the page is full or the index is exhausted.
    resume = None
    exhausted = False
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
        items = resp.get("Items", [])
        page_full = False
        for item in items:
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
        if not cursor:
            exhausted = True
            break
        if page_full:
            break

    # No next page if we drained the index and didn't fill the page.
    next_token = None if (exhausted and len(runs) < limit) else _encode_cursor(resume)
    return {"runs": runs, "next_token": next_token}


def get_run(assignment_id: str) -> dict | None:
    """Full run record by id, or None if absent."""
    resp = _get_table().get_item(Key={"assignment_id": assignment_id})
    return resp.get("Item")


def _scan_all_runs(cap: int) -> tuple[list[dict], bool]:
    """Read up to ``cap`` runs newest-first off the fleet index. Returns
    (items, truncated)."""
    table = _get_table()
    items: list[dict] = []
    cursor = None
    while len(items) < cap:
        query = {
            "IndexName": ALL_RUNS_INDEX,
            "KeyConditionExpression": Key("gsi_all").eq(ALL_RUNS_PK),
            "ScanIndexForward": False,
            "Limit": min(_MAX_LIMIT, cap - len(items)),
        }
        if cursor:
            query["ExclusiveStartKey"] = cursor
        resp = table.query(**query)
        items.extend(resp.get("Items", []))
        cursor = resp.get("LastEvaluatedKey")
        if not cursor:
            return items, False
    return items, True


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
