"""Capability deployer — turns a built agent image into a live AgentCore runtime.

This is the event-driven back half of UI-driven onboarding. The admin API
(admin.py) only *starts a build* (codebuild:StartBuild) and returns immediately —
it never holds the privilege to create IAM roles or runtimes. When the shared
build pipeline finishes, EventBridge invokes THIS Lambda, which:

  1. reads the capability row for the built agent,
  2. ensures the agent's per-agent runtime IAM role exists (path-scoped +
     permissions-boundary'd — see below),
  3. create-or-updates the AgentCore runtime to the freshly built image, with the
     agent's merged env (guardrail + gateway URL + the capability's own env),
  4. waits for READY, then flips the capability to ``active`` and republishes the
     Dispatch Router registry so the agent becomes routable.

Security posture (why the privileged actions live HERE, not on the admin API):
the admin Lambda is Cognito-facing and internet-reachable; giving it
iam:CreateRole / create-agent-runtime would make an authz bug or admin-account
compromise able to mint arbitrary roles/runtimes. Instead those actions are on
THIS function's role, which is invocable ONLY by the CodeBuild-completion event
rule — not by any HTTP path. The role it creates is further constrained by CFN:
  - created only under the fixed path /sdlc-agents/capabilities/,
  - with a mandatory PermissionsBoundary (CapabilityRuntimeBoundary) that caps
    what any such role can ever do, so even a bug here can't grant a runtime more
    than the boundary allows,
  - iam:PassRole scoped to that same path + the bedrock-agentcore service.
See the template's CapabilityDeployerFunction + CapabilityRuntimeBoundary.

Failure handling: a FAILED/STOPPED build, or a runtime that never reaches READY,
marks the capability ``failed`` with a reason and leaves any EXISTING runtime
untouched — a failed rebuild never tears down a working agent (critical for the
weekly security rebuild, Phase 6).
"""

import json
import logging
import os
import re
import time

import boto3

import config_store

# An ECR image tag we'll accept off a build event: alphanumerics, dot, underscore,
# hyphen only — no ":" or "@" or "/" that could repoint the image reference.
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class DeployGuardError(Exception):
    """A safety guard refused the deploy (e.g. a name collision with a foreign,
    non-fleet runtime). Caught by deploy_capability, which marks the capability
    ``failed`` with the reason and leaves all existing resources untouched."""

# Bounded READY poll. AgentCore runtime create/update takes minutes; the Lambda
# timeout (template: 900s) caps the outer bound, this caps our own loop.
_READY_POLL_MAX = 55
_READY_POLL_SLEEP = 10

_agentcore = None
_iam = None


def _acc():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore-control")
    return _agentcore


def _iam_client():
    global _iam
    if _iam is None:
        _iam = boto3.client("iam")
    return _iam


