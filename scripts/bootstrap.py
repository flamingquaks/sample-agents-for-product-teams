#!/usr/bin/env python3
"""Interactive bootstrap for the SDLC Agent Fleet.

Does the one-time, privileged setup that CI depends on but can't create for
itself — the genuinely cumbersome part of standing up the fleet — then hands
ongoing deployment to GitHub Actions. It runs against the AWS profile you
choose, because creating IAM roles and an OIDC provider needs a privileged
human, not a CI runner.

What it sets up (all idempotent):

  1. Preflight — aws / sam / gh installed (guidance if not).
  2. Pick an AWS profile + region; confirm the account.
  3. GitHub OIDC provider + a repo-scoped deploy role (trust restricted to your
     repo's main branch + PRs — the thing CI assumes).
  4. Per-agent AgentCore runtime IAM roles (least-privilege, per the agent's
     tool footprint).
  5. Foundation stack via `sam deploy` (DynamoDB, Dispatch Router, webhook API,
     SSM registry, guardrail — optionally the dashboard).
  6. GitHub Actions secrets + variables (`gh`) so the deploy workflows run:
     AWS_DEPLOY_ROLE_ARN, AWS_ACCOUNT_ID, AWS_REGION, TARGET_REPO, Asana GIDs.
  7. SSM secret preflight — points you at the bootstrap scripts for anything
     missing (this script does not write secrets itself).

It does NOT build images or create AgentCore runtimes — that's CI's job
(`.github/workflows/deploy-agent.yml`), which is the single source of truth for
agent deploys. After bootstrap, you push to `main` and CI deploys the agents.

Config is remembered in .sdlc-agents/bootstrap.config.json for re-runs. Use
--dry-run to see the plan without touching anything.

Usage:
    python scripts/bootstrap.py [--dry-run] [--profile NAME] [--region REGION]
"""

import argparse
import configparser
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".sdlc-agents" / "bootstrap.config.json"
FOUNDATION_DIR = REPO_ROOT / "infra" / "foundation"

SHIPPING_AGENTS = ["workitems", "researcher", "docwriter", "adr"]
REQUIRED_TOOLS = ("aws", "sam", "gh")

DEPLOY_ROLE_NAME = "sdlc-agents-deploy"
GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"


# --------------------------------------------------------------------------- #
# Tooling preflight
# --------------------------------------------------------------------------- #


def detect_tools() -> dict[str, bool]:
    """Which required CLIs are on PATH."""
    return {tool: shutil.which(tool) is not None for tool in REQUIRED_TOOLS}


def install_hint(tool: str) -> str:
    """OS-specific one-liner to install a missing tool. We guide rather than
    auto-install: these need sudo, vary by OS, and silently running a package
    manager mid-bootstrap is too surprising."""
    mac = platform.system() == "Darwin"
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
        "gh": {
            "Darwin": "brew install gh",
            "Linux": "See the docs link for your distro's package",
            "doc": "https://github.com/cli/cli#installation",
        },
    }
    h = hints[tool]
    cmd = h["Darwin"] if mac else h["Linux"]
    return f"  {cmd}\n  docs: {h['doc']}"


