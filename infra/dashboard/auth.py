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

# Cognito groups whose members may call the dashboard API. Must match the groups
# provisioned in infra/foundation/template.yaml (DashboardOperatorGroup /
# DashboardAdminGroup). Kept as constants, not env-driven, so a stack
# misconfiguration cannot silently widen who counts as an operator or admin.
#
# operators view fleet activity (read-only). admins additionally configure the
# fleet (onboard repos, restriction settings) via the write endpoints. admins
# are a strict superset for API purposes: an admin can do anything an operator
# can plus the writes.
OPERATOR_GROUP = "operators"
ADMIN_GROUP = "admins"


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


def _in_group(event: dict, allowed: set[str], label: str) -> bool:
    """True iff the authenticated caller belongs to any group in ``allowed``.

    Fails closed: no claims, no subject, or no matching group → False.
    """
    claims = _claims(event)
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if not sub:
        logger.warning("Dashboard request with no authenticated subject; denying")
        return False
    groups = parse_groups(claims)
    if allowed.isdisjoint(groups):
        logger.warning("Subject %s not authorized for %s; denying", sub, label)
        return False
    return True


def is_operator(event: dict) -> bool:
    """True iff the caller may read fleet activity — a member of ``operators``
    or ``admins`` (admins can do everything operators can)."""
    return _in_group(event, {OPERATOR_GROUP, ADMIN_GROUP}, "read access")


def is_admin(event: dict) -> bool:
    """True iff the caller may configure the fleet — a member of ``admins``.
    Fails closed. Gates the write endpoints (repo onboarding, settings)."""
    return _in_group(event, {ADMIN_GROUP}, "admin access")


def caller_sub(event: dict) -> str:
    """The authenticated Cognito ``sub``, for audit logging. '' if absent."""
    claims = _claims(event)
    return claims.get("sub", "") if isinstance(claims, dict) else ""
