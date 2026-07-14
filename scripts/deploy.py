#!/usr/bin/env python3
"""Interactive deploy for the SDLC Agent Fleet.

Walks an operator through configuration and runs the whole local-credentials
deploy — no GitHub OIDC role or CI required, because a human already has
credentials. It orchestrates the real CLIs (`aws`, `sam`, `docker`) against the
AWS profile you choose, so it reflects exactly what a hand deploy would do:

  1. Preflight — check aws / sam / docker are installed (guidance if not).
  2. Pick an AWS profile + region; confirm the account.
  3. Collect config (stage, Asana GIDs, target repo, per-agent env) — remembered
     in .sdlc-agents/deploy.config.json so re-runs pre-fill.
  4. Choose which agents to deploy.
  5. `sam build && sam deploy` the foundation stack.
  6. Preflight the SSM secrets each selected agent needs (guidance if missing).
  7. Per agent: IAM runtime role -> ECR repo -> docker build/push ->
     AgentCore Runtime create/update -> wait READY.
  8. Sync the dispatch registry to SSM.

Everything is idempotent (safe to re-run) and gated behind a confirmation that
shows the account/region/stage. Use --dry-run to see the plan without touching
AWS.

Usage:
    python scripts/deploy.py [--dry-run] [--profile NAME] [--region REGION]
"""

import argparse
import configparser
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".sdlc-agents" / "deploy.config.json"
FOUNDATION_DIR = REPO_ROOT / "infra" / "foundation"
AGENTS_DIR = REPO_ROOT / "agents"

# Agents that ship today (agents/ minus shared/). The interactive picker offers
# these; the IAM/env tables below must stay in sync with the set.
SHIPPING_AGENTS = ["workitems", "researcher", "docwriter", "adr"]

REQUIRED_TOOLS = ("aws", "sam", "docker")


# --------------------------------------------------------------------------- #
# Tooling preflight
# --------------------------------------------------------------------------- #


def detect_tools() -> dict[str, bool]:
    """Which required CLIs are on PATH. Docker also needs a running daemon,
    checked separately by docker_daemon_ok()."""
    return {tool: shutil.which(tool) is not None for tool in REQUIRED_TOOLS}