def gh_authenticated() -> bool:
    """True if `gh` has a usable auth token (needed to set repo secrets/vars)."""
    if shutil.which("gh") is None:
        return False
    try:
        return (
            subprocess.run(
                ["gh", "auth", "status"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except Exception:
        return False


def preflight_tools() -> bool:
    """Report tool status; True only if all usable. Guidance for anything missing."""
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
    if present["gh"] and not gh_authenticated():
        ok = False
        print(
            "  ⚠️  gh is installed but not authenticated — run `gh auth login` "
            "(needs repo admin to set Actions secrets/variables)."
        )
    return ok


# --------------------------------------------------------------------------- #
# AWS profile / identity
# --------------------------------------------------------------------------- #


def list_profiles() -> list[str]:
    """Profile names from ~/.aws/config and ~/.aws/credentials."""
    profiles: list[str] = []
    for path, strip in (
        (Path.home() / ".aws" / "config", True),
        (Path.home() / ".aws" / "credentials", False),
    ):
        if not path.exists():
            continue
        parser = configparser.ConfigParser()
        parser.read(path)
        for section in parser.sections():
            name = (
                section[len("profile ") :]
                if strip and section.startswith("profile ")
                else section
            )
            if name not in profiles:
                profiles.append(name)
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
# Per-agent runtime IAM policies (kept in sync with docs/aws-deploy.md and the
# deploy-*.yml workflows). CI assumes these roles exist; this script creates
# them, since IAM-role creation isn't something the deploy role should self-grant.
# --------------------------------------------------------------------------- #


AGENT_SSM: dict[str, list[str]] = {
    "workitems": ["asana-mcp-*", "asana-pat", "github-mcp-*"],
    "researcher": ["asana-mcp-*", "researcher-tavily-api-key"],
    "docwriter": ["asana-mcp-*", "github-mcp-*"],
    "adr": ["github-mcp-*"],
}

# Concrete leaf SSM params the secret preflight probes (agents fail at
# invocation, not deploy, when these are absent).
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
    """Inline policies for an agent's runtime role. Baseline (all): CloudWatch
    Logs, the assignments table, ECR pull. Plus per-agent SSM reads."""
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
        # When the fleet routes MCP tool calls through the AgentCore Gateway
        # (GATEWAY_MCP_URL set), the runtime signs requests with SigV4 using this
        # role, which needs bedrock-agentcore:InvokeGateway on the fleet gateway.
        # Scoped to the fleet gateway name pattern (sdlcFleet<Stage>); harmless
        # when the gateway isn't deployed. This is the runtime side of the auth
        # the gateway's AWS_IAM inbound authorizer expects.
        "agentcore-gateway-invoke": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "bedrock-agentcore:InvokeGateway",
                    "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/sdlcFleet{stage}*",
                }
            ],
        },
    }
    suffixes = AGENT_SSM.get(agent, [])
    if suffixes:
        policies["ssm-read"] = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "ssm:GetParameter",
                    "Resource": [
                        f"arn:aws:ssm:{region}:{account}:parameter/sdlc-agents/{s}"
                        for s in suffixes
                    ],
                }
            ],
        }
    return policies


