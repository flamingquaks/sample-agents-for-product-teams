"""Trigger authorization for the Dispatch Router — Cedar via Amazon Verified
Permissions (AVP), data-driven.

This is the THIRD Cedar surface in the fleet, and the one the router consults on
every inbound dispatch (decision "C" in docs/specs/slack-connectors-spec.md §2):

  - the dashboard API's authz (infra/dashboard/auth.py) decides who may read/write
    the dashboard,
  - the AgentCore Gateway policy engine decides which tool calls an agent may make,
  - and THIS module decides whether a given *sender* may *trigger* a given *agent*
    from a given *source / workspace / channel*.

Data-driven model (spec §5). The AVP policy store holds a SMALL, FIXED set of
Cedar policies (authored once, in the foundation template — see FIXED_POLICIES
below). Admin-authored grants are DATA, not policies: the router reads the
``trigger_rule`` + ``slack_channel`` rows (via trigger_grants) and passes the
resolved grant sets to AVP as ENTITY ATTRIBUTES on the ``Agent`` resource, plus
``context.channelAllowed``. So granting a user access is a DynamoDB write, and
the AVP policy count stays constant no matter how many users/rules exist — a
policy-per-user would be the AVP anti-pattern this deliberately avoids.

The fixed policies:
  P1 permit — principal is in the agent's allowed principals, OR a member of one
     of its allowed groups.
  P2 forbid — principal is in the agent's denied principals, OR a member of a
     denied group (forbid-wins gives explicit deny precedence over any permit).
  P3 forbid — the channel isn't allowed for the workspace (context.channelAllowed
     is false); the posture math is resolved in trigger_grants, not Cedar.
Default-deny: no allowed-membership ⇒ no permit ⇒ deny.

This is the fleet's ONLY trigger-authorization mechanism — no per-capability
allowlist. Every source authorizes here; the principal namespaces the source
(``github:<login>``, ``asana:<gid>``, ``slack:<team>:<uid>``) and github/asana
carry no workspace/channel (channel axis treated as allowed for them).

Fail-closed: any AVP error, a missing store, or a non-ALLOW decision denies. The
store is required infrastructure (provisioned alongside the dashboard's own AVP
store); an unset store is a deployment error and denies every trigger.
"""

import logging
import os
from dataclasses import dataclass

import trigger_grants

logger = logging.getLogger(__name__)

# The env var carrying the store id. Required — an unset store fails closed.
STORE_ENV = "TRIGGER_POLICY_STORE_ID"

# Cedar entity-type namespace + names — must match the SdlcTrigger policy-store
# schema and the fixed policies in infra/foundation/template.yaml.
CEDAR_NAMESPACE = "SdlcTrigger"
_USER_TYPE = f"{CEDAR_NAMESPACE}::User"
_GROUP_TYPE = f"{CEDAR_NAMESPACE}::Group"
_ACTION_TYPE = f"{CEDAR_NAMESPACE}::Action"
_AGENT_TYPE = f"{CEDAR_NAMESPACE}::Agent"
ACTION_TRIGGER = "Trigger"

# The FIXED policy set the SdlcTrigger store is provisioned with (spec §5.2).
# Authored ONCE in the foundation template; kept here as the source of truth +
# for the docs/tests. Grants are data (entity attrs), so these never change as
# users/rules are added. Cedar forbid-wins gives P2/P3 precedence over P1.
FIXED_POLICIES = {
    "sdlc_trigger_permit": (
        "permit(\n"
        "  principal,\n"
        '  action == SdlcTrigger::Action::"Trigger",\n'
        "  resource\n"
        ") when {\n"
        "  resource.allowedPrincipals.contains(principal) ||\n"
        "  principal.groups.containsAny(resource.allowedGroups)\n"
        "};"
    ),
    "sdlc_trigger_forbid_denied": (
        "forbid(\n"
        "  principal,\n"
        '  action == SdlcTrigger::Action::"Trigger",\n'
        "  resource\n"
        ") when {\n"
        "  resource.deniedPrincipals.contains(principal) ||\n"
        "  principal.groups.containsAny(resource.deniedGroups)\n"
        "};"
    ),
    "sdlc_trigger_forbid_channel": (
        "forbid(\n"
        "  principal,\n"
        '  action == SdlcTrigger::Action::"Trigger",\n'
        "  resource\n"
        ") when { !context.channelAllowed };"
    ),
}

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

    ``allow`` is a plain bool: True on ALLOW; False (fail-closed) on any
    non-ALLOW, an AVP error, or an unconfigured store. ``reason`` names which,
    for the reject notice + metrics.
    """

    allow: bool
    reason: str = ""


def store_id() -> str | None:
    """The configured trigger policy-store id, or None when unset/blank."""
    return os.environ.get(STORE_ENV) or None


def _set(values) -> dict:
    """A Cedar set-of-strings attribute value for AVP's entity/attribute JSON."""
    return {"set": [{"string": v} for v in values]}


