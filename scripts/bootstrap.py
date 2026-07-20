#!/usr/bin/env python3
"""Interactive one-time BASE deploy for the SDLC Agent Fleet.

Stands up the shared base platform — and nothing per-agent. The fleet moved to
UI-driven agent onboarding: the dashboard builds each agent's container (via the
shared CodeBuild pipeline) and creates the per-agent runtime IAM role +
AgentCore runtime (the `capability_deployer` Lambda owns those, under IAM path
`/sdlc-agents/capabilities/`). So this script deliberately does NOT create any
per-agent runtime roles, OIDC providers, CI deploy roles, or GitHub Actions
secrets — that machinery has been retired.

What it sets up (all idempotent):

  1. Preflight — aws / sam installed (guidance if not).
  2. Pick an AWS profile + region; confirm the account.
  3. Foundation stack via `sam deploy` (DynamoDB, Dispatch Router, webhook API,
     SSM registry, guardrail, capability build pipeline — optionally the
     dashboard + AgentCore Gateway).
  4. Upload the agent source (agents/ tree) to the capability build pipeline's
     source bucket so the first UI onboard has something to build.
  5. Seed the initial onboarded repos (when there's no dashboard UI to do it).
  6. SSM secret preflight — points you at the bootstrap scripts for anything
     missing (this script does not write secrets itself).

It does NOT build images or create AgentCore runtimes — that's the dashboard's
job now. After this base deploy, open the dashboard, add operators/admins to the
Cognito groups, and onboard agents + repos from the Admin view.

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
REQUIRED_TOOLS = ("aws", "sam")


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
    }
    h = hints[tool]
    cmd = h["Darwin"] if mac else h["Linux"]
    return f"  {cmd}\n  docs: {h['doc']}"


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
# SSM secret preflight
# --------------------------------------------------------------------------- #

# Concrete leaf SSM params the secret preflight probes (agents fail at
# invocation, not deploy, when these are absent). GitHub agents authenticate via
# the App (app-id SSM param + private-key secret), not a PAT — those are seeded
# by the stack and populated by the admin manifest flow, so they're not probed as
# operator-set secrets here.
AGENT_REQUIRED_SSM: dict[str, list[str]] = {
    "workitems": [
        "asana-pat",
        "asana-mcp-client-id",
        "asana-mcp-client-secret",
        "asana-mcp-refresh-token",
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
    ],
    "adr": [],
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


def upload_build_source(runner: Runner, cfg: dict, outputs: dict[str, str]) -> None:
    """Upload the fleet source (the agents/ tree) to the capability build pipeline's
    source bucket as source.zip. This is what the shared CodeBuild project unpacks
    and builds when an agent is onboarded from the dashboard — so it must exist
    before the first onboard. Re-run whenever agent code changes so the pipeline
    (and the weekly security rebuild) build the current source.

    No-op when the dashboard isn't deployed (no build pipeline / no source bucket)."""
    bucket = outputs.get("CapabilitySourceBucketName")
    if not bucket:
        return
    print("\n== Upload agent source for the build pipeline ==")
    src = REPO_ROOT / "agents"
    if not src.is_dir():
        runner.failures.append("agents/ directory not found — cannot upload build source")
        print("    ⚠️  no agents/ directory to upload")
        return
    # Zip agents/ so the archive root contains agents/<name>/... — the buildspec
    # builds `docker build -f agents/$AGENT_NAME/Dockerfile agents/`.
    if runner.dry_run:
        print(f"  [dry-run] zip agents/ → source.zip and upload to s3://{bucket}/source.zip")
        return
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in src.rglob("*"):
                # Skip caches / build junk so the image build context stays lean.
                if any(part in {"__pycache__", ".pytest_cache", "build", "node_modules"}
                       for part in path.parts):
                    continue
                if path.is_file():
                    zf.write(path, path.relative_to(REPO_ROOT))
        runner.aws_step(
            "upload build source",
            ["s3", "cp", str(archive), f"s3://{bucket}/source.zip"],
        )


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
            "    GitHub: register the fleet GitHub App in the dashboard admin UI "
            "(populates the app-id SSM param + private-key secret)"
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

    print("\n== Plan ==")
    print(f"  Account:   {account}")
    print(f"  Region:    {region}")
    print(f"  Stage:     {cfg['stage']}")
    print(f"  Dashboard: {'yes' if cfg.get('deploy_dashboard') else 'no'}")
    gw = "no"
    if cfg.get("deploy_gateway"):
        gw = f"yes ({cfg.get('gateway_enforcement', 'LOG_ONLY')})"
    print(f"  Gateway:   {gw}")
    repos = cfg.get("initial_repos") or []
    print(f"  Repos:     {', '.join(repos) if repos else '(none — onboard in the UI)'}")
    print(
        "  Sets up: foundation stack (+ capability build pipeline, optional "
        "dashboard/gateway), uploads agent source, seeds onboarded repos."
    )
    print(
        "  Then: open the dashboard and onboard agents + repos — the dashboard "
        "builds each container and stands up its runtime."
    )

    if not args.dry_run and not prompt_yes("\nProceed with base deploy?", default=False):
        print("Aborted.")
        return 0

    deploy_foundation(runner, cfg)
    outputs = stack_outputs(runner, f"sdlc-agents-{cfg['stage']}")
    upload_build_source(runner, cfg, outputs)
    seed_fleet_repos(runner, cfg)
    check_secrets(runner, agents)

    # A swallowed AWS failure must not read as success — surface them and exit
    # non-zero so the operator fixes the setup before onboarding agents.
    if runner.failures:
        print(f"\n== Base deploy finished with {len(runner.failures)} problem(s) ==")
        for f in runner.failures:
            print(f"  ✗ {f}")
        print(
            "\nThe setup is incomplete — resolve the above (often a permissions issue)\n"
            "and re-run (the script is idempotent)."
        )
        return 1

    print("\n== Base deploy complete — onboard agents in the dashboard ==")
    print("  1. Populate any missing SSM secrets (see above).")
    if cfg.get("deploy_dashboard") and outputs.get("DashboardUrl"):
        print(f"  2. Open the dashboard: {outputs['DashboardUrl']}")
        print(
            "     Add users to the Cognito 'operators' (viewers) and 'admins' groups."
        )
        print(
            "     Then, in the Admin view, onboard agents + the repos the fleet may"
        )
        print(
            "     act on — the dashboard builds each agent's container (shared"
        )
        print(
            "     CodeBuild pipeline) and stands up its per-agent runtime IAM role +"
        )
        print("     AgentCore runtime. No agent is baked in by this script.")
    else:
        print(
            "  2. The dashboard is not deployed — re-run with the dashboard enabled to"
        )
        print(
            "     get the Admin UI for onboarding agents + repos (the fleet's build +"
        )
        print("     runtime lifecycle is UI-driven).")
    if outputs.get("WebhookEndpoint"):
        print(f"  3. Register the Asana webhook: {outputs['WebhookEndpoint']}")
        print("     python scripts/bootstrap_asana_webhook.py")
    if cfg.get("deploy_gateway") and outputs.get("FleetGatewayUrl"):
        print(
            f"  4. Gateway: set GATEWAY_MCP_URL={outputs['FleetGatewayUrl']} on the agent"
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
