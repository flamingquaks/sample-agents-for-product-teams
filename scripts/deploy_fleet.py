"""Deploy the whole v2 fleet — foundation + agents + dashboard — in one command.

v1 deployed each piece from its own push-on-main GitHub Actions workflow. v2 is
one coherent system, so this is the single, on-demand deployer that stands the
fleet up (or updates it) in dependency order:

  1. Foundation stack (sam) — DynamoDB, Lambdas, guardrail, AgentCore Gateway +
     Cedar policy engine + SCM broker/interceptor, Cognito + dashboard API/CDN.
     Preserves the target stack's existing parameter values by default.
  2. Per-agent runtime IAM roles — create the missing ones (e.g. adr), refresh
     policies on the rest. Reuses scripts.bootstrap so the role shape can't drift.
  3. Agent runtimes — build/push each container to ECR, resolve guardrail +
     gateway URL from stack outputs, create-or-update the AgentCore runtime, wait
     for READY. Same steps as .github/workflows/deploy-agent.yml, in a loop.
  4. Dispatch registry — sync .dispatch/agents.yaml to SSM with real runtime ARNs.
  5. Dashboard SPA — build, write config.json from stack outputs, S3 sync +
     CloudFront invalidation. Same as .github/workflows/deploy-dashboard.yml.

Idempotent: safe to re-run. ``--dry-run`` prints every command without executing
(no AWS calls that mutate). Scoped strictly to the four fleet agents so it never
touches other runtimes sharing the account.

Usage:
    python scripts/deploy_fleet.py --stage staging --region us-east-1
    python scripts/deploy_fleet.py --stage staging --dry-run
    python scripts/deploy_fleet.py --agents adr            # one agent + registry
    python scripts/deploy_fleet.py --skip-dashboard
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import boto3

# Reuse the role shape + agent list from bootstrap so they can never drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402

logger = logging.getLogger("deploy_fleet")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

REPO_ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_DIR = REPO_ROOT / "infra" / "foundation"
AGENTS_DIR = REPO_ROOT / "agents"
DASHBOARD_DIR = REPO_ROOT / "dashboard"

AGENTS = list(bootstrap.SHIPPING_AGENTS)  # workitems, researcher, docwriter, adr

# Per-agent runtime env beyond the guardrail + gateway URL (which are resolved
# from stack outputs). Mirrors the env_vars each deploy-<agent>.yml wrapper sets;
# adr reads no agent-specific env. Asana GIDs come from the foundation stack's
# parameters so this stays a single source of truth with the deployed stack.
ASANA_AGENTS = {"workitems", "researcher", "docwriter"}


class DeployError(RuntimeError):
    pass


class Runner:
    """Runs shell/AWS commands, or prints them under --dry-run."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def run(self, cmd: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
        printable = " ".join(cmd)
        if self.dry_run:
            logger.info("DRY-RUN %s%s", printable, f"  (cwd={cwd})" if cwd else "")
            return ""
        logger.info("$ %s", printable)
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=False,
            text=True,
            capture_output=capture,
        )
        if result.returncode != 0:
            out = (result.stdout or "") + (result.stderr or "")
            raise DeployError(f"command failed ({result.returncode}): {printable}\n{out}")
        return (result.stdout or "").strip() if capture else ""


# --- foundation --------------------------------------------------------------


