"""Push the fleet Cedar policy to the AgentCore Gateway policy engine.

This is the runtime (non-CloudFormation) half of the WS5 boundary: when an admin
changes the repo allowlist, the admin API regenerates ONE named, fleet-owned
Cedar policy (fleet_policy.render_fleet_policy) and overwrites it on the live
policy engine via ``bedrock-agentcore-control``. The static scaffold (engine,
gateway, targets, the initial policy) is CloudFormation; only these edits are
API calls — the same config-vs-CFN split WS1 chose for the DynamoDB config.

Design:
  - Single fleet policy, addressed by name (``FLEET_POLICY_NAME``). We find it by
    name in ListPolicies, then UpdatePolicy, or CreatePolicy if absent — so the
    engine's policy set stays deterministic (no orphaned per-repo policies).
  - CreatePolicy/UpdatePolicy are asynchronous (HTTP 202); we poll GetPolicy
    until the policy reaches ACTIVE (or LOG_ONLY, per rollout) and raise
    PolicySyncError on a *_FAILED status, a Cedar-analysis rejection, or timeout.
  - EnforcementMode is env-driven (``FLEET_POLICY_ENFORCEMENT``, default
    LOG_ONLY) so an operator rolls out log-only first, watches CloudWatch, then
    flips to ACTIVE by redeploying the env — without a code change.
  - No-op when ``POLICY_ENGINE_ID`` is unset (gateway not yet provisioned): the
    admin API still manages the dispatch allowlist; the tool-call policy simply
    isn't enforced until the gateway lands. Logged, not raised.

Raises PolicySyncError (imported from admin) on any real failure so the admin
handler leaves the repo row ``pending`` and reports the inconsistency.
"""

import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import config_store
import fleet_policy

logger = logging.getLogger(__name__)

FLEET_POLICY_NAME = os.environ.get("FLEET_POLICY_NAME", "sdlc_allowed_repos")

# Enforcement (LOG_ONLY vs enforce) is controlled ONCE, at the gateway→engine
# attachment (template's PolicyEngineConfiguration.Mode, from the
# GatewayPolicyEnforcement parameter) — NOT per policy. CreatePolicy/UpdatePolicy
# in the deployed SDK don't accept an enforcementMode parameter, and a per-policy
# mode would be a second, conflicting control anyway.
_TERMINAL_OK = {"ACTIVE"}
_TERMINAL_FAIL = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
_POLL_ATTEMPTS = 30
_POLL_SLEEP_SECONDS = 2

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore-control")
    return _client


def _find_policy_id(
    client, engine_id: str, name: str = FLEET_POLICY_NAME
) -> str | None:
    """Return the id of the policy called ``name``, or None if absent."""
    return _list_policy_ids(client, engine_id).get(name)


def _list_policy_ids(client, engine_id: str) -> dict[str, str]:
    """One full pagination of the engine's policies → ``{name: policy_id}``.
    Shared by the upsert lookups and the stale-permit scan so a sync makes ONE
    listing pass, not one per policy (which is quadratic in policy count and
    invites control-plane throttling as the fleet grows)."""
    ids: dict[str, str] = {}
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(policyEngineId=engine_id):
        for policy in page.get("policies", []):
            name = policy.get("name")
            pid = policy.get("policyId") or policy.get("id")
            if name and pid:
                ids[name] = pid
    return ids


def _poll_until_ready(client, engine_id: str, policy_id: str) -> str:
    """Poll GetPolicy until a terminal status. Raises on failure/timeout."""
    from admin import PolicySyncError

    for _ in range(_POLL_ATTEMPTS):
        resp = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = resp.get("status", "")
        if status in _TERMINAL_OK:
            return status
        if status in _TERMINAL_FAIL:
            reasons = resp.get("statusReasons") or resp.get("failureReason") or ""
            raise PolicySyncError(
                f"fleet policy {policy_id} reached {status}: {reasons}"
            )
        time.sleep(_POLL_SLEEP_SECONDS)
    raise PolicySyncError(
        f"fleet policy {policy_id} did not reach a ready state within "
        f"{_POLL_ATTEMPTS * _POLL_SLEEP_SECONDS}s"
    )