def deploy_role_trust(account: str, owner: str, repo: str) -> dict:
    """Trust policy for the CI deploy role: GitHub OIDC, restricted with
    StringEquals to this repo's main branch + PRs (never a StringLike wildcard —
    that would let any branch assume the role)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub": [
                            f"repo:{owner}/{repo}:ref:refs/heads/main",
                            f"repo:{owner}/{repo}:pull_request",
                        ],
                    }
                },
            }
        ],
    }


def deploy_role_policy(region: str, account: str) -> dict:
    """Scoped permission policy for the CI deploy role — only what the deploy
    workflows actually do, instead of AdministratorAccess.

    The workflows that assume this role (deploy-agent.yml, deploy-dashboard.yml,
    agent-dispatch.yml, claude-code.yml) build/push agent images, create/update
    AgentCore runtimes, sync the SSM registry, read stack outputs, invoke the
    Dispatch Router + Bedrock, and (dashboard) push the SPA to S3 + invalidate
    CloudFront. The foundation `sam deploy` runs locally in this script, NOT in
    CI, so no CloudFormation write/IAM-role-create permissions are granted here.
    PassRole is limited to the per-agent runtime roles the workflows attach."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrPushPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:CreateRepository",
                    "ecr:DescribeRepositories",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:PutImageScanningConfiguration",
                ],
                "Resource": "*",
            },
            {
                "Sid": "AgentCoreRuntime",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore-control:*",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                "Resource": "*",
            },
            {
                # agent-dispatch.yml assumes this role and invokes the Dispatch
                # Router Lambda to route an @mention — without this the core
                # trigger path fails AccessDenied.
                "Sid": "InvokeDispatchRouter",
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": f"arn:aws:lambda:{region}:{account}:function:dispatch-router-*",
            },
            {
                # Runtime roles the deploy workflows attach, plus the gateway
                # service role the foundation stack creates and hands to the
                # AgentCore Gateway. Both are passed to bedrock-agentcore.
                "Sid": "PassAgentCoreRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [
                    f"arn:aws:iam::{account}:role/*-agentcore-runtime",
                    f"arn:aws:iam::{account}:role/sdlc-fleet-gateway-*",
                ],
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "RegistrySyncAndStackOutputs",
                "Effect": "Allow",
                "Action": [
                    "ssm:PutParameter",
                    "ssm:GetParameter",
                    "cloudformation:DescribeStacks",
                ],
                "Resource": "*",
            },
            {
                "Sid": "BedrockInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ApplyGuardrail",
                ],
                "Resource": "*",
            },
            {
                "Sid": "DashboardPublish",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "cloudfront:CreateInvalidation",
                ],
                "Resource": [
                    f"arn:aws:s3:::sdlc-agent-dashboard-{account}-*",
                    f"arn:aws:s3:::sdlc-agent-dashboard-{account}-*/*",
                    f"arn:aws:cloudfront::{account}:distribution/*",
                ],
            },
            {
                "Sid": "InspectorScan",
                "Effect": "Allow",
                "Action": "inspector-scan:ScanSbom",
                "Resource": "*",
            },
        ],
    }


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #


class Runner:
    """Runs shell commands, honoring --dry-run (print, don't execute).

    Mutating AWS/gh calls go through ``step()``, which records failures rather
    than raising — so one failed IAM/gh call doesn't abort the whole run, but
    the failures are collected and surfaced at the end (main() aborts with a
    summary instead of falsely reporting success). ``run()``/``aws()`` keep the
    raise-on-failure default for the few hard gates (sam) that must stop the run.
    """

    def __init__(self, dry_run: bool, profile: str | None, region: str):
        self.dry_run = dry_run
        self.profile = profile
        self.region = region
        self.failures: list[str] = []

    def _aws_base(self) -> list[str]:
        base = ["aws"]
        if self.profile:
            base += ["--profile", self.profile]
        base += ["--region", self.region]
        return base

    def aws(self, args: list[str], capture: bool = False, check: bool = True):
        return self.run(self._aws_base() + args, capture=capture, check=check)

    def aws_step(self, label: str, args: list[str]) -> bool:
        """Run a mutating aws command, recording (not raising) on failure."""
        return self.step(label, self._aws_base() + args)

    def step(self, label: str, cmd: list[str]) -> bool:
        """Run a mutating command; on non-zero exit, record the failure under
        ``label`` and return False (never raises). Returns True on success or
        under --dry-run."""
        result = self.run(cmd, capture=True, check=False)
        if result is None:  # dry-run
            return True
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            msg = detail[-1] if detail else f"exit {result.returncode}"
            self.failures.append(f"{label}: {msg}")
            print(f"    ⚠️  {label} failed: {msg}")
            return False
        return True

    def aws_json(self, args: list[str]):
        result = self.aws(args + ["--output", "json"], capture=True, check=False)
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def aws_exists(self, args: list[str]) -> bool | None:
        """Existence check for a get-* call that distinguishes a genuine
        'not found' from a transient/permission error. Returns True (exists),
        False (genuinely absent — NoSuchEntity/NotFound/ResourceNotFound), or
        None (couldn't tell: transient error or dry-run — caller should not
        blindly take the create path). check=False so a 404 isn't fatal."""
        result = self.aws(args + ["--output", "json"], capture=True, check=False)
        if result is None:  # dry-run
            return None
        if result.returncode == 0:
            return True
        err = ((result.stderr or "") + (result.stdout or "")).lower()
        if any(
            marker in err for marker in ("nosuchentity", "notfound", "does not exist")
        ):
            return False
        return None  # ambiguous — don't assume absent

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
        return input(f"{text}{suffix}: ").strip() or default
    except EOFError:
        return default


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
    raw = prompt(f"{text} ({'Y/n' if default else 'y/N'})").lower()
    return default if not raw else raw.startswith("y")


