"""HTTP response helpers for the dashboard query API.

Small, dependency-free helpers shared by the query handler: a JSON encoder that
copes with the ``Decimal`` values boto3 returns for DynamoDB numbers, and
success/error envelope builders that mirror the shape the rest of the fleet
already uses (``{"error": ...}`` on failure, a bare data object on success).
"""

import json
from decimal import Decimal

# Permissive CORS for GET-only read endpoints. The dashboard SPA is served from
# a different origin (CloudFront) than the API (API Gateway), so the browser
# needs these on the actual response, not just the preflight. Auth is enforced
# by the Cognito authorizer + operator-group check regardless of origin, so a
# wildcard origin here does not widen access — it only lets the browser read the
# response it was already authorized to receive.
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
}


class _DecimalEncoder(json.JSONEncoder):
    """Render DynamoDB ``Decimal`` numbers as int when integral, else float.

    boto3's resource interface returns every number as ``Decimal``; ``json``
    can't serialize it. Integral values (counts, epoch timestamps, token
    counts) render as ``int`` so the SPA doesn't see ``1.0``; fractional values
    (cost estimates) render as ``float``.
    """

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def json_response(status_code: int, body: dict) -> dict:
    """Build an API Gateway proxy response with the CORS headers and a
    Decimal-aware JSON body."""
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def ok(data) -> dict:
    """200 with the payload as the response body."""
    return json_response(200, data)


def error(status_code: int, message: str) -> dict:
    """Non-2xx with a ``{"error": message}`` envelope (matches the router)."""
    return json_response(status_code, {"error": message})