def _reason_from(determining_policies: list | None) -> str:
    """A short reason from the AVP decision's determining policies. For a DENY the
    determining policy is one of the two fixed forbids (denied subject / blocked
    channel) or empty (default-deny = no matching permit)."""
    if not determining_policies:
        return "no-matching-grant"
    ids = [p.get("policyId", "") for p in determining_policies if isinstance(p, dict)]
    ids = [i for i in ids if i]
    return f"policy:{ids[0]}" if ids else "no-matching-grant"


def is_authorized(
    *,
    principal: str,
    agent_id: str,
    source: str,
    context: dict | None = None,
) -> Decision:
    """Decide whether ``principal`` may trigger ``agent_id`` from ``source``.

    ``context`` carries the request dimensions:
      - ``workspace`` — the Slack team id ("" for github/asana).
      - ``channel_id`` — the Slack channel id ("" when absent).
      - ``requester_email`` — optional; passed as the principal's ``email`` attr.
      - ``principal_groups`` — the group ids the principal belongs to (from the
        receiver/enrichment); passed as the principal's ``groups`` attribute so
        the fixed group-membership policies match.

    The agent's grant sets (allowed/denied principals + groups) and the channel
    posture are read from DynamoDB (trigger_grants) and passed to AVP as entity
    attributes + context — the grants are DATA, the policy set is fixed.

    Fail-closed everywhere: an unset store, any AVP error, or a non-ALLOW
    decision all yield ``allow=False`` with a naming ``reason``.
    """
    sid = store_id()
    if not sid:
        logger.error(
            "%s is unset — trigger authorization store not configured; denying "
            "(principal=%s agent=%s source=%s)",
            STORE_ENV,
            principal,
            agent_id,
            source,
        )
        return Decision(allow=False, reason="authz-store-unconfigured")

    ctx = context or {}
    workspace = str(ctx.get("workspace", "") or "")
    channel = str(ctx.get("channel_id", "") or "")
    email = ctx.get("requester_email")
    groups = ctx.get("principal_groups") or []

    # Resolve the DATA: the agent's grant sets + whether this channel is allowed.
    # A read error here must not fail open — trigger_grants reads through a cache
    # and any exception propagates to the except below (deny).
    try:
        grants = trigger_grants.agent_grants(agent_id, workspace)
        channel_allowed = trigger_grants.channel_allowed(workspace, channel)
    except Exception:  # noqa: BLE001
        logger.exception(
            "trigger-grant lookup failed (agent=%s workspace=%s); denying", agent_id, workspace
        )
        return Decision(allow=False, reason="authz-unavailable")

    principal_entity = {
        "identifier": {"entityType": _USER_TYPE, "entityId": principal},
        "parents": [
            {"entityType": _GROUP_TYPE, "entityId": g} for g in sorted(set(groups))
        ],
        "attributes": {"groups": _set(sorted(set(groups)))},
    }
    if email:
        principal_entity["attributes"]["email"] = {"string": str(email)}

    agent_entity = {
        "identifier": {"entityType": _AGENT_TYPE, "entityId": agent_id},
        "attributes": {
            "allowedPrincipals": _set(grants.allowed_principals),
            "deniedPrincipals": _set(grants.denied_principals),
            "allowedGroups": _set(grants.allowed_groups),
            "deniedGroups": _set(grants.denied_groups),
        },
    }

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
                    "channelAllowed": {"boolean": bool(channel_allowed)},
                }
            },
            entities={"entityList": [principal_entity, agent_entity]},
        )
    except Exception:  # noqa: BLE001
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
