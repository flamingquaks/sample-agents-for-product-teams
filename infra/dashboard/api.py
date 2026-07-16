"""Dashboard query API Lambda.

Read-only, cross-user view over the ``dispatch-assignments`` table for the
fleet monitoring dashboard. One Lambda fronts four GET routes behind the
Cognito authorizer; every route additionally requires operator-group
membership (see auth.is_operator) before touching data.

Routes (all GET, all operator-only):
    GET /runs                  fleet list, newest-first, filterable + paginated
    GET /runs/{assignment_id}  full detail for one run
    GET /trace                 runs sharing a trace dimension (?dim=&value=)
    GET /stats                 fleet rollups for the dashboard header

The router (infra/dispatch) writes the data; this only reads it. Kept separate
from the Dispatch Router Lambda so a read-path bug can never affect dispatch.
"""

import logging

import auth
import queries
from http_responses import error, ok

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _route(event: dict) -> dict:
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}
    params = event.get("queryStringParameters") or {}

    if resource == "/runs":
        return ok(queries.list_runs(params))

    if resource == "/runs/{assignment_id}":
        assignment_id = path_params.get("assignment_id")
        if not assignment_id:
            return error(400, "missing assignment_id")
        run = queries.get_run(assignment_id)
        if run is None:
            return error(404, f"run {assignment_id} not found")
        return ok(run)

    if resource == "/trace":
        dimension = params.get("dim")
        value = params.get("value")
        if not dimension or not value:
            return error(400, "trace requires ?dim=<dimension>&value=<value>")
        if dimension not in queries.TRACE_DIMENSIONS:
            allowed = ", ".join(sorted(queries.TRACE_DIMENSIONS))
            return error(
                400, f"unknown trace dimension '{dimension}'. allowed: {allowed}"
            )
        return ok(queries.trace(dimension, value))

    if resource == "/stats":
        return ok(queries.stats())

    return error(404, f"no such route: {resource}")


def handler(event, context=None):
    """Lambda entry point. Enforces operator auth, then routes."""
    if not auth.is_operator(event):
        # Do not distinguish "not logged in" from "not an operator" in the body
        # — the authorizer already rejects unauthenticated calls with 401, so a
        # request reaching here with a valid token but no operator role is a
        # genuine 403.
        return error(403, "operator role required")

    try:
        return _route(event)
    except Exception:
        logger.exception(
            "dashboard query failed for %s (caller=%s)",
            event.get("resource"),
            auth.caller_sub(event),
        )
        return error(500, "internal error")