def _upsert_policy(
    client, engine_id: str, name: str, statement: str,
    known_ids: dict[str, str] | None = None,
) -> str:
    """Create-or-update the named policy with ``statement``, then poll to ready.
    Returns the policy id. Raises PolicySyncError (via _poll_until_ready) or the
    underlying ClientError on failure — callers wrap those. Enforcement mode is
    NOT set here — it lives on the gateway→engine attachment.

    ``known_ids`` (a name→id map from one _list_policy_ids pass) avoids a fresh
    full listing per policy when the caller upserts several in one sync.

    Concurrency: an update is last-writer-wins but always writes the FULL current
    statement (not a diff), so a re-sync converges. The one non-convergent race
    is create/create — two writers both find no policy and both create the same
    name; we treat a create conflict as "someone created it first" and adopt +
    update the existing one instead of leaving a duplicate."""
    definition = {"cedar": {"statement": statement}}
    if known_ids is not None:
        policy_id = known_ids.get(name)
    else:
        policy_id = _find_policy_id(client, engine_id, name)
    if policy_id is None:
        try:
            resp = client.create_policy(
                policyEngineId=engine_id,
                name=name,
                definition=definition,
            )
            policy_id = resp.get("policyId") or resp.get("id")
            logger.info("Created policy %s (%s)", name, policy_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ConflictException", "ResourceConflictException"):
                raise
            policy_id = _find_policy_id(client, engine_id, name)
            if policy_id is None:
                raise
            client.update_policy(
                policyEngineId=engine_id,
                policyId=policy_id,
                definition=definition,
            )
            logger.info("Adopted+updated policy %s after create conflict", name)
    else:
        client.update_policy(
            policyEngineId=engine_id,
            policyId=policy_id,
            definition=definition,
        )
        logger.info("Updated policy %s (%s)", name, policy_id)
    _poll_until_ready(client, engine_id, policy_id)
    return policy_id


def sync_fleet_policy() -> None:
    """Push the fleet repo-allowlist policy from the current allowed set, then the
    per-agent permit policies.

    No-op (logged) when POLICY_ENGINE_ID is unset. Raises PolicySyncError on any
    failure reaching the engine. Enforcement (LOG_ONLY vs enforce) is set on the
    gateway→engine attachment, not here.
    """
    from admin import PolicySyncError

    engine_id = os.environ.get("POLICY_ENGINE_ID", "").strip()
    gateway_arn = os.environ.get("FLEET_GATEWAY_ARN", "").strip()
    account_id = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    allowed = config_store.allowed_repos()
    if not engine_id or not gateway_arn:
        # Cedar policies require the concrete gateway ARN as their resource
        # (wildcards are rejected), so we can't render them without it. Absent the
        # gateway, only the dispatch allowlist applies.
        logger.info(
            "POLICY_ENGINE_ID/FLEET_GATEWAY_ARN unset — skipping Gateway policy "
            "sync (allowed=%s)",
            allowed,
        )
        return

    client = _get_client()
    try:
        # ONE listing pass, reused by every upsert lookup and the stale-permit
        # scan — a per-policy listing would be quadratic in policy count.
        known_ids = _list_policy_ids(client, engine_id)
        for name, statement in fleet_policy.render_fleet_policies(
            allowed, gateway_arn
        ).items():
            _upsert_policy(client, engine_id, name, statement, known_ids=known_ids)
        _sync_agent_permits(client, engine_id, account_id, gateway_arn, known_ids)
    except (ClientError, BotoCoreError) as exc:
        # A Cedar-analysis rejection surfaces here (validation is at create/update
        # time); treat every control-plane error as a sync failure.
        raise PolicySyncError(f"gateway policy write failed: {exc}") from exc

    logger.info("Fleet policy set synced (allowed=%s)", allowed)


def _grants_by_agent() -> tuple[dict[str, list[str]], bool]:
    """Build the agent → tool-grant map that drives the permit policies (spec
    §3.5). DATA-DRIVEN: a **custom** agent contributes its row's ``tool_grants``;
    a **built-in** agent keeps its fixed, code-defined ``AGENT_TOOL_GRANTS`` list
    (its row carries no tool_grants — config is locked), so system agents can't be
    silently narrowed/widened by a row edit. Only enabled, non-disabled agents get
    a permit; a disabled/empty-grant agent is omitted (default-deny → no tools).

    Returns ``(grants, complete)``: ``complete`` is False when the config store
    couldn't be read — the caller then still upserts the (built-in default)
    permits but MUST NOT run the stale-permit deletion pass, because with the
    custom rows unreadable every custom agent's permit would look stale and a
    transient DynamoDB error would strip all custom agents of tool access."""
    grants = dict(fleet_policy.AGENT_TOOL_GRANTS)  # built-in defaults
    try:
        caps = config_store.list_capabilities()
    except Exception:  # noqa: BLE001 — degraded read; keep built-in defaults
        logger.exception("could not read capabilities for tool grants; using built-in defaults")
        return grants, False
    for cap in caps:
        agent_id = cap.get("agent_id", "")
        if not agent_id:
            continue
        if not cap.get("enabled") or cap.get("status") in (
            config_store.CAP_DISABLED,
            config_store.CAP_DELETING,
        ):
            grants.pop(agent_id, None)  # disabled/deleting → no permit
            continue
        if cap.get("builtin"):
            continue  # built-in keeps its fixed code-defined grants
        if cap.get("review_status") == "pending_review":
            # The row's grants are UNAPPROVED (an edit may have just widened
            # them, spec §7.5) — rendering them would put a never-approved
            # permit on the Gateway the next time ANY sync runs. Fail closed:
            # no permit until a second admin approves. (Approve re-syncs.)
            grants.pop(agent_id, None)
            continue
        grants[agent_id] = list(cap.get("tool_grants") or [])
    return grants, True


_AGENT_PERMIT_PREFIX = "sdlc_permit_"


def _sync_agent_permits(
    client, engine_id: str, account_id: str, gateway_arn: str,
    known_ids: dict[str, str] | None = None,
) -> None:
    """Provision one permit policy per agent (default-deny means the fleet does
    nothing without them), and DELETE any stale ``sdlc_permit_*`` policy whose
    agent no longer has a grant (disabled, deleting, deleted, or grants cleared) —
    an orphaned permit would keep a torn-down agent's role authorized on the
    Gateway indefinitely. Skipped when the account is unknown — the forbid
    policies still apply, but under ENFORCE nothing is permitted until the permits
    land, which is why rollout is LOG_ONLY first.

    Grants are DATA-DRIVEN (`_grants_by_agent`): custom agents' authored
    ``tool_grants`` and the built-ins' fixed lists render through one path."""
    if not account_id:
        logger.info("AWS_ACCOUNT_ID unset — skipping per-agent permits")
        return
    if known_ids is None:
        known_ids = _list_policy_ids(client, engine_id)
    grants, complete = _grants_by_agent()
    desired = fleet_policy.agent_permit_policies(account_id, gateway_arn, grants)
    for name, statement in desired.items():
        _upsert_policy(client, engine_id, name, statement, known_ids=known_ids)
    # Retract permits for agents that no longer have one. Only our own
    # namespaced policies are ever considered — the fleet forbids and any
    # foreign policy on the engine are untouched. Skipped entirely on a
    # degraded config read: with the custom rows unreadable every custom
    # permit would look stale, and a transient DynamoDB error must never strip
    # running custom agents of tool access.
    if not complete:
        logger.warning("config read degraded — skipping stale-permit deletion this sync")
        return
    for name, policy_id in known_ids.items():
        if not name.startswith(_AGENT_PERMIT_PREFIX) or name in desired:
            continue
        client.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        logger.info("Deleted stale agent permit %s (%s)", name, policy_id)