# --------------------------------------------------------------------------- #
# Bootstrap steps
# --------------------------------------------------------------------------- #


def choose_profile(cfg: dict, override: str | None) -> str | None:
    if override:
        return override
    profiles = list_profiles()
    if not profiles:
        print("  No named AWS profiles found. Using default credentials.")
        return None
    default = cfg.get("profile") if cfg.get("profile") in profiles else profiles[0]
    print("Select an AWS profile:")
    return prompt_choice("Profile", profiles, default)


def choose_agents(cfg: dict) -> list[str]:
    prev = cfg.get("agents", SHIPPING_AGENTS)
    print(f"Agents available: {', '.join(SHIPPING_AGENTS)}")
    raw = prompt("Which to set up (comma-separated, or 'all')", ",".join(prev))
    if raw.strip().lower() == "all":
        return list(SHIPPING_AGENTS)
    chosen = [a.strip() for a in raw.split(",") if a.strip() in SHIPPING_AGENTS]
    return chosen or list(SHIPPING_AGENTS)


def gather_config(cfg: dict) -> dict:
    print("\n== Configuration ==")
    cfg["stage"] = prompt_choice(
        "Stage", ["dev", "staging", "prod"], cfg.get("stage", "dev")
    )
    # The fleet is multi-repo and deploys as its own isolated repo: this is the
    # repo that HOSTS the fleet (its CI/OIDC trust + Actions secrets), NOT a repo
    # the agents act on. Which repos the agents act on is configured at runtime
    # by an admin in the dashboard (onboard repos), not baked here.
    cfg["target_repo"] = prompt(
        "Fleet's own GitHub repo (owner/repo) — for CI/OIDC, not a work target",
        cfg.get("target_repo", ""),
    )
    cfg["asana_project_gid"] = prompt(
        "Asana project GID (blank to skip)", cfg.get("asana_project_gid", "")
    )
    cfg["asana_workspace_gid"] = prompt(
        "Asana workspace GID (blank to skip)", cfg.get("asana_workspace_gid", "")
    )
    cfg["workitems_bot_gid"] = prompt(
        "Workitems bot Asana user GID (foundation stack)",
        cfg.get("workitems_bot_gid", ""),
    )
    cfg["agent_field_gid"] = prompt(
        "Asana 'Agent' custom-field GID (optional)", cfg.get("agent_field_gid", "")
    )
    cfg["deploy_dashboard"] = prompt_yes(
        "Deploy the monitoring dashboard?", cfg.get("deploy_dashboard", False)
    )
    # The admin API owns the Gateway Cedar-policy sync, so the gateway needs the
    # dashboard. Only offer it when the dashboard is on.
    if cfg.get("deploy_dashboard"):
        cfg["deploy_gateway"] = prompt_yes(
            "Deploy the AgentCore Gateway + Cedar policy engine (deterministic "
            "tool-call boundary)?",
            cfg.get("deploy_gateway", False),
        )
        if cfg.get("deploy_gateway"):
            cfg["gateway_enforcement"] = prompt_choice(
                "Gateway policy enforcement (roll out LOG_ONLY first)",
                ["LOG_ONLY", "ACTIVE"],
                cfg.get("gateway_enforcement", "LOG_ONLY"),
            )
    else:
        cfg["deploy_gateway"] = False
    # Which repos the fleet may act on. Onboarding normally happens in the admin
    # UI, but with the dashboard off there is no UI — so seed at least one repo
    # here, or every GitHub mention is rejected as "not onboarded" with no
    # in-band remedy. Comma-separated owner/repo list; optional when the
    # dashboard is on (onboard later in the UI).
    default_repos = ",".join(cfg.get("initial_repos", []) or [])
    prompt_label = "Initial repos to onboard (comma-separated owner/repo)"
    if not cfg.get("deploy_dashboard"):
        prompt_label += " [required — no admin UI to onboard later]"
    raw_repos = prompt(prompt_label, default_repos)
    cfg["initial_repos"] = [r.strip() for r in raw_repos.split(",") if r.strip()]
    return cfg


