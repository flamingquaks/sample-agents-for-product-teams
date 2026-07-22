"""Authorization for the dashboard API — Cedar policies via Amazon Verified
Permissions (AVP).

The dashboard exposes fleet-wide, cross-user data + configuration, so its
endpoints require more than a valid login. Authorization decisions are made by
**AVP** evaluating Cedar policies (the same policy language the agent-tool
gateway uses, now applied to the API), decoupling *who may do what* from app
code so permissions can grow without code changes.

Two coarse actions today, matching the historical operator/admin split:
  - ``Read``  — view fleet activity (GET routes). Gated by ``is_operator``.
  - ``Write`` — configure the fleet (POST/PUT/DELETE). Gated by ``is_admin``.
The Cedar policies (infra/foundation/template.yaml) permit ``operators`` group
members ``Read`` and ``admins`` members ``Read`` + ``Write``. Finer per-route
actions can be added later by adding Cedar policies alone.

Mechanism: the API Gateway Cognito authorizer validates the JWT and forwards its
claims at ``event.requestContext.authorizer.claims`` (it forwards claims, not
the raw token — so we call AVP ``IsAuthorized`` with a principal + group parent
entities built from ``cognito:groups``, rather than ``IsAuthorizedWithToken``).
Fail-closed: any AVP error, missing subject, or non-ALLOW decision denies.

Backward-compat: when ``AVP_POLICY_STORE_ID`` is unset (AVP not yet deployed, or
unit tests), authorization falls back to the equivalent in-process group check —
the SAME semantics, evaluated locally. Both paths fail closed.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Cedar entity-type namespace + names — must match the AVP policy store schema
# and policies in infra/foundation/template.yaml.
CEDAR_NAMESPACE = "SdlcDashboard"
_USER_TYPE = f"{CEDAR_NAMESPACE}::User"
_GROUP_TYPE = f"{CEDAR_NAMESPACE}::Group"
_ACTION_TYPE = f"{CEDAR_NAMESPACE}::Action"
_APP_TYPE = f"{CEDAR_NAMESPACE}::Application"
_APP_ID = "dashboard"
ACTION_READ = "Read"
ACTION_WRITE = "Write"

# Cognito groups. Still the source of the principal's group membership (read from
# the token claim); the Cedar policies key on these names.
OPERATOR_GROUP = "operators"
ADMIN_GROUP = "admins"

_avp = None


def _avp_client():
    global _avp
    if _avp is None:
        import boto3

        _avp = boto3.client("verifiedpermissions")
    return _avp


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


def _authorize(event: dict, action: str, label: str) -> bool:
    """Authorize the caller for ``action`` (Read|Write) on the dashboard.

    Uses AVP when a policy store is configured, else the equivalent local group
    check. Both fail closed: no subject → deny; any error → deny; non-ALLOW → deny.
    """
    claims = _claims(event)
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if not sub:
        logger.warning("Dashboard request with no authenticated subject; denying")
        return False
    groups = parse_groups(claims)

    store_id = os.environ.get("AVP_POLICY_STORE_ID")
    if not store_id:
        # AVP not deployed (or unit test) — evaluate the SAME policy locally.
        # Read: operators or admins. Write: admins only.
        allowed = {ADMIN_GROUP} if action == ACTION_WRITE else {OPERATOR_GROUP, ADMIN_GROUP}
        if allowed.isdisjoint(groups):
            logger.warning("Subject %s not authorized for %s (local); denying", sub, label)
            return False
        return True

    # AVP path: principal is the user; its group memberships are passed as parent
    # entities so the Cedar policies (`principal in Group::"operators"`) match.
    try:
        parents = [{"entityType": _GROUP_TYPE, "entityId": g} for g in sorted(groups)]
        resp = _avp_client().is_authorized(
            policyStoreId=store_id,
            principal={"entityType": _USER_TYPE, "entityId": sub},
            action={"actionType": _ACTION_TYPE, "actionId": action},
            resource={"entityType": _APP_TYPE, "entityId": _APP_ID},
            entities={
                "entityList": [
                    {
                        "identifier": {"entityType": _USER_TYPE, "entityId": sub},
                        "parents": parents,
                    }
                ]
            },
        )
    except Exception:  # noqa: BLE001
        # Never fail open: an AVP outage/permission error denies (the API stays
        # secure, just unavailable — the caller retries).
        logger.exception("AVP authorization call failed for %s; denying", label)
        return False

    decision = resp.get("decision")
    if decision != "ALLOW":
        logger.warning("Subject %s denied %s by AVP (decision=%s)", sub, label, decision)
        return False
    return True


def is_operator(event: dict) -> bool:
    """True iff the caller may read fleet activity (Read). operators + admins."""
    return _authorize(event, ACTION_READ, "read access")


def is_admin(event: dict) -> bool:
    """True iff the caller may configure the fleet (Write). admins only.
    Fails closed. Gates the write endpoints (repo/capability onboarding, settings)."""
    return _authorize(event, ACTION_WRITE, "admin access")


def caller_sub(event: dict) -> str:
    """The authenticated Cognito ``sub``, for audit logging. '' if absent."""
    claims = _claims(event)
    return claims.get("sub", "") if isinstance(claims, dict) else ""


def caller_email(event: dict) -> str:
    """The caller's email (human-readable identity for display). Falls back to
    cognito:username, then sub — always returns something non-empty for an
    authenticated request, but prefers the readable email."""
    claims = _claims(event)
    if not isinstance(claims, dict):
        return ""
    return (
        claims.get("email")
        or claims.get("cognito:username")
        or claims.get("sub", "")
    )