# Fleet-wide runtime env every agent REQUIRES to boot: the prompt-injection
# guardrail (threat T-1/2/3) and the Cedar-enforced tool gateway (the agent
# refuses to start without GATEWAY_MCP_URL). A capability created without any of
# these would either boot with the guardrail silently detached or never reach
# READY — so a missing value is a deploy misconfiguration we surface up front
# rather than passing through. Note GATEWAY_MCP_URL is only wired when the stack
# is deployed with the gateway enabled (DeployGateway=true).
_REQUIRED_BASE_ENV = ("BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "GATEWAY_MCP_URL")

# Fleet-wide runtime env that's injected when present but is NOT boot-critical —
# a missing value degrades gracefully rather than failing the deploy.
# MANTLE_PROJECT_ID tags model usage to the fleet's shared cost-attribution
# project; absent, calls just fall back to the account's default project.
_OPTIONAL_BASE_ENV = ("MANTLE_PROJECT_ID",)


def _base_env() -> tuple[dict[str, str], list[str]]:
    """The fleet-wide runtime env, resolved from THIS function's environment (the
    stack populates it from the same outputs the agents require). Returns
    ``(env, missing)`` where ``missing`` names any REQUIRED key absent/blank so
    the caller can fail fast with an actionable reason instead of a silent
    misconfiguration that only surfaces as a runtime timeout. Optional keys are
    added when present and never reported as missing."""
    env = {}
    missing = []
    for key in _REQUIRED_BASE_ENV:
        val = os.environ.get(key)
        if val:
            env[key] = val
        else:
            missing.append(key)
    for key in _OPTIONAL_BASE_ENV:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env, missing


def _runtime_role_arn(agent_id: str) -> str:
    """The per-agent runtime role ARN. Path-scoped so it falls under the
    CreateRole/PassRole grants (and the permissions boundary) the template
    restricts this function to."""
    account = os.environ["AWS_ACCOUNT_ID"]
    return (
        f"arn:aws:iam::{account}:role/sdlc-agents/capabilities/"
        f"{agent_id}-agentcore-runtime"
    )


def _ensure_runtime_role(agent_id: str) -> str:
    """Create (if absent) the agent's runtime role under the fixed capabilities
    path, with the mandatory permissions boundary, and (re)put its inline policies.
    Idempotent. Returns the role ARN.

    The role's trust + policies mirror what scripts/bootstrap.py::agent_role_policies
    produced, but are inlined here because bootstrap is being retired (Phase 5) and
    this is now the single place runtime roles are made."""
    iam = _iam_client()
    role_name = f"{agent_id}-agentcore-runtime"
    path = "/sdlc-agents/capabilities/"
    boundary = os.environ["RUNTIME_PERMISSIONS_BOUNDARY_ARN"]
    account = os.environ["AWS_ACCOUNT_ID"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    stage = os.environ["STAGE"]

    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam.create_role(
            Path=path,
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            PermissionsBoundary=boundary,
            Description=f"AgentCore runtime role for capability {agent_id}",
        )
        logger.info("created runtime role %s", role_name)
    except iam.exceptions.EntityAlreadyExistsException:
        logger.info("runtime role %s exists — refreshing policies", role_name)

    table_arn = f"arn:aws:dynamodb:{region}:{account}:table/dispatch-assignments-{stage}"
    policies = {
        "cloudwatch-logs": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                }
            ],
        },
        "dynamodb-assignments": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:Query",
                    ],
                    "Resource": [table_arn, f"{table_arn}/index/*"],
                }
            ],
        },
        "ecr-pull": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetAuthorizationToken",
                    ],
                    "Resource": "*",
                }
            ],
        },
        "agentcore-gateway-invoke": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "bedrock-agentcore:InvokeGateway",
                    # Account+region wildcard, NOT a name-prefixed pattern: the
                    # service lowercases the gateway name into the resource id
                    # (sdlcFleet<stage> -> sdlcfleet<stage>-<suffix>) and IAM
                    # matching is case-sensitive, so `gateway/sdlcFleet<stage>*`
                    # would NEVER match the real id and every tool call would be
                    # denied. Matches template.yaml FleetGatewayRole, which uses
                    # `gateway/*` for the same reason.
                    "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*",
                }
            ],
        },
        # Model access via Bedrock Mantle (the fleet's primary path): inference on
        # any project (cost attribution scopes at the project level; inference is
        # account-wide) + short-term bearer-token minting. Plus bedrock:ApplyGuardrail
        # (guardrail applied via Mantle headers) and bedrock:InvokeModel for the ADR
        # agent's Titan embeddings, which stay on the classic bedrock-runtime path.
        "model-access": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-mantle:CreateInference",
                        "bedrock-mantle:GetInference",
                        "bedrock-mantle:ListInferences",
                    ],
                    "Resource": f"arn:aws:bedrock-mantle:{region}:{account}:project/*",
                },
                {
                    "Effect": "Allow",
                    "Action": "bedrock-mantle:CallWithBearerToken",
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:ApplyGuardrail", "bedrock:InvokeModel"],
                    "Resource": "*",
                },
            ],
        },
    }
    for name, doc in policies.items():
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=name,
            PolicyDocument=json.dumps(doc),
        )
    return _runtime_role_arn(agent_id)


# Tag every fleet-managed runtime with this marker so we can tell OUR runtimes
# apart from any other AgentCore runtime in the account that happens to share a
# name. The deployer refuses to UPDATE a same-named runtime that lacks this tag,
# so onboarding a capability whose agent_id collides with a foreign runtime can
# never hijack (repoint image/role/env of) that runtime.
_FLEET_TAG_KEY = "sdlc-fleet"


def _fleet_tag_value() -> str:
    return f"capability-{os.environ['STAGE']}"


def _find_runtime(agent_id: str) -> tuple[str, str]:
    """Find this agent's runtime by name. Returns (runtime_id, runtime_arn), or
    ('','') if none exists yet. Paginates — the account can hold many runtimes."""
    acc = _acc()
    paginator = acc.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == agent_id:
                return rt["agentRuntimeId"], rt.get("agentRuntimeArn", "")
    return "", ""