def _existing_stack_params(region: str, stack: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    except cfn.exceptions.ClientError:
        return {}
    if not stacks:
        return {}
    return {p["ParameterKey"]: p["ParameterValue"] for p in stacks[0].get("Parameters", [])}


def _stack_outputs(region: str, stack: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def deploy_foundation(runner: Runner, stage: str, region: str, auto_approve: bool) -> None:
    stack = f"sdlc-agents-{stage}"
    logger.info("== Foundation stack (%s) ==", stack)
    # Preserve the parameter values already on the stack (DeployGateway,
    # DeployDashboard, GitHubAuthMode, the Asana GIDs an operator set, etc.) so a
    # redeploy is a code/template update, not a silent reconfiguration. For a
    # brand-new stack there are none, and the template defaults apply.
    existing = _existing_stack_params(region, stack)
    runner.run(["sam", "build"], cwd=FOUNDATION_DIR)
    cmd = [
        "sam", "deploy",
        "--stack-name", stack,
        "--region", region,
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--no-fail-on-empty-changeset",
        "--resolve-s3",
    ]
    # Default: let sam PRINT the changeset and prompt for approval before it
    # applies IAM/networking changes to the live stack (never a blind apply).
    # --auto-approve opts into the non-interactive path for unattended runs.
    cmd += ["--no-confirm-changeset"] if auto_approve else ["--confirm-changeset"]
    if existing:
        overrides = [f"{k}={v}" for k, v in sorted(existing.items())]
        cmd += ["--parameter-overrides", *overrides]
        logger.info("preserving %d existing stack parameters", len(existing))
    else:
        # New stack: Stage is the only required-without-default parameter beyond
        # the Asana GIDs, which an operator sets later; template defaults cover
        # the rest. Fail loudly rather than guess Asana GIDs.
        cmd += ["--parameter-overrides", f"Stage={stage}"]
        logger.warning(
            "no existing stack %s — deploying with template defaults + Stage=%s; "
            "set Asana GIDs / DeployGateway / DeployDashboard afterward if needed",
            stack, stage,
        )
    runner.run(cmd, cwd=FOUNDATION_DIR)


# --- agent runtimes ----------------------------------------------------------


def ensure_agent_roles(runner: Runner, agents: list[str], region: str, account: str, stage: str) -> None:
    logger.info("== Per-agent runtime IAM roles ==")
    iam = None if runner.dry_run else boto3.client("iam")
    for agent in agents:
        role = f"{agent}-agentcore-runtime"
        exists = True
        if not runner.dry_run:
            try:
                iam.get_role(RoleName=role)
            except iam.exceptions.NoSuchEntityException:
                exists = False
        if exists:
            logger.info("role %s: exists (refreshing policies)", role)
        else:
            logger.info("role %s: creating", role)
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
            runner.run([
                "aws", "iam", "create-role",
                "--role-name", role,
                "--assume-role-policy-document", json.dumps(trust),
                "--description", f"AgentCore runtime role for {agent}",
            ])
        runner.run([
            "aws", "iam", "attach-role-policy",
            "--role-name", role,
            "--policy-arn", "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
        ])
        for name, doc in bootstrap.agent_role_policies(agent, region, account, stage).items():
            runner.run([
                "aws", "iam", "put-role-policy",
                "--role-name", role,
                "--policy-name", name,
                "--policy-document", json.dumps(doc),
            ])


def _ecr_login(runner: Runner, region: str, account: str) -> str:
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    if runner.dry_run:
        logger.info("DRY-RUN docker login %s", registry)
        return registry
    pw = runner.run(
        ["aws", "ecr", "get-login-password", "--region", region], capture=True
    )
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=pw, text=True, check=True,
    )
    return registry


def _ensure_ecr_repo(runner: Runner, agent: str, region: str) -> None:
    repo = f"sdlc-agents/{agent}"
    if not runner.dry_run:
        ecr = boto3.client("ecr", region_name=region)
        try:
            ecr.describe_repositories(repositoryNames=[repo])
            return
        except ecr.exceptions.RepositoryNotFoundException:
            pass
    runner.run([
        "aws", "ecr", "create-repository",
        "--repository-name", repo,
        "--region", region,
        "--image-scanning-configuration", "scanOnPush=true",
        "--image-tag-mutability", "IMMUTABLE",
    ])


def _git_sha(runner: Runner) -> str:
    if runner.dry_run:
        return "dryrunsha"
    return runner.run(["git", "rev-parse", "--short", "HEAD"], capture=True)


def _agent_env(agent: str, outputs: dict[str, str], stack_params: dict[str, str], region: str) -> str:
    """Runtime env for an agent: guardrail (all) + gateway URL (all) + Asana GIDs
    (asana agents). Matches deploy-agent.yml's resolve steps + the per-agent
    wrapper env_vars, sourced from the deployed stack so there's one truth."""
    pairs = [
        f"BEDROCK_GUARDRAIL_ID={outputs['GuardrailId']}",
        f"BEDROCK_GUARDRAIL_VERSION={outputs['GuardrailVersion']}",
        f"GATEWAY_MCP_URL={outputs['FleetGatewayUrl']}",
    ]
    if agent in ASANA_AGENTS:
        # The stack doesn't publish the Asana project/workspace GIDs as outputs;
        # they are operator-set. Pull from SSM if present, else leave unset (the
        # agent KeyErrors at invocation, which the operator resolves separately).
        pass
    return ",".join(pairs)


def deploy_agent(runner: Runner, agent: str, region: str, account: str,
                 outputs: dict[str, str], stack_params: dict[str, str], sha: str,
                 registry: str) -> None:
    logger.info("== Agent runtime: %s ==", agent)
    repo = f"sdlc-agents/{agent}"
    image = f"{registry}/{repo}:{sha}"
    _ensure_ecr_repo(runner, agent, region)
    # Build context is agents/ so the shared/ package is included (matches
    # deploy-agent.yml's working-directory: agents).
    runner.run(
        ["docker", "build", "-f", f"{agent}/Dockerfile", "-t", image, "."],
        cwd=AGENTS_DIR,
    )
    runner.run(["docker", "push", image])

    env_csv = _agent_env(agent, outputs, stack_params, region)
    role_arn = f"arn:aws:iam::{account}:role/{agent}-agentcore-runtime"
    artifact = f"containerConfiguration={{containerUri={image}}}"

    existing_id = ""
    if not runner.dry_run:
        acc = boto3.client("bedrock-agentcore-control", region_name=region)
        rts = acc.list_agent_runtimes().get("agentRuntimes", [])
        existing_id = next(
            (r["agentRuntimeId"] for r in rts if r["agentRuntimeName"] == agent), ""
        )

    common = [
        "--agent-runtime-artifact", artifact,
        "--role-arn", role_arn,
        "--network-configuration", "networkMode=PUBLIC",
        "--region", region,
    ]
    env_args = ["--environment-variables", env_csv] if env_csv else []

    if existing_id:
        logger.info("updating runtime %s (%s)", agent, existing_id)
        runner.run([
            "aws", "bedrock-agentcore-control", "update-agent-runtime",
            "--agent-runtime-id", existing_id, *common, *env_args,
        ])
        runtime_id = existing_id
    else:
        logger.info("creating runtime %s", agent)
        out = runner.run([
            "aws", "bedrock-agentcore-control", "create-agent-runtime",
            "--agent-runtime-name", agent, *common, *env_args,
            "--query", "agentRuntimeId", "--output", "text",
        ], capture=True)
        runtime_id = out or "dryrun-id"

    _wait_runtime_ready(runner, region, runtime_id, agent)


def _wait_runtime_ready(runner: Runner, region: str, runtime_id: str, agent: str) -> None:
    if runner.dry_run:
        logger.info("DRY-RUN wait for %s READY", agent)
        return
    acc = boto3.client("bedrock-agentcore-control", region_name=region)
    for _ in range(60):
        status = acc.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
        logger.info("%s status=%s", agent, status)
        if status == "READY":
            return
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            raise DeployError(f"{agent} runtime failed: {status}")
        time.sleep(10)
    raise DeployError(f"timed out waiting for {agent} runtime READY")


# --- dashboard ---------------------------------------------------------------


def deploy_dashboard(runner: Runner, region: str, outputs: dict[str, str]) -> None:
    logger.info("== Dashboard SPA ==")
    bucket = outputs.get("DashboardSiteBucketName")
    if not bucket:
        logger.info("dashboard not enabled on the stack (no site bucket) — skipping")
        return
    runner.run(["npm", "ci"], cwd=DASHBOARD_DIR)
    runner.run(["npm", "run", "build"], cwd=DASHBOARD_DIR)

    authority = f"https://cognito-idp.{region}.amazonaws.com/{outputs['DashboardUserPoolId']}"
    config = {
        "apiBaseUrl": outputs["DashboardApiEndpoint"],
        "cognitoAuthority": authority,
        "cognitoClientId": outputs["DashboardUserPoolClientId"],
        "cognitoLoginDomain": outputs["DashboardLoginDomain"],
        "redirectUri": outputs["DashboardUrl"],
    }
    config_path = DASHBOARD_DIR / "dist" / "config.json"
    if runner.dry_run:
        logger.info("DRY-RUN write %s = %s", config_path, json.dumps(config))
    else:
        config_path.write_text(json.dumps(config, indent=2))
        logger.info("wrote %s", config_path)

    # Immutable hashed assets cache-forever; index.html + config.json never cached.
    runner.run([
        "aws", "s3", "sync", "dist/", f"s3://{bucket}/", "--delete",
        "--cache-control", "public,max-age=31536000,immutable",
        "--exclude", "index.html", "--exclude", "config.json",
    ], cwd=DASHBOARD_DIR)
    runner.run([
        "aws", "s3", "cp", "dist/index.html", f"s3://{bucket}/index.html",
        "--cache-control", "no-cache",
    ], cwd=DASHBOARD_DIR)
    runner.run([
        "aws", "s3", "cp", "dist/config.json", f"s3://{bucket}/config.json",
        "--cache-control", "no-cache", "--content-type", "application/json",
    ], cwd=DASHBOARD_DIR)
    dist = outputs.get("DashboardDistributionId")
    if dist:
        runner.run([
            "aws", "cloudfront", "create-invalidation",
            "--distribution-id", dist, "--paths", "/*",
        ])
    logger.info("published dashboard: %s", outputs.get("DashboardUrl", ""))


# --- registry ----------------------------------------------------------------


def sync_registry_to_ssm(runner: Runner, stage: str, region: str) -> None:
    logger.info("== Dispatch registry → SSM ==")
    # Invoked as a subprocess (as the workflow does) — sync_registry.main() reads
    # sys.argv directly, so calling it in-process would inherit our argv.
    runner.run([
        sys.executable, str(REPO_ROOT / "scripts" / "sync_registry.py"),
        "--stage", stage, "--region", region,
    ])


# --- orchestration -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deploy the whole fleet.")
    ap.add_argument("--stage", default="staging", choices=["dev", "staging", "prod"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--agents", default=",".join(AGENTS),
                    help="comma-separated subset of agents (default: all)")
    ap.add_argument("--skip-foundation", action="store_true")
    ap.add_argument("--skip-agents", action="store_true")
    ap.add_argument("--skip-dashboard", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--auto-approve", action="store_true",
        help="skip the sam changeset confirmation prompt (unattended runs); by "
             "default the foundation changeset is printed and must be confirmed",
    )
    args = ap.parse_args(argv)

    agents = [a.strip() for a in args.agents.split(",") if a.strip() in AGENTS]
    if not agents:
        logger.error("no valid agents in --agents (choose from %s)", ",".join(AGENTS))
        return 2

    runner = Runner(args.dry_run)
    stack = f"sdlc-agents-{args.stage}"
    account = boto3.client("sts").get_caller_identity()["Account"] if not args.dry_run else "ACCOUNT"
    logger.info(
        "Deploying fleet: stage=%s region=%s account=%s agents=%s%s",
        args.stage, args.region, account, ",".join(agents),
        " [DRY-RUN]" if args.dry_run else "",
    )

    if not args.skip_foundation:
        deploy_foundation(runner, args.stage, args.region, args.auto_approve)

    # Resolve outputs once the foundation exists (agents + dashboard depend on them).
    if args.dry_run:
        outputs, stack_params = {
            "GuardrailId": "<guardrail>", "GuardrailVersion": "<ver>",
            "FleetGatewayUrl": "<gateway-url>",
            "DashboardSiteBucketName": "<bucket>", "DashboardDistributionId": "<dist>",
            "DashboardApiEndpoint": "<api>", "DashboardUserPoolId": "<pool>",
            "DashboardUserPoolClientId": "<client>", "DashboardLoginDomain": "<login>",
            "DashboardUrl": "<url>",
        }, {}
    else:
        outputs = _stack_outputs(args.region, stack)
        stack_params = _existing_stack_params(args.region, stack)
        for req in ("GuardrailId", "GuardrailVersion", "FleetGatewayUrl"):
            if not outputs.get(req):
                raise DeployError(
                    f"stack {stack} missing output {req} — deploy the foundation "
                    f"stack (with DeployGateway=true) first"
                )

    if not args.skip_agents:
        ensure_agent_roles(runner, agents, args.region, account, args.stage)
        sha = _git_sha(runner)
        registry = _ecr_login(runner, args.region, account)
        for agent in agents:
            deploy_agent(runner, agent, args.region, account, outputs,
                         stack_params, sha, registry)
        sync_registry_to_ssm(runner, args.stage, args.region)

    if not args.skip_dashboard:
        deploy_dashboard(runner, args.region, outputs)

    logger.info("Fleet deploy complete%s.", " (dry-run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
