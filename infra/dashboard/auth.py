"""Operator authorization for the dashboard query API.

The dashboard exposes fleet-wide, cross-user run data, so its endpoints require
more than a valid login: the caller must be a member of the ``operators``
Cognito group. This is deliberately separate from the Dispatch Router's
per-agent allowlist (``router.check_authorization``) — that gates *who may
invoke an agent*; this gates *who may see the whole fleet's activity*.

The API Gateway Cognito authorizer validates the JWT and forwards its claims at
``event.requestContext.authorizer.claims``. Group membership arrives in the
``cognito:groups`` claim, which API Gateway flattens to a string.
"""

import logging

logger = logging.getLogger(__name__)

# Cognito group whose members may call the dashboard API. Must match the group
# provisioned in infra/foundation/template.yaml (OperatorGroup). Kept as a
# constant, not env-driven, so a stack misconfiguration cannot silently widen
# who counts as an operator.
OPERATOR_GROUP = "operators"


def _claims(event: dict) -> dict:
    ctx = event.get("requestContext") or {}
    authorizer = ctx.get("authorizer") or {}
    # REST API Cognito authorizer nests claims under "claims"; be tolerant of a
    # flattened shape just in case.
    claims = authorizer.get("claims")
    return claims if isinstance(claims, dict) else authorizer


def parse_groups(claims: dict) -> set[str]:
    """Parse the ``cognito:groups`` claim into a set of group names.

    API Gateway flattens a multi-value group claim to a string that is either a
    bracketed space-separated list (``"[operators admins]"``) or comma-
    separated; a single group is a bare string. Missing/blank ⇒ empty set.
    """
    raw = claims.get("cognito:groups") if claims else None
    if not isinstance(raw, str) or not raw:
        return set()
    stripped = raw.strip().lstrip("[").rstrip("]")
    return {g.strip() for g in stripped.replace(",", " ").split() if g.strip()}


def is_operator(event: dict) -> bool:
    """True iff the authenticated caller is a member of the operator group.

    Fails closed: no claims, no groups claim, or the group absent → False.
    """
    claims = _claims(event)
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if not sub:
        logger.warning("Dashboard request with no authenticated subject; denying")
        return False
    groups = parse_groups(claims)
    if OPERATOR_GROUP not in groups:
        logger.warning("Subject %s not in %s group; denying", sub, OPERATOR_GROUP)
        return False
    return True


def caller_sub(event: dict) -> str:
    """The authenticated Cognito ``sub``, for audit logging. '' if absent."""
    claims = _claims(event)
    return claims.get("sub", "") if isinstance(claims, dict) else ""