def ensure_oidc_provider(runner: Runner, account: str) -> None:
    print("\n== GitHub OIDC provider ==")
    arn = f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"
    exists = runner.aws_exists(
        ["iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", arn]
    )
    if exists:
        print("  OIDC provider: exists (skip create)")
        return
    if exists is None and not runner.dry_run:
        # Ambiguous (transient/permission error) — don't blindly create; record.
        runner.failures.append("oidc-provider: could not determine if it exists")
        print(
            "    ⚠️  could not check the OIDC provider — skipping create to avoid a dup"
        )
        return
    runner.aws_step(
        "create OIDC provider",
        [
            "iam",
            "create-open-id-connect-provider",
            "--url",
            GITHUB_OIDC_URL,
            "--client-id-list",
            "sts.amazonaws.com",
            "--thumbprint-list",
            GITHUB_OIDC_THUMBPRINT,
        ],
    )


def ensure_deploy_role(
    runner: Runner, account: str, region: str, owner: str, repo: str
) -> str | None:
    """Create/update the CI deploy role and return its ARN — or None if we
    couldn't confirm the role actually exists afterward (so a fabricated ARN is
    never handed on to the GitHub secret)."""
    print("\n== CI deploy role ==")
    trust = deploy_role_trust(account, owner, repo)
    exists = runner.aws_exists(["iam", "get-role", "--role-name", DEPLOY_ROLE_NAME])
    if exists is None and not runner.dry_run:
        runner.failures.append(
            f"deploy role: could not check whether {DEPLOY_ROLE_NAME} exists"
        )
        print(
            "    ⚠️  could not check the deploy role — skipping to avoid a wrong-branch write"
        )
        return None
    if exists:
        print(f"  role {DEPLOY_ROLE_NAME}: exists — updating trust policy to this repo")
        runner.aws_step(
            "update deploy-role trust",
            [
                "iam",
                "update-assume-role-policy",
                "--role-name",
                DEPLOY_ROLE_NAME,
                "--policy-document",
                json.dumps(trust),
            ],
        )
    else:
        runner.aws_step(
            "create deploy role",
            [
                "iam",
                "create-role",
                "--role-name",
                DEPLOY_ROLE_NAME,
                "--assume-role-policy-document",
                json.dumps(trust),
                "--description",
                "Assumed by GitHub Actions via OIDC to deploy the SDLC agent fleet",
            ],
        )
    # Scoped deploy policy (not AdministratorAccess) — only what the CI deploy
    # workflows actually do. See deploy_role_policy() for the rationale.
    runner.aws_step(
        "attach deploy-role policy",
        [
            "iam",
            "put-role-policy",
            "--role-name",
            DEPLOY_ROLE_NAME,
            "--policy-name",
            "sdlc-agents-deploy",
            "--policy-document",
            json.dumps(deploy_role_policy(region, account)),
        ],
    )
    # Verify the role really exists before handing its ARN to the GitHub secret,
    # so a failed create above can't publish an ARN CI will fail to assume.
    if runner.dry_run:
        return f"arn:aws:iam::{account}:role/{DEPLOY_ROLE_NAME}"
    confirmed = runner.aws_json(["iam", "get-role", "--role-name", DEPLOY_ROLE_NAME])
    if not confirmed:
        runner.failures.append(
            f"deploy role {DEPLOY_ROLE_NAME} was not created — CI will have no role to assume"
        )
        return None
    return confirmed["Role"]["Arn"]