def _is_fleet_runtime(runtime_arn: str) -> bool:
    """Whether a runtime carries our fleet tag — the guard against updating a
    foreign runtime that merely shares the agent's name."""
    if not runtime_arn:
        return False
    acc = _acc()
    tags = acc.list_tags_for_resource(resourceArn=runtime_arn).get("tags", {})
    return tags.get(_FLEET_TAG_KEY) == _fleet_tag_value()


def _env_csv(env: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in env.items())


def _deploy_runtime(agent_id: str, image_uri: str, role_arn: str, env: dict) -> tuple[str, str]:
    """Create or update the agent's AgentCore runtime to ``image_uri``. Returns
    ``(runtime_id, runtime_arn)`` — the ARN as reported by the service, which is
    the authoritative value written into the registry (never re-synthesized). An
    existing FLEET runtime is UPDATED (its env is replaced wholesale, which is why
    we always pass the full merged env); a new one is CREATED and tagged as
    fleet-managed.

    Raises DeployGuardError if a runtime with this name exists but is NOT
    fleet-tagged — refusing to hijack a foreign runtime that merely shares the
    agent's name (the IAM grant is account-wide, so this app-level check is the
    real scoping)."""
    acc = _acc()
    artifact = {"containerConfiguration": {"containerUri": image_uri}}
    network = {"networkMode": "PUBLIC"}
    env_vars = env  # AgentCore takes a map for the SDK call
    existing_id, existing_arn = _find_runtime(agent_id)
    if existing_id:
        if not _is_fleet_runtime(existing_arn):
            raise DeployGuardError(
                f"a runtime named {agent_id!r} already exists but is not managed by "
                f"this fleet ({_FLEET_TAG_KEY}={_fleet_tag_value()} tag absent) — "
                "refusing to overwrite it; choose a different agent_id"
            )
        logger.info("updating runtime %s (%s)", agent_id, existing_id)
        resp = acc.update_agent_runtime(
            agentRuntimeId=existing_id,
            agentRuntimeArtifact=artifact,
            roleArn=role_arn,
            networkConfiguration=network,
            environmentVariables=env_vars,
        )
        # Prefer the ARN the service returns; fall back to the one we already
        # resolved via _find_runtime if update's response omits it.
        return existing_id, resp.get("agentRuntimeArn") or existing_arn
    logger.info("creating runtime %s", agent_id)
    resp = acc.create_agent_runtime(
        agentRuntimeName=agent_id,
        agentRuntimeArtifact=artifact,
        roleArn=role_arn,
        networkConfiguration=network,
        environmentVariables=env_vars,
        tags={_FLEET_TAG_KEY: _fleet_tag_value()},
    )
    return resp["agentRuntimeId"], resp.get("agentRuntimeArn", "")


def _wait_ready(runtime_id: str) -> tuple[bool, str]:
    """Poll until the runtime is READY. Returns (ok, status). Bounded by the
    Lambda timeout via _READY_POLL_MAX."""
    acc = _acc()
    status = "UNKNOWN"
    for _ in range(_READY_POLL_MAX):
        status = acc.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
        if status == "READY":
            return True, status
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            return False, status
        time.sleep(_READY_POLL_SLEEP)
    return False, f"timeout(last={status})"