def docker_daemon_ok() -> bool:
    """True if the Docker daemon is reachable (image build/push needs it)."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except Exception:
        return False


def install_hint(tool: str) -> str:
    """OS-specific one-liner to install a missing tool. We guide rather than
    auto-install: these need sudo, vary by OS, and Docker needs daemon setup —
    silently running a package manager mid-deploy is too surprising."""
    system = platform.system()
    mac = system == "Darwin"
    hints = {
        "aws": {
            "Darwin": "brew install awscli",
            "Linux": 'curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip && unzip awscliv2.zip && sudo ./aws/install',
            "doc": "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        },
        "sam": {
            "Darwin": "brew install aws-sam-cli",
            "Linux": "brew install aws-sam-cli  # or see the docs link",
            "doc": "https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html",
        },
        "docker": {
            "Darwin": "brew install --cask docker  # then launch Docker Desktop",
            "Linux": "curl -fsSL https://get.docker.com | sh  # then start the daemon",
            "doc": "https://docs.docker.com/get-docker/",
        },
    }
    h = hints[tool]
    cmd = h["Darwin"] if mac else h["Linux"]
    return f"  {cmd}\n  docs: {h['doc']}"


def preflight_tools() -> bool:
    """Report tool status; return True only if all are usable. Prints install
    guidance for anything missing."""
    print("Checking required tools…")
    present = detect_tools()
    ok = True
    for tool in REQUIRED_TOOLS:
        if present[tool]:
            print(f"  ✅ {tool}")
        else:
            ok = False
            print(f"  ❌ {tool} — not found. Install it:")
            print(install_hint(tool))
    if present["docker"] and not docker_daemon_ok():
        ok = False
        print(
            "  ⚠️  docker is installed but the daemon isn't reachable — "
            "start Docker (e.g. open Docker Desktop / `sudo systemctl start docker`)."
        )
    return ok


# --------------------------------------------------------------------------- #
# AWS profile / identity
# --------------------------------------------------------------------------- #


def list_profiles() -> list[str]:
    """Profile names from ~/.aws/config and ~/.aws/credentials. Config sections
    are prefixed 'profile ' except [default]; credentials sections are bare."""
    profiles: list[str] = []
    config = Path.home() / ".aws" / "config"
    creds = Path.home() / ".aws" / "credentials"

    if config.exists():
        parser = configparser.ConfigParser()
        parser.read(config)
        for section in parser.sections():
            name = (
                section[len("profile ") :]
                if section.startswith("profile ")
                else section
            )
            if name not in profiles:
                profiles.append(name)
    if creds.exists():
        parser = configparser.ConfigParser()
        parser.read(creds)
        for section in parser.sections():
            if section not in profiles:
                profiles.append(section)
    return profiles


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            print(f"  (ignoring unreadable {CONFIG_PATH})")
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Per-agent IAM policies and runtime env (must match docs/aws-deploy.md and the
# deploy-*.yml workflows)
# --------------------------------------------------------------------------- #


def _ssm_read_statement(region: str, account: str, suffixes: list[str]) -> dict:
    resources = [
        f"arn:aws:ssm:{region}:{account}:parameter/sdlc-agents/{s}" for s in suffixes
    ]
    return {
        "Effect": "Allow",
        "Action": "ssm:GetParameter",
        "Resource": resources,
    }


# Per-agent SSM parameter suffixes the runtime role may read (and that the
# secret preflight checks for). "*" suffixes are prefix reads.
AGENT_SSM: dict[str, list[str]] = {
    "workitems": ["asana-mcp-*", "asana-pat", "github-mcp-*"],
    "researcher": ["asana-mcp-*", "researcher-tavily-api-key"],
    "docwriter": ["asana-mcp-*", "github-mcp-*"],
    "adr": ["github-mcp-*"],
}

# Concrete SSM parameters checked in the secret preflight (expanded from the
# wildcard sets above — a "*" set means the deployer must have run the matching
# bootstrap; we probe the specific leaf params the agents actually read).
AGENT_REQUIRED_SSM: dict[str, list[str]] = {
    "workitems": [
        "asana-pat",
        "asana-mcp-client-id",
        "asana-mcp-client-secret",
        "asana-mcp-refresh-token",
        "github-mcp-token",
    ],
    "researcher": [
        "asana-mcp-client-id",
        "asana-mcp-client-secret",
        "asana-mcp-refresh-token",
        "researcher-tavily-api-key",
    ],
    "docwriter": [
        "asana-mcp-client-id",
        "asana-mcp-client-secret",
        "asana-mcp-refresh-token",
        "github-mcp-token",
    ],
    "adr": ["github-mcp-token"],
}


def agent_role_policies(
    agent: str, region: str, account: str, stage: str
) -> dict[str, dict]:
    """Inline policies for an agent's runtime role, keyed by policy name.
    Baseline (all agents): CloudWatch Logs, the assignments table, ECR pull.
    Plus the per-agent SSM reads from AGENT_SSM."""
    table_arn = (
        f"arn:aws:dynamodb:{region}:{account}:table/dispatch-assignments-{stage}"
    )
    policies: dict[str, dict] = {
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
    }
    suffixes = AGENT_SSM.get(agent, [])
    if suffixes:
        policies["ssm-read"] = {
            "Version": "2012-10-17",
            "Statement": [_ssm_read_statement(region, account, suffixes)],
        }
    return policies


def agent_env(
    agent: str, cfg: dict, stage: str, guardrail_id: str, guardrail_version: str
) -> dict[str, str]:
    """Runtime environment variables for an agent (mirrors deploy-*.yml plus the
    guardrail env from the foundation stack, plus the stage-correct assignments
    table so non-dev stages write to the right table)."""
    env = {
        "ASSIGNMENTS_TABLE": f"dispatch-assignments-{stage}",
        "BEDROCK_GUARDRAIL_ID": guardrail_id,
        "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
    }
    if agent in ("workitems", "docwriter", "adr"):
        env["GITHUB_REPO"] = cfg.get("target_repo", "")
    if agent in ("workitems", "docwriter", "researcher"):
        env["ASANA_PROJECT_GID"] = cfg.get("asana_project_gid", "")
        env["ASANA_WORKSPACE_GID"] = cfg.get("asana_workspace_gid", "")
        if cfg.get("asana_project_name"):
            env["ASANA_PROJECT_NAME"] = cfg["asana_project_name"]
    return {k: v for k, v in env.items() if v != ""}


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #


class Runner:
    """Runs shell commands, honoring --dry-run (print, don't execute)."""

    def __init__(self, dry_run: bool, profile: str | None, region: str):
        self.dry_run = dry_run
        self.profile = profile
        self.region = region

    def _aws_base(self) -> list[str]:
        base = ["aws"]
        if self.profile:
            base += ["--profile", self.profile]
        base += ["--region", self.region]
        return base

    def aws(self, args: list[str], capture: bool = False, check: bool = True):
        """Run an `aws` subcommand with the selected profile/region."""
        cmd = self._aws_base() + args
        return self.run(cmd, capture=capture, check=check)

    def aws_json(self, args: list[str]):
        """Run an aws subcommand and parse JSON stdout. Returns None on dry-run
        or non-zero exit (caller treats as 'not found')."""
        result = self.aws(args + ["--output", "json"], capture=True, check=False)
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def run(
        self,
        cmd: list[str],
        capture: bool = False,
        check: bool = True,
        cwd: Path | None = None,
    ):
        printable = " ".join(cmd)
        if self.dry_run:
            print(f"  [dry-run] {printable}")
            return None
        print(f"  $ {printable}")
        result = subprocess.run(cmd, capture_output=capture, text=True, cwd=cwd)
        if check and result.returncode != 0:
            if capture and result.stderr:
                print(result.stderr, file=sys.stderr)
            raise RuntimeError(f"command failed ({result.returncode}): {printable}")
        return result


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{text}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def prompt_choice(text: str, options: list[str], default: str | None = None) -> str:
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    while True:
        raw = prompt(text, default or "")
        if raw in options:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  Please choose one of the listed options.")


def prompt_yes(text: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = prompt(f"{text} ({d})").lower()
    if not raw:
        return default
    return raw.startswith("y")


# --------------------------------------------------------------------------- #
# Interactive flow
# --------------------------------------------------------------------------- #


def choose_profile(cfg: dict, override: str | None) -> str | None:
    if override:
        return override
    profiles = list_profiles()
    if not profiles:
        print(
            "  No named AWS profiles found in ~/.aws. Using default credentials "
            "(env vars / instance role)."
        )
        return None
    default = cfg.get("profile") if cfg.get("profile") in profiles else profiles[0]
    print("Select an AWS profile:")
    return prompt_choice("Profile", profiles, default)


def choose_agents(cfg: dict) -> list[str]:
    prev = cfg.get("agents", SHIPPING_AGENTS)
    print(f"Agents available: {', '.join(SHIPPING_AGENTS)}")
    raw = prompt("Which to deploy (comma-separated, or 'all')", ",".join(prev))
    if raw.strip().lower() == "all":
        return list(SHIPPING_AGENTS)
    chosen = [a.strip() for a in raw.split(",") if a.strip()]
    unknown = [a for a in chosen if a not in SHIPPING_AGENTS]
    if unknown:
        print(f"  Ignoring unknown agents: {', '.join(unknown)}")
    return [a for a in chosen if a in SHIPPING_AGENTS] or list(SHIPPING_AGENTS)


def deploy_foundation(runner: Runner, cfg: dict) -> None:
    print("\n== Foundation stack (sam) ==")
    stack = f"sdlc-agents-{cfg['stage']}"
    runner.run(["sam", "build"], cwd=FOUNDATION_DIR)
    deploy_cmd = [
        "sam",
        "deploy",
        "--stack-name",
        stack,
        "--capabilities",
        "CAPABILITY_IAM",
        "CAPABILITY_NAMED_IAM",
        "--resolve-s3",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
        "--region",
        runner.region,
        "--parameter-overrides",
        f"Stage={cfg['stage']}",
        f"WorkitemsBotGID={cfg.get('workitems_bot_gid', '')}",
        f"AgentFieldGID={cfg.get('agent_field_gid', '')}",
        f"DeployDashboard={'true' if cfg.get('deploy_dashboard') else 'false'}",
    ]
    if runner.profile:
        deploy_cmd += ["--profile", runner.profile]
    runner.run(deploy_cmd, cwd=FOUNDATION_DIR)


def stack_outputs(runner: Runner, stack: str) -> dict[str, str]:
    data = runner.aws_json(["cloudformation", "describe-stacks", "--stack-name", stack])
    if not data:
        return {}
    outs = data.get("Stacks", [{}])[0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def check_secrets(runner: Runner, agents: list[str]) -> None:
    print("\n== Secret preflight (SSM) ==")
    needed: set[str] = set()
    for agent in agents:
        needed.update(AGENT_REQUIRED_SSM.get(agent, []))
    missing = []
    for suffix in sorted(needed):
        name = f"/sdlc-agents/{suffix}"
        found = runner.aws_json(["ssm", "get-parameter", "--name", name])
        if found:
            print(f"  ✅ {name}")
        else:
            missing.append(name)
            print(f"  ❌ {name} — not set")
    if missing:
        print(
            "\n  Some agents will fail at invocation without these. Populate them with the "
            "connect flows / bootstrap scripts:"
        )
        print(
            "    Asana:  python scripts/bootstrap_asana_oauth.py   (and asana-pat / MCP client creds)"
        )
        print(
            "    GitHub: set /sdlc-agents/github-mcp-token (PAT) or the GitHub App params"
        )
        print("    Tavily: set /sdlc-agents/researcher-tavily-api-key")
        if not prompt_yes("Continue deploying agents anyway?", default=False):
            raise SystemExit("Aborting — populate the missing secrets first.")


def ensure_agent_role(
    runner: Runner, agent: str, region: str, account: str, stage: str
) -> None:
    role = f"{agent}-agentcore-runtime"
    existing = runner.aws_json(["iam", "get-role", "--role-name", role])
    if existing is None and not runner.dry_run:
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
        runner.aws(
            [
                "iam",
                "create-role",
                "--role-name",
                role,
                "--assume-role-policy-document",
                json.dumps(trust),
                "--description",
                f"AgentCore runtime role for {agent}",
            ]
        )
    else:
        print(
            f"  role {role}: exists (skip create)"
            if existing
            else f"  [dry-run] create role {role}"
        )
    runner.aws(
        [
            "iam",
            "attach-role-policy",
            "--role-name",
            role,
            "--policy-arn",
            "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
        ],
        check=False,
    )
    for name, doc in agent_role_policies(agent, region, account, stage).items():
        runner.aws(
            [
                "iam",
                "put-role-policy",
                "--role-name",
                role,
                "--policy-name",
                name,
                "--policy-document",
                json.dumps(doc),
            ],
            check=False,
        )


def ensure_ecr(runner: Runner, agent: str) -> None:
    repo = f"sdlc-agents/{agent}"
    if (
        runner.aws_json(["ecr", "describe-repositories", "--repository-names", repo])
        is None
    ):
        runner.aws(
            [
                "ecr",
                "create-repository",
                "--repository-name",
                repo,
                "--image-scanning-configuration",
                "scanOnPush=true",
                "--image-tag-mutability",
                "IMMUTABLE",
            ],
            check=False,
        )
    else:
        print(f"  ECR repo {repo}: exists (skip create)")


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        )
        return out.stdout.strip()[:12] or "latest"
    except Exception:
        return "latest"


def build_and_push(
    runner: Runner, agent: str, account: str, region: str, tag: str
) -> str:
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/sdlc-agents/{agent}:{tag}"
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    # ECR login: pipe the password into docker login. In dry-run just show it.
    if runner.dry_run:
        print(f"  [dry-run] aws ecr get-login-password | docker login {registry}")
        print(
            f"  [dry-run] docker build -f {agent}/Dockerfile -t {image} . (cwd=agents/)"
        )
        print(f"  [dry-run] docker push {image}")
        return image
    pw = runner.aws(["ecr", "get-login-password"], capture=True)
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=pw.stdout,
        text=True,
        check=True,
    )
    runner.run(
        ["docker", "build", "-f", f"{agent}/Dockerfile", "-t", image, "."],
        cwd=AGENTS_DIR,
    )
    runner.run(["docker", "push", image])
    return image


def deploy_runtime(
    runner: Runner, agent: str, image: str, account: str, env: dict[str, str]
) -> None:
    role_arn = f"arn:aws:iam::{account}:role/{agent}-agentcore-runtime"
    artifact = f"containerConfiguration={{containerUri={image}}}"
    env_arg = ",".join(f"{k}={v}" for k, v in env.items())

    listing = runner.aws_json(["bedrock-agentcore-control", "list-agent-runtimes"])
    existing_id = None
    if listing:
        for rt in listing.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == agent:
                existing_id = rt.get("agentRuntimeId")
                break

    common = [
        "--agent-runtime-artifact",
        artifact,
        "--role-arn",
        role_arn,
        "--network-configuration",
        "networkMode=PUBLIC",
    ]
    if env_arg:
        common += ["--environment-variables", env_arg]

    if existing_id:
        print(f"  runtime {agent}: updating {existing_id}")
        runner.aws(
            [
                "bedrock-agentcore-control",
                "update-agent-runtime",
                "--agent-runtime-id",
                existing_id,
            ]
            + common,
            check=False,
        )
    else:
        print(f"  runtime {agent}: creating")
        runner.aws(
            [
                "bedrock-agentcore-control",
                "create-agent-runtime",
                "--agent-runtime-name",
                agent,
            ]
            + common,
            check=False,
        )


def sync_registry(runner: Runner, stage: str) -> None:
    print("\n== Sync dispatch registry ==")
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "sync_registry.py"),
        "--stage",
        stage,
        "--region",
        runner.region,
    ]
    if runner.dry_run:
        cmd.append("--dry-run")
    env = os.environ.copy()
    if runner.profile:
        env["AWS_PROFILE"] = runner.profile
    printable = " ".join(cmd)
    print(f"  $ {printable}")
    subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def gather_config(cfg: dict) -> dict:
    """Interactive config, pre-filled from the saved config."""
    print("\n== Configuration ==")
    cfg["stage"] = prompt_choice(
        "Stage", ["dev", "staging", "prod"], cfg.get("stage", "dev")
    )
    cfg["target_repo"] = prompt(
        "Target GitHub repo (owner/repo)", cfg.get("target_repo", "")
    )
    cfg["asana_project_gid"] = prompt(
        "Asana project GID (blank to skip)", cfg.get("asana_project_gid", "")
    )
    cfg["asana_workspace_gid"] = prompt(
        "Asana workspace GID (blank to skip)", cfg.get("asana_workspace_gid", "")
    )
    cfg["asana_project_name"] = prompt(
        "Asana project name (cosmetic, optional)", cfg.get("asana_project_name", "")
    )
    cfg["workitems_bot_gid"] = prompt(
        "Workitems bot Asana user GID (for the foundation stack)",
        cfg.get("workitems_bot_gid", ""),
    )
    cfg["agent_field_gid"] = prompt(
        "Asana 'Agent' custom-field GID (optional)", cfg.get("agent_field_gid", "")
    )
    cfg["deploy_dashboard"] = prompt_yes(
        "Deploy the monitoring dashboard?", cfg.get("deploy_dashboard", False)
    )
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Print the plan; touch nothing."
    )
    ap.add_argument("--profile", help="AWS profile to use (skips the picker).")
    ap.add_argument("--region", help="AWS region (skips the prompt).")
    args = ap.parse_args()

    print("SDLC Agent Fleet — interactive deploy\n")

    if not preflight_tools():
        print(
            "\nInstall the missing tool(s) above, then re-run. (Docker also needs its daemon running.)"
        )
        return 1

    cfg = load_config()
    profile = choose_profile(cfg, args.profile)
    cfg["profile"] = profile
    region = args.region or prompt("AWS region", cfg.get("region", "us-west-2"))
    cfg["region"] = region

    runner = Runner(dry_run=args.dry_run, profile=profile, region=region)

    # Confirm identity before anything else.
    ident = runner.aws_json(["sts", "get-caller-identity"])
    if ident:
        account = ident["Account"]
        print(
            f"\nAWS account: {account}  (arn: {ident.get('Arn', '?')})  region: {region}"
        )
    elif args.dry_run:
        account = "000000000000"
        print(f"\n[dry-run] AWS account: <unknown>  region: {region}")
    else:
        print("\nCould not resolve AWS identity — check your profile/credentials.")
        return 1

    cfg = gather_config(cfg)
    agents = choose_agents(cfg)
    cfg["agents"] = agents
    save_config(cfg)

    print("\n== Plan ==")
    print(f"  Account:   {account}")
    print(f"  Region:    {region}")
    print(f"  Stage:     {cfg['stage']}")
    print(f"  Dashboard: {'yes' if cfg.get('deploy_dashboard') else 'no'}")
    print(f"  Agents:    {', '.join(agents)}")
    print(f"  Config saved to: {CONFIG_PATH.relative_to(REPO_ROOT)}")

    if not args.dry_run and not prompt_yes("\nProceed with deploy?", default=False):
        print("Aborted.")
        return 0

    deploy_foundation(runner, cfg)

    stack = f"sdlc-agents-{cfg['stage']}"
    outputs = stack_outputs(runner, stack)
    guardrail_id = outputs.get("GuardrailId", "")
    guardrail_version = outputs.get("GuardrailVersion", "DRAFT")

    check_secrets(runner, agents)

    tag = git_sha()
    for agent in agents:
        print(f"\n== Agent: {agent} ==")
        ensure_agent_role(runner, agent, region, account, cfg["stage"])
        ensure_ecr(runner, agent)
        image = build_and_push(runner, agent, account, region, tag)
        env = agent_env(agent, cfg, cfg["stage"], guardrail_id, guardrail_version)
        deploy_runtime(runner, agent, image, account, env)

    sync_registry(runner, cfg["stage"])

    print("\n== Done ==")
    if outputs.get("WebhookEndpoint"):
        print(f"  Asana webhook endpoint: {outputs['WebhookEndpoint']}")
        print("  Register it with: python scripts/bootstrap_asana_webhook.py")
    if cfg.get("deploy_dashboard") and outputs.get("DashboardUrl"):
        print(
            f"  Dashboard: {outputs['DashboardUrl']} (add operators to the Cognito 'operators' group)"
        )
    print("  Verify a runtime is READY, then @mention an agent to smoke-test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