def ensure_agent_roles(
    runner: Runner, agents: list[str], region: str, account: str, stage: str
) -> None:
    print("\n== Per-agent runtime IAM roles ==")
    for agent in agents:
        role = f"{agent}-agentcore-runtime"
        exists = runner.aws_exists(["iam", "get-role", "--role-name", role])
        if exists is None and not runner.dry_run:
            runner.failures.append(f"{role}: could not check whether it exists")
            print(
                f"    ⚠️  could not check {role} — skipping to avoid a wrong-branch write"
            )
            continue
        if exists:
            print(f"  role {role}: exists (updating policies)")
        else:
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
            runner.aws_step(
                f"create role {role}",
                [
                    "iam",
                    "create-role",
                    "--role-name",
                    role,
                    "--assume-role-policy-document",
                    json.dumps(trust),
                    "--description",
                    f"AgentCore runtime role for {agent}",
                ],
            )
        runner.aws_step(
            f"attach BedrockFullAccess to {role}",
            [
                "iam",
                "attach-role-policy",
                "--role-name",
                role,
                "--policy-arn",
                "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
            ],
        )
        for name, doc in agent_role_policies(agent, region, account, stage).items():
            runner.aws_step(
                f"put {name} on {role}",
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
            )


def deploy_foundation(runner: Runner, cfg: dict) -> None:
    print("\n== Foundation stack (sam) ==")
    stack = f"sdlc-agents-{cfg['stage']}"
    runner.run(["sam", "build"], cwd=FOUNDATION_DIR)
    cmd = [
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
        f"DeployGateway={'true' if cfg.get('deploy_gateway') else 'false'}",
        f"GatewayPolicyEnforcement={cfg.get('gateway_enforcement', 'LOG_ONLY')}",
    ]
    if runner.profile:
        cmd += ["--profile", runner.profile]
    runner.run(cmd, cwd=FOUNDATION_DIR)


def stack_outputs(runner: Runner, stack: str) -> dict[str, str]:
    data = runner.aws_json(["cloudformation", "describe-stacks", "--stack-name", stack])
    if not data:
        return {}
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in data.get("Stacks", [{}])[0].get("Outputs", [])
    }


def seed_fleet_repos(runner: Runner, cfg: dict) -> None:
    """Onboard the initial repos directly into the fleet-config table.

    Onboarding is normally an admin-UI action, but with the dashboard off there
    is no UI, and an empty table makes the Dispatch Router reject every GitHub
    mention as 'not onboarded'. Seeding here gives an in-band path. Repos are
    written enabled + eligible + active (the same row shape config_store.put_repo
    writes and fleet_config reads), lowercased to match the store's canonical
    form. When the Gateway is enabled, the admin API's later policy sync will
    fold these into the Cedar policy; standalone (no gateway) they only affect
    dispatch, which is exactly what a dashboard-less deploy needs."""
    repos = cfg.get("initial_repos") or []
    if not repos:
        if not cfg.get("deploy_dashboard"):
            runner.failures.append(
                "no initial repos seeded and dashboard is off — every GitHub "
                "mention will be rejected until repos are onboarded"
            )
            print(
                "    ⚠️  no repos onboarded and no dashboard to onboard them later"
            )
        return
    print("\n== Seed onboarded repos (fleet-config table) ==")
    table = f"fleet-config-{cfg['stage']}"
    for repo in repos:
        norm = repo.strip().casefold()
        if norm.count("/") != 1 or not all(norm.split("/")):
            runner.failures.append(f"skipped invalid initial repo '{repo}'")
            print(f"    ⚠️  '{repo}' is not owner/repo — skipped")
            continue
        item = json.dumps(
            {
                "pk": {"S": f"repo#{norm}"},
                "kind": {"S": "repo"},
                "repo": {"S": norm},
                "enabled": {"BOOL": True},
                "multi_repo_eligible": {"BOOL": True},
                "onboarded_by": {"S": "bootstrap"},
                "onboarded_at": {"N": "0"},
                "status": {"S": "active"},
            }
        )
        runner.aws_step(
            f"seed repo {norm}",
            ["dynamodb", "put-item", "--table-name", table, "--item", item],
        )


