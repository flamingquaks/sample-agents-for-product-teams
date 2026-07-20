"""Project admin-authored trigger rules into the AVP ``SdlcTrigger`` policy store.

This is the management half of the router's trigger-authz decision (spec §5.5):
the admin API writes a ``trigger_rule`` row (config_store), and this module
renders it to a Cedar policy and pushes it to Amazon Verified Permissions, where
the Dispatch Router's ``trigger_authz.is_authorized`` evaluates it.

Relationship to policy_sync.py (the Gateway tool-call policy):
  - policy_sync targets the AgentCore Gateway engine (bedrock-agentcore-control),
    whose CreatePolicy/UpdatePolicy are asynchronous (202 + poll to ACTIVE).
  - THIS targets AVP (verifiedpermissions), whose CreatePolicy is synchronous and
    validates Cedar at create time (the store is STRICT) — so no poll loop, and a
    malformed statement / schema mismatch raises ValidationException immediately.

Why STATIC policies, not template-linked: an AVP template-linked policy can only
substitute ``?principal`` and ``?resource``. A trigger rule also pins the
WORKSPACE and CHANNEL set as ``when`` conditions, which a template can't carry
per-instance — so each rule becomes one static policy whose full statement is
rendered from the row. (The CFN templates in the spec remain a convenience for
hand-authored rules; the admin path renders static policies.)

Rollback invariant (spec §5.5) is owned by the admin API caller: on CREATE it
persists the row then calls ``project_rule`` and rolls the row back if projection
raises; on DELETE it calls ``revoke_rule`` (AVP first) then removes the row. This
module provides the primitives + the pure renderer; it never deletes a row.

No-op (logged, returns None) when ``TRIGGER_POLICY_STORE_ID`` is unset — the
admin can author rows before the store is provisioned; they project once it lands.
"""

import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import config_store

logger = logging.getLogger(__name__)

# Must match trigger_authz.CEDAR_NAMESPACE (router side) and the store schema.
CEDAR_NAMESPACE = "SdlcTrigger"
STORE_ENV = "TRIGGER_POLICY_STORE_ID"

_client = None


class TriggerPolicySyncError(Exception):
    """Raised when a rule can't be projected to / revoked from AVP. The admin API
    catches this to honor the persist↔project rollback invariant."""


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("verifiedpermissions")
    return _client


def store_id() -> str | None:
    return os.environ.get(STORE_ENV) or None


