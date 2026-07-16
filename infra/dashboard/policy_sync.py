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
# LOG_ONLY | ACTIVE — per-policy enforcement (NOT the gateway-attachment enum).
_DEFAULT_ENFORCEMENT = "LOG_ONLY"

_TERMINAL_OK = {"ACTIVE", "LOG_ONLY"}
_TERMINAL_FAIL = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
_POLL_ATTEMPTS = 30
_POLL_SLEEP_SECONDS = 2

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore-control")
    return _client


def _enforcement_mode() -> str:
    mode = os.environ.get("FLEET_POLICY_ENFORCEMENT", _DEFAULT_ENFORCEMENT).upper()
    return mode if mode in {"ACTIVE", "LOG_ONLY"} else _DEFAULT_ENFORCEMENT


def _find_policy_id(client, engine_id: str) -> str | None:
    """Return the fleet policy's id by name, or None if it doesn't exist yet."""
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(policyEngineId=engine_id):
        for policy in page.get("policies", []):
            if policy.get("name") == FLEET_POLICY_NAME:
                return policy.get("policyId") or policy.get("id")
    return None


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


def sync_fleet_policy() -> None:
    """Regenerate + push the fleet Cedar policy from the current allowed set.

    No-op (logged) when POLICY_ENGINE_ID is unset. Raises PolicySyncError on any
    failure reaching the engine or the intended enforcement state.
    """
    from admin import PolicySyncError

    engine_id = os.environ.get("POLICY_ENGINE_ID", "").strip()
    allowed = config_store.allowed_repos()
    if not engine_id:
        logger.info(
            "POLICY_ENGINE_ID unset — skipping Gateway policy sync (allowed=%s)",
            allowed,
        )
        return

    statement = fleet_policy.render_fleet_policy(allowed)
    mode = _enforcement_mode()
    definition = {"cedar": {"statement": statement}}
    client = _get_client()

    # Concurrency note: two admins editing at once both read the live table
    # (allowed_repos above), render the FULL current allowlist, and overwrite the
    # single named policy — so an update is last-writer-wins but always reflects
    # the whole current table, not a partial diff, and a re-sync converges. The
    # one non-convergent race is a create/create: both find no policy and both
    # create the same name, leaving a duplicate. We guard that by treating a
    # create conflict as "someone created it first" and falling back to update.
    try:
        policy_id = _find_policy_id(client, engine_id)
        if policy_id is None:
            policy_id = _create_or_adopt(client, engine_id, definition, mode)
        else:
            client.update_policy(
                policyEngineId=engine_id,
                policyId=policy_id,
                definition=definition,
                enforcementMode=mode,
            )
            logger.info("Updated fleet policy %s (mode=%s)", policy_id, mode)
    except (ClientError, BotoCoreError) as exc:
        # A Cedar-analysis rejection surfaces here (validation is at create/update
        # time); treat every control-plane error as a sync failure.
        raise PolicySyncError(f"gateway policy write failed: {exc}") from exc

    status = _poll_until_ready(client, engine_id, policy_id)
    logger.info("Fleet policy %s synced to %s (allowed=%s)", policy_id, status, allowed)


def _create_or_adopt(client, engine_id: str, definition: dict, mode: str) -> str:
    """Create the fleet policy, or adopt+update the existing one if a concurrent
    writer created it first (create/create race). Returns the policy id."""
    try:
        resp = client.create_policy(
            policyEngineId=engine_id,
            name=FLEET_POLICY_NAME,
            definition=definition,
            enforcementMode=mode,
        )
        policy_id = resp.get("policyId") or resp.get("id")
        logger.info("Created fleet policy %s (mode=%s)", policy_id, mode)
        return policy_id
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("ConflictException", "ResourceConflictException"):
            raise
        # Lost the create race — re-find the policy the other writer made and
        # update it so our allowlist snapshot is applied (idempotent name).
        policy_id = _find_policy_id(client, engine_id)
        if policy_id is None:
            raise
        client.update_policy(
            policyEngineId=engine_id,
            policyId=policy_id,
            definition=definition,
            enforcementMode=mode,
        )
        logger.info("Adopted+updated fleet policy %s after create conflict", policy_id)
        return policy_id