def set_github_config(
    runner: Runner, cfg: dict, account: str, deploy_role_arn: str | None
) -> None:
    """Set the Actions secrets + variables the deploy workflows read, via gh."""
    print("\n== GitHub Actions secrets + variables ==")
    repo = cfg["target_repo"]
    if not repo:
        print("  (no target repo set — skipping; set secrets/vars manually)")
        return
    if not deploy_role_arn and not runner.dry_run:
        # The deploy role wasn't confirmed (see ensure_deploy_role) — do NOT
        # write a role ARN CI can't assume. Record and skip the ARN secret.
        runner.failures.append(
            "skipped AWS_DEPLOY_ROLE_ARN secret — deploy role was not confirmed"
        )
        print("    ⚠️  deploy role not confirmed — not setting AWS_DEPLOY_ROLE_ARN")
    secrets = {"AWS_ACCOUNT_ID": account}
    if deploy_role_arn:
        secrets["AWS_DEPLOY_ROLE_ARN"] = deploy_role_arn
    # TARGET_REPO is intentionally NOT set: the fleet is multi-repo, so agents
    # take the repo from the dispatch (an admin onboards repos in the dashboard),
    # not from a baked deploy variable.
    variables = {"AWS_REGION": runner.region}
    if cfg.get("asana_project_gid"):
        variables["ASANA_PROJECT_GID"] = cfg["asana_project_gid"]
    if cfg.get("asana_workspace_gid"):
        variables["ASANA_WORKSPACE_GID"] = cfg["asana_workspace_gid"]
    for key, val in secrets.items():
        runner.step(
            f"gh secret {key}",
            ["gh", "secret", "set", key, "--repo", repo, "--body", val],
        )
    for key, val in variables.items():
        runner.step(
            f"gh variable {key}",
            ["gh", "variable", "set", key, "--repo", repo, "--body", val],
        )