def _cedar_str(value: str) -> str:
    r"""A Cedar string literal for ``value`` with the two chars that could break
    out of a ``"..."`` literal escaped: backslash and double-quote. Ids that reach
    here are already shape-validated at the config_store boundary (Slack ids,
    agent_id), but a group name / email subject_id is free-form, so escape
    defensively rather than trust the caller."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_rule_statement(rule: dict) -> str:
    """Render a ``trigger_rule`` row to a single Cedar policy statement.

    Scope:
      - principal: ``== SdlcTrigger::User::"<id>"`` for a user rule, or
        ``in SdlcTrigger::Group::"<id>"`` for a group rule (the router passes the
        principal's groups as parent entities, so ``in`` matches membership).
      - action: always ``== SdlcTrigger::Action::"Trigger"``.
      - resource: ``== SdlcTrigger::Agent::"<id>"``, or unconstrained when the
        rule's agent is the ``"*"`` wildcard.
    Conditions (omitted when the field is the ``*`` wildcard):
      - workspace: ``context.workspace == "<team>"``.
      - channels: ``["C…","C…"].contains(context.channel)``.
    Effect is ``permit`` or ``forbid`` (Cedar forbid-wins, so a forbid rule beats
    any permit).
    """
    effect = rule.get("effect", config_store.RULE_PERMIT)
    if effect not in config_store.RULE_EFFECTS:
        raise TriggerPolicySyncError(f"invalid rule effect {effect!r}")

    subject_type = rule.get("subject_type")
    subject_id = rule.get("subject_id", "")
    if subject_type == config_store.RULE_SUBJECT_GROUP:
        principal = f"principal in {CEDAR_NAMESPACE}::Group::{_cedar_str(subject_id)}"
    else:
        principal = f"principal == {CEDAR_NAMESPACE}::User::{_cedar_str(subject_id)}"

    agent_id = rule.get("agent_id", "*")
    if agent_id == "*":
        resource = "resource"
    else:
        resource = f"resource == {CEDAR_NAMESPACE}::Agent::{_cedar_str(agent_id)}"

    head = (
        f"{effect}(\n"
        f"  {principal},\n"
        f"  action == {CEDAR_NAMESPACE}::Action::\"Trigger\",\n"
        f"  {resource}\n"
        f")"
    )

    conditions = []
    workspace = rule.get("workspace", "*")
    if workspace and workspace != "*":
        conditions.append(f"context.workspace == {_cedar_str(workspace)}")
    channels = rule.get("channels") or ["*"]
    if channels != ["*"]:
        listed = ", ".join(_cedar_str(c) for c in channels)
        conditions.append(f"[{listed}].contains(context.channel)")

    if conditions:
        return head + " when {\n  " + " &&\n  ".join(conditions) + "\n};"
    return head + ";"


def project_rule(rule: dict) -> str | None:
    """Create the AVP static policy for ``rule`` and return its policy id.

    If the rule already carries an ``avp_policy_id`` (a REPLACE), the stale policy
    is deleted first so a rule maps to exactly one live policy — AVP has no
    "update a static policy's statement" that changes scope safely, and a
    delete+create keeps the store free of orphans. Stamps the new id back onto the
    row via config_store.set_trigger_rule_policy_id.

    No-op (returns None) when the store is unconfigured. Raises
    TriggerPolicySyncError on any AVP failure (incl. a Cedar-validation reject) so
    the admin caller can roll back the row.
    """
    sid = store_id()
    if not sid:
        logger.info(
            "%s unset — skipping AVP projection for rule %s",
            STORE_ENV,
            rule.get("rule_id"),
        )
        return None

    statement = render_rule_statement(rule)
    client = _get_client()
    rule_id = rule.get("rule_id", "")

    old_policy_id = rule.get("avp_policy_id")
    try:
        resp = client.create_policy(
            policyStoreId=sid,
            definition={
                "static": {
                    "description": f"trigger rule {rule_id} ({rule.get('connector')})",
                    "statement": statement,
                }
            },
        )
    except (ClientError, BotoCoreError) as exc:
        raise TriggerPolicySyncError(
            f"AVP CreatePolicy failed for rule {rule_id}: {exc}"
        ) from exc

    policy_id = resp.get("policyId")
    config_store.set_trigger_rule_policy_id(rule_id, policy_id)

    # Best-effort cleanup of the superseded policy on a replace. A failure here
    # leaves a stale (but harmless — same rule) policy; log rather than fail the
    # projection, since the new policy is already live + linked.
    if old_policy_id and old_policy_id != policy_id:
        try:
            client.delete_policy(policyStoreId=sid, policyId=old_policy_id)
        except (ClientError, BotoCoreError):
            logger.warning(
                "Could not delete superseded AVP policy %s for rule %s",
                old_policy_id,
                rule_id,
            )
    return policy_id


def revoke_rule(rule: dict) -> None:
    """Delete the rule's AVP policy (if any) and clear its linkage. No-op when the
    store is unconfigured or the rule was never projected. Raises
    TriggerPolicySyncError on an AVP failure so the admin caller does NOT remove
    the row while Cedar still enforces it (delete direction of the invariant)."""
    sid = store_id()
    policy_id = rule.get("avp_policy_id")
    if not sid or not policy_id:
        return
    client = _get_client()
    try:
        client.delete_policy(policyStoreId=sid, policyId=policy_id)
    except client.exceptions.ResourceNotFoundException:
        # Already gone — treat as revoked so a retry converges.
        logger.info("AVP policy %s already absent; treating as revoked", policy_id)
    except (ClientError, BotoCoreError) as exc:
        raise TriggerPolicySyncError(
            f"AVP DeletePolicy failed for rule {rule.get('rule_id')}: {exc}"
        ) from exc
    config_store.set_trigger_rule_policy_id(rule.get("rule_id", ""), None)