def deploy_capability(agent_id: str, image_tag: str) -> None:
    """Bring a freshly built image live for ``agent_id``: ensure the role, deploy
    the runtime, wait READY, mark active + republish. Marks the capability
    ``failed`` (leaving any existing runtime intact) on any error."""
    cap = config_store.get_capability(agent_id)
    if not cap:
        logger.warning("no capability row for %s — build event ignored", agent_id)
        return

    account = os.environ["AWS_ACCOUNT_ID"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    image_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/sdlc-agents/{agent_id}:{image_tag}"

    try:
        base_env, missing = _base_env()
        if missing:
            # Fail fast with a clear reason rather than creating a runtime that
            # boots without the guardrail or never reaches READY without the
            # gateway URL. Left BEFORE any role/runtime mutation.
            detail = (
                "deploy misconfiguration: required runtime env missing "
                f"({', '.join(missing)}). Check the CapabilityDeployer function's "
                "environment / stack outputs (GATEWAY_MCP_URL requires the gateway "
                "to be enabled)."
            )
            config_store.set_capability_status(agent_id, config_store.CAP_FAILED, detail=detail[:900])
            logger.error("%s for capability %s", detail, agent_id)
            return
        role_arn = _ensure_runtime_role(agent_id)
        # A freshly created IAM role isn't always immediately assumable by the
        # service; a brief settle avoids a spurious create-runtime failure.
        time.sleep(8)
        env = config_store.capability_env_pairs(cap, base_env)
        runtime_id, runtime_arn = _deploy_runtime(agent_id, image_uri, role_arn, env)
        ok, status = _wait_ready(runtime_id)
        if not ok:
            config_store.set_capability_status(
                agent_id, config_store.CAP_FAILED, detail=f"runtime not READY: {status}"
            )
            logger.error("runtime for %s did not reach READY: %s", agent_id, status)
            return
        config_store.set_capability_deploy_state(
            agent_id,
            image_tag=image_tag,
            runtime_arn=runtime_arn,
            build_id="",  # cleared; the row already recorded the build that ran
        )
        config_store.set_capability_status(agent_id, config_store.CAP_ACTIVE, detail="ready")
    except Exception as exc:  # noqa: BLE001
        # Never tear down a working runtime on failure — just record why.
        logger.exception("deploy failed for capability %s", agent_id)
        try:
            config_store.set_capability_status(
                agent_id, config_store.CAP_FAILED, detail=f"deploy error: {exc}"[:900]
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not mark %s failed", agent_id)
        return

    # Publish AFTER the capability is committed active — OUTSIDE the try above, so
    # a transient SSM publish error can't flip a runtime that is genuinely up and
    # active to ``failed``. The registry re-publishes on the next capability
    # change, and the router refreshes its cached copy within its TTL meanwhile.
    try:
        config_store.publish_registry()
    except Exception:  # noqa: BLE001
        logger.exception(
            "capability %s is active but registry publish failed; it will "
            "re-publish on the next capability change", agent_id
        )
    logger.info("capability %s active (image %s)", agent_id, image_tag)


def _agent_from_build_event(event: dict) -> tuple[str, str, str] | None:
    """Extract (agent_id, image_tag, build_status) from a CodeBuild state-change
    event. AGENT_NAME + IMAGE_TAG are the build's environment override variables
    (set by StartBuild); the build status is the phase result. Returns None if the
    event isn't a build we recognize."""
    detail = event.get("detail") or {}
    status = detail.get("build-status", "")
    env = detail.get("additional-information", {}).get("environment", {})
    overrides = {
        v.get("name"): v.get("value")
        for v in env.get("environment-variables", [])
        if isinstance(v, dict)
    }
    agent_id = overrides.get("AGENT_NAME", "")
    image_tag = overrides.get("IMAGE_TAG", "")
    if not agent_id or not config_store.valid_agent_id(agent_id):
        return None
    # IMAGE_TAG is interpolated into the pulled image URI. Even though the only
    # caller that can StartBuild (the admin Lambda) generates a safe build-<ts>
    # tag today, validate it here so a crafted tag (e.g. an "@sha256:" digest or a
    # ":"/"/" that repoints the reference to another image) can never reach the
    # runtime if any other principal ever gains StartBuild.
    if not _IMAGE_TAG_RE.match(image_tag):
        logger.warning("build event for %s has invalid IMAGE_TAG %r — ignoring",
                       agent_id, image_tag)
        return None
    return agent_id, image_tag, status


def handler(event, context=None):
    """EventBridge entry point for CodeBuild build-state-change events.

    Only SUCCEEDED builds proceed to runtime deployment. A FAILED/STOPPED build
    marks the capability ``failed`` with the build status and leaves any existing
    runtime untouched."""
    parsed = _agent_from_build_event(event)
    if parsed is None:
        logger.info("build event without a valid AGENT_NAME override — ignoring")
        return {"ok": False, "reason": "unrecognized build event"}
    agent_id, image_tag, status = parsed

    if status != "SUCCEEDED":
        logger.warning("build for %s ended %s — marking capability failed", agent_id, status)
        try:
            config_store.set_capability_status(
                agent_id, config_store.CAP_FAILED, detail=f"build {status}"
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not mark %s failed after build %s", agent_id, status)
        return {"ok": False, "agent_id": agent_id, "build_status": status}

    deploy_capability(agent_id, image_tag)
    return {"ok": True, "agent_id": agent_id}