def check_secrets(runner: Runner, agents: list[str]) -> None:
    print("\n== SSM secret preflight ==")
    needed: set[str] = set()
    for agent in agents:
        needed.update(AGENT_REQUIRED_SSM.get(agent, []))
    missing = []
    for suffix in sorted(needed):
        name = f"/sdlc-agents/{suffix}"
        if runner.aws_json(["ssm", "get-parameter", "--name", name]):
            print(f"  ✅ {name}")
        else:
            missing.append(name)
            print(f"  ❌ {name} — not set")
    if missing:
        print(
            "\n  Agents will fail at invocation without these. Populate them (this script "
            "does not handle secrets):"
        )
        print(
            "    Asana:  python scripts/bootstrap_asana_oauth.py  (+ asana-pat / MCP client creds)"
        )
        print(
            "    GitHub: set /sdlc-agents/github-mcp-token (PAT) or the GitHub App params"
        )
        print("    Tavily: set /sdlc-agents/researcher-tavily-api-key")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


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

    print("SDLC Agent Fleet — interactive bootstrap\n")

    if not preflight_tools():
        print("\nInstall/authenticate the tool(s) above, then re-run.")
        return 1

    cfg = load_config()
    profile = choose_profile(cfg, args.profile)
    cfg["profile"] = profile
    region = args.region or prompt("AWS region", cfg.get("region", "us-west-2"))
    cfg["region"] = region

    runner = Runner(dry_run=args.dry_run, profile=profile, region=region)

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

    if "/" not in (cfg.get("target_repo") or ""):
        print(
            "\n  Target repo must be 'owner/repo' to configure OIDC trust + GitHub secrets."
        )
        if not args.dry_run:
            return 1
        owner, repo = "OWNER", "REPO"
    else:
        owner, repo = cfg["target_repo"].split("/", 1)

    print("\n== Plan ==")
    print(f"  Account:   {account}")
    print(f"  Region:    {region}")
    print(f"  Stage:     {cfg['stage']}")
    print(f"  Repo:      {owner}/{repo}")
    print(f"  Dashboard: {'yes' if cfg.get('deploy_dashboard') else 'no'}")
    gw = "no"
    if cfg.get("deploy_gateway"):
        gw = f"yes ({cfg.get('gateway_enforcement', 'LOG_ONLY')})"
    print(f"  Gateway:   {gw}")
    repos = cfg.get("initial_repos") or []
    print(f"  Repos:     {', '.join(repos) if repos else '(none — onboard in the UI)'}")
    print(f"  Agents:    {', '.join(agents)}")
    print(
        "  Sets up: OIDC provider, CI deploy role, per-agent runtime roles, "
        "foundation stack, GitHub secrets/vars."
    )
    print("  Then: push to main → CI (deploy-agent.yml) builds + deploys the agents.")

    if not args.dry_run and not prompt_yes("\nProceed with bootstrap?", default=False):
        print("Aborted.")
        return 0

    ensure_oidc_provider(runner, account)
    deploy_role_arn = ensure_deploy_role(runner, account, region, owner, repo)
    ensure_agent_roles(runner, agents, region, account, cfg["stage"])
    deploy_foundation(runner, cfg)
    outputs = stack_outputs(runner, f"sdlc-agents-{cfg['stage']}")
    seed_fleet_repos(runner, cfg)
    set_github_config(runner, cfg, account, deploy_role_arn)
    check_secrets(runner, agents)

    # A swallowed IAM/gh failure must not read as success — surface them and
    # exit non-zero so the operator fixes the setup before pushing to CI.
    if runner.failures:
        print(f"\n== Bootstrap finished with {len(runner.failures)} problem(s) ==")
        for f in runner.failures:
            print(f"  ✗ {f}")
        print(
            "\nThe setup is incomplete — resolve the above (often a permissions or gh-scope\n"
            "issue) and re-run (the script is idempotent). Do NOT push to CI yet."
        )
        return 1

    print("\n== Bootstrap complete — hand off to CI ==")
    print("  1. Populate any missing SSM secrets (see above).")
    print(
        "  2. Commit + push to `main` — the per-agent deploy-*.yml workflows build each"
    )
    print("     image, create/update its AgentCore Runtime, and sync the registry.")
    print(f"  3. Watch the run:  gh run watch --repo {owner}/{repo}")
    if outputs.get("WebhookEndpoint"):
        print(f"  4. Register the Asana webhook: {outputs['WebhookEndpoint']}")
        print("     python scripts/bootstrap_asana_webhook.py")
    if cfg.get("deploy_dashboard") and outputs.get("DashboardUrl"):
        print(
            f"  5. Dashboard: {outputs['DashboardUrl']}"
        )
        print(
            "     Add viewers to the Cognito 'operators' group and fleet admins to"
        )
        print(
            "     'admins'. An admin then onboards the repos the fleet may act on"
        )
        print(
            "     in the Admin view — the fleet is multi-repo, so no repo is baked in."
        )
    if cfg.get("deploy_gateway") and outputs.get("FleetGatewayUrl"):
        print(
            f"  6. Gateway: set GATEWAY_MCP_URL={outputs['FleetGatewayUrl']} on the agent"
        )
        print(
            "     runtimes to route tool calls through the policy engine. Enforcement"
        )
        print(
            f"     is {cfg.get('gateway_enforcement', 'LOG_ONLY')}; author the per-agent"
        )
        print(
            "     permit policies + confirm the write-tool names, then flip to ACTIVE."
        )
        print("     See docs/aws-deploy.md § AgentCore Gateway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
