"""Trigger authorization for the Dispatch Router — Cedar policies via Amazon
Verified Permissions (AVP).

This is the THIRD Cedar surface in the fleet, and the one the router consults on
every inbound dispatch (decision "C" in docs/specs/slack-connectors-spec.md §2):

  - the dashboard API's authz (infra/dashboard/auth.py) decides who may read/write
    the dashboard,
  - the AgentCore Gateway policy engine decides which tool calls an agent may make,
  - and THIS module decides whether a given *sender* may *trigger* a given *agent*
    from a given *source / workspace / channel*.

Historically that last decision was a flat in-code allowlist match on a
capability's ``authorization.users`` (router.check_authorization). That has no
notion of WHERE a trigger happened (which Slack workspace / channel), which the
multi-workspace Slack connector needs. So trigger authorization moves to AVP,
evaluating the ``SdlcTrigger`` policy store the admin API manages (one
template-linked Cedar policy per admin-authored rule — see
infra/dashboard/trigger_policy_sync.py).

Mechanism mirrors auth._authorize: build an ``IsAuthorized`` call with the sender
as the principal, ``Trigger`` as the action, the agent id as the resource, and
``{workspace, channel, source}`` as the request context; pass the principal's
group memberships as parent entities so group-scoped rules match. Fail-closed:
any AVP error, missing store, or non-ALLOW decision denies.

Back-compat: when ``TRIGGER_POLICY_STORE_ID`` is unset (AVP store not deployed,
the toggle off, or unit tests), ``is_authorized`` returns a decision whose
``allow`` is ``None`` — the SIGNAL to the caller (router.check_authorization) to
fall back to the legacy ``authorization.users`` allowlist. This is what keeps the
whole spine dark until an operator flips ``EnableTriggerAuthz`` on: with the store
unset, dispatch behavior is byte-for-byte today's.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Cedar entity-type namespace + names — must match the SdlcTrigger policy-store
# schema and the templates in infra/foundation/template.yaml.
CEDAR_NAMESPACE = "SdlcTrigger"
_USER_TYPE = f"{CEDAR_NAMESPACE}::User"
_GROUP_TYPE = f"{CEDAR_NAMESPACE}::Group"
_ACTION_TYPE = f"{CEDAR_NAMESPACE}::Action"
_AGENT_TYPE = f"{CEDAR_NAMESPACE}::Agent"
ACTION_TRIGGER = "Trigger"

# The env var carrying the store id. Unset ⇒ back-compat (legacy allowlist).
STORE_ENV = "TRIGGER_POLICY_STORE_ID"

_avp = None


def _avp_client():
    global _avp
    if _avp is None:
        import boto3

        _avp = boto3.client("verifiedpermissions")
    return _avp


@dataclass(frozen=True)
class Decision:
    """The outcome of a trigger-authz check.

    ``allow`` is a tri-state:
      - ``True``  — AVP returned ALLOW.
      - ``False`` — AVP returned a non-ALLOW decision, OR the call failed
        (fail-closed). ``reason`` names why for the reject notice + metrics.
      - ``None``  — the store is not configured; the caller MUST fall back to the
        legacy allowlist. NOT a deny — the Cedar path simply isn't active.
    """

    allow: bool | None
    reason: str = ""


def store_id() -> str | None:
    """The configured trigger policy-store id, or None when unset/blank."""
    return os.environ.get(STORE_ENV) or None


def _reason_from(determining_policies: list | None) -> str:
    """A short, human-facing reason derived from the AVP decision's determining
    policies. AVP returns the ids of the policies that drove the decision; the
    admin API names each rule's linked policy after the rule, so the id is a
    usable breadcrumb for the dashboard + the in-thread reject notice. Falls back
    to a generic reason when AVP returns none (a pure default-deny)."""
    if not determining_policies:
        return "no-matching-rule"
    ids = [p.get("policyId", "") for p in determining_policies if isinstance(p, dict)]
    ids = [i for i in ids if i]
    return f"policy:{ids[0]}" if ids else "no-matching-rule"


def is_authorized(
    *,
    principal: str,
    agent_id: str,
    source: str,
    context: dict | None = None,
) -> Decision:
    """Decide whether ``principal`` may trigger ``agent_id`` from ``source``.

    ``context`` carries the request dimensions the Cedar rules condition on:
      - ``workspace`` — the Slack team id (``""`` for github/asana).
      - ``channel_id`` — the Slack channel id (``""`` when absent).
      - ``requester_email`` — optional; passed as the principal's ``email``
        attribute so email-based rules match.
      - ``principal_groups`` — optional list of group ids the principal belongs
        to; passed as Cedar parent entities so group-scoped rules match.

    Returns a Decision. ``allow=None`` when the store is unset (caller falls back
    to the legacy allowlist). Fail-closed otherwise: any error ⇒ ``allow=False``.
    """
    sid = store_id()
    if not sid:
        # Store not deployed / toggle off / unit test — signal legacy fallback.
        return Decision(allow=None)

    ctx = context or {}
    workspace = str(ctx.get("workspace", "") or "")
    channel = str(ctx.get("channel_id", "") or "")
    email = ctx.get("requester_email")
    groups = ctx.get("principal_groups") or []

    principal_entity = {"identifier": {"entityType": _USER_TYPE, "entityId": principal}}
    principal_entity["parents"] = [
        {"entityType": _GROUP_TYPE, "entityId": g} for g in sorted(set(groups))
    ]
    if email:
        # Cedar record attribute; the schema declares User.email as optional.
        principal_entity["attributes"] = {"email": {"string": str(email)}}

    try:
        resp = _avp_client().is_authorized(
            policyStoreId=sid,
            principal={"entityType": _USER_TYPE, "entityId": principal},
            action={"actionType": _ACTION_TYPE, "actionId": ACTION_TRIGGER},
            resource={"entityType": _AGENT_TYPE, "entityId": agent_id},
            context={
                "contextMap": {
                    "workspace": {"string": workspace},
                    "channel": {"string": channel},
                    "source": {"string": source},
                }
            },
            entities={"entityList": [principal_entity]},
        )
    except Exception:  # noqa: BLE001
        # Never fail open: an AVP outage / permission error denies the trigger
        # (the fleet stays secure, the sender retries) — mirrors auth._authorize.
        logger.exception(
            "AVP trigger authorization failed (principal=%s agent=%s source=%s); denying",
            principal,
            agent_id,
            source,
        )
        return Decision(allow=False, reason="authz-unavailable")

    decision = resp.get("decision")
    if decision != "ALLOW":
        reason = _reason_from(resp.get("determiningPolicies"))
        logger.warning(
            "Trigger denied by AVP: principal=%s agent=%s source=%s workspace=%s channel=%s (%s)",
            principal,
            agent_id,
            source,
            workspace,
            channel,
            reason,
        )
        return Decision(allow=False, reason=reason)
    return Decision(allow=True)
