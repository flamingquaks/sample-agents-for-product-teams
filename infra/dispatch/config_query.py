"""Shared paged reader for the ``fleet-config`` table (dispatch side).

Every dispatch-side reader (``identity``, ``notify``, ``slack_notify``,
``fleet_config``, ``trigger_grants``) wants "all rows of one kind". The table
carries a ``kind`` attribute on every row and a ``kind-index`` GSI (partition
``kind``, sort ``pk``), so a kind read is a bounded **Query**, not a full-table
Scan + ``FilterExpression`` (which reads and bills O(table) and grows with the
whole fleet's data, not the one kind).

This module is the ONE place that paging + the index name live, so a change to
either (page cap, projection, index rename) is a single edit rather than five
hand-copied ``while start_key`` loops — one of which would inevitably drift and
silently drop rows past the first 1 MB page.
"""

import os

import boto3

KIND_INDEX = "kind-index"

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["FLEET_CONFIG_TABLE"])
    return _table


def query_kind(kind: str, *, pk_prefix: str | None = None) -> list[dict]:
    """All rows of one ``kind`` via the kind-index, draining every page.

    ``pk_prefix`` (optional) range-narrows the Query on the index sort key
    (``pk``) — e.g. ``"slack_channel#<team>#"`` returns only that team's
    channels without reading the other teams' rows. Rows come back in ``pk``
    order; callers that need another order sort in memory.
    """
    table = _get_table()
    rows: list[dict] = []
    start_key = None
    while True:
        kwargs: dict = {
            "IndexName": KIND_INDEX,
            "KeyConditionExpression": "#k = :k",
            "ExpressionAttributeNames": {"#k": "kind"},
            "ExpressionAttributeValues": {":k": kind},
        }
        if pk_prefix:
            kwargs["KeyConditionExpression"] = "#k = :k AND begins_with(pk, :p)"
            kwargs["ExpressionAttributeValues"][":p"] = pk_prefix
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.query(**kwargs)
        rows.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    return rows
