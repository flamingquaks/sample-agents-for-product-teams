"""Deploy the fleet BASE platform — foundation stack + build source + dashboard.

This is the one-command deploy for the parts that genuinely require a build host
and privileged, one-time setup. Agents themselves are NO LONGER deployed here:
onboarding an agent is a self-service action in the Admin dashboard, which builds
its container (shared CodeBuild pipeline) and stands up its AgentCore runtime
(capability_deployer Lambda) — no per-agent script or CI wrapper. This deployer
just stands up the platform those UI actions run on:

  1. Foundation stack (sam) — DynamoDB, Lambdas, guardrail, Cognito + dashboard
     API/CDN, the shared build pipeline + capability deployer/rebuilder, and
     (opt-in) the AgentCore Gateway + Cedar policy engine. Preserves the target
     stack's existing parameter values by default.
  2. Build source — zip agents/ and upload it as source.zip to the pipeline's
     source bucket, so the first dashboard onboard has something to build. Re-run
     whenever agent code changes (the weekly rebuild also builds this source).
  3. Dashboard SPA — build, write config.json from stack outputs, S3 sync +
     CloudFront invalidation.

Idempotent: safe to re-run. ``--dry-run`` prints every command without executing.

Usage:
    python scripts/deploy_fleet.py --stage staging --region us-east-1
    python scripts/deploy_fleet.py --stage staging --dry-run
    python scripts/deploy_fleet.py --skip-dashboard
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3

logger = logging.getLogger("deploy_fleet")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

REPO_ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_DIR = REPO_ROOT / "infra" / "foundation"
AGENTS_DIR = REPO_ROOT / "agents"
DASHBOARD_DIR = REPO_ROOT / "dashboard"

# Dirs to leave out of the build source zip (caches / build junk) so the image
# build context the pipeline uses stays lean.
_SOURCE_EXCLUDE = {"__pycache__", ".pytest_cache", "build", "node_modules"}


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


def _describe_stack(region: str, stack: str) -> dict | None:
    """The stack's describe-stacks record, or None if it genuinely does not
    exist yet. A "does not exist" ValidationError is the expected new-stack
    signal; ANY other ClientError (throttling, AccessDenied) is re-raised — we
    must NOT mistake a transient/permission failure on a live stack for a
    brand-new one, or deploy_foundation would silently redeploy it with
    template defaults and tear down the gateway/dashboard."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    except cfn.exceptions.ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    return stacks[0] if stacks else None


def _stack_params(stack: dict | None) -> dict[str, str]:
    if not stack:
        return {}
    return {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters", [])}


def _stack_outputs(stack: dict | None) -> dict[str, str]:
    if not stack:
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def deploy_foundation(
    runner: Runner,
    stage: str,
    region: str,
    auto_approve: bool,
    param_overrides: dict[str, str] | None = None,
) -> None:
    stack = f"sdlc-agents-{stage}"
    logger.info("== Foundation stack (%s) ==", stack)
    # Preserve the parameter values already on the stack (DeployGateway,
    # DeployDashboard, the Asana GIDs an operator set, etc.) so a redeploy is a
    # code/template update, not a silent reconfiguration. For a brand-new stack
    # there are none, and the template defaults apply.
    existing = _stack_params(_describe_stack(region, stack))
    # Effective parameters, precedence low→high:
    #   1. Stage (always set from --stage),
    #   2. the values already on the stack (preserve a redeploy's config),
    #   3. explicit --param / --full overrides from THIS invocation (so one
    #      command can stand the whole stack up, or flip a toggle on redeploy).
    # A NEW stack starts from just Stage, so passing DeployDashboard=true etc.
    # here is what makes a single first-run deploy the FULL solution rather than
    # the dashboard/gateway-off template defaults.
    params: dict[str, str] = {"Stage": stage}
    params.update(existing)
    params.update(param_overrides or {})
    if not existing:
        logger.info(
            "new stack %s — deploying with: %s",
            stack, ", ".join(f"{k}={v}" for k, v in sorted(params.items())),
        )
    else:
        logger.info(
            "existing stack %s — %d preserved params, %d override(s) this run",
            stack, len(existing), len(param_overrides or {}),
        )
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
    # Use the explicit ParameterKey=/ParameterValue= form so an empty value
    # (e.g. an unset Asana GID) is passed literally rather than being a parse
    # error in sam's shorthand ``KEY=VALUE`` splitter.
    overrides = [
        f"ParameterKey={k},ParameterValue={v}" for k, v in sorted(params.items())
    ]
    cmd += ["--parameter-overrides", *overrides]
    runner.run(cmd, cwd=FOUNDATION_DIR)


# --- build source ------------------------------------------------------------


def upload_build_source(runner: Runner, outputs: dict[str, str]) -> None:
    """Zip agents/ and upload it as source.zip to the capability build pipeline's
    source bucket. This is what the shared CodeBuild project unpacks and builds
    when an agent is onboarded from the dashboard (and what the weekly security
    rebuild rebuilds), so it must exist before the first onboard. Re-run whenever
    agent code changes.

    No-op when the dashboard/pipeline isn't deployed (no source bucket output)."""
    bucket = outputs.get("CapabilitySourceBucketName")
    if not bucket:
        logger.info("no build source bucket on the stack (dashboard off) — skipping source upload")
        return
    logger.info("== Build source → s3://%s/source.zip ==", bucket)
    if not AGENTS_DIR.is_dir():
        raise DeployError(f"{AGENTS_DIR} not found — cannot upload build source")
    if runner.dry_run:
        logger.info("DRY-RUN zip agents/ and upload to s3://%s/source.zip", bucket)
        return
    # Zip agents/ so the archive root contains agents/<name>/... — the buildspec
    # builds `docker build -f agents/$AGENT_NAME/Dockerfile agents/`.
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in AGENTS_DIR.rglob("*"):
                if any(part in _SOURCE_EXCLUDE for part in path.parts):
                    continue
                if path.is_file():
                    zf.write(path, path.relative_to(REPO_ROOT))
        runner.run(["aws", "s3", "cp", str(archive), f"s3://{bucket}/source.zip"])
    logger.info("uploaded build source")


# --- built-in capability seeding ---------------------------------------------

# The 5 repo-resident system agents. Seeded as `builtin` capability rows so they
# appear in the dashboard as fixed, enable/disable-only agents (spec §3.1). Config
# here is DECLARATIVE metadata only (aliases/triggers/description) — the actual
# behavior is the code under agents/<id>/. Aliases mirror CLAUDE.md's agent table.
# `triggers` is registry metadata (the router doesn't gate on it today), seeded to
# the sources each agent is designed for.
_BUILTIN_AGENTS = {
    "workitems": {
        "description": "PO/PM — decomposition, status, risk, sync",
        "aliases": ["pm", "status", "plan"],
        "triggers": {"github": ["comment_mention"], "asana": ["assignment", "comment_mention"]},
    },
    "researcher": {
        "description": "BA — research, competitive intel, backlog",
        "aliases": ["ba", "research", "analyze"],
        "triggers": {"asana": ["assignment", "comment_mention"]},
    },
    "docwriter": {
        "description": "Tech writer — API docs, guides, release notes",
        "aliases": ["docs", "doc", "writer"],
        "triggers": {"github": ["comment_mention"]},
    },
    "adr": {
        "description": "ADR linker — tags issues, reviews PRs vs the ADR library",
        "aliases": ["decisions", "architecture"],
        "triggers": {"github": ["comment_mention"]},
    },
    "reviewer": {
        "description": "Code reviewer — inline PR findings (correctness, safety, soundness)",
        "aliases": ["review", "cr"],
        "triggers": {"github": ["comment_mention", "pull_request"]},
    },
}


def seed_builtin_capabilities(runner: Runner, outputs: dict[str, str]) -> None:
    """Idempotently seed a `builtin` capability row for each system agent so the
    dashboard lists them as fixed, enable/disable-only agents (spec §3.1, §8.3).

    Idempotent + non-destructive: an already-seeded (or already-ENABLED/active)
    built-in is left untouched except for its declarative metadata — we NEVER
    reset `enabled`/`status`/deploy-state, so re-running the deployer can't knock a
    live built-in out of the registry. A brand-new row is written disabled +
    pending (`enabled:false`, `status:disabled`) so enabling it in the UI is the
    explicit first deploy.

    No-op when the dashboard/config table isn't deployed (no table output)."""
    table_name = outputs.get("FleetConfigTableName")
    if not table_name:
        logger.info("no fleet-config table on the stack (dashboard off) — skipping built-in seed")
        return
    logger.info("== Seed built-in capabilities → %s ==", table_name)
    if runner.dry_run:
        for agent_id in _BUILTIN_AGENTS:
            logger.info("DRY-RUN seed built-in capability %s", agent_id)
        return
    table = boto3.resource("dynamodb").Table(table_name)
    now = int(time.time())
    for agent_id, meta in _BUILTIN_AGENTS.items():
        existing = table.get_item(Key={"pk": f"capability#{agent_id}"}).get("Item") or {}
        item = {
            "pk": f"capability#{agent_id}",
            "kind": "capability",
            "agent_id": agent_id,
            "description": meta["description"],
            "aliases": meta["aliases"],
            "triggers": meta["triggers"],
            "limits": existing.get("limits", {}),
            "env": existing.get("env", {}),
            "builtin": True,
            # Preserve lifecycle + deploy state on an existing row; a NEW row is
            # born disabled so enabling it in the UI is the explicit first deploy.
            "enabled": bool(existing.get("enabled", False)),
            "status": existing.get("status", "disabled"),
            "onboarded_by": existing.get("onboarded_by", "system-seed"),
            "onboarded_at": existing.get("onboarded_at", now),
            "updated_at": now,
        }
        for k in ("image_tag", "runtime_arn", "build_id", "status_detail"):
            if k in existing:
                item[k] = existing[k]
        table.put_item(Item=item)
        logger.info("seeded built-in capability %s (enabled=%s)", agent_id, item["enabled"])


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


# --- Forge forwarder (atlassian-connector spec §A5) ---------------------------


def deploy_forge_forwarder(stage: str, region: str, dry_run: bool) -> None:
    """Deploy the atlassian-events Forge forwarder so Jira/Confluence are fully
    connectable from the admin app with no separate manual asset deploy. The
    forwarder is site-agnostic (cloud id derived per invocation), so this runs
    once per stage; connecting a new site later needs only the in-dashboard
    install link. Gracefully skipped — with the exact command to run — when the
    Forge CLI isn't installed/authenticated, since Forge auth is interactive and
    can't be automated here."""
    logger.info("== Atlassian events forwarder (Forge) ==")
    import deploy_forge_atlassian as forge

    if not forge.forge_cli_ready(dry_run):
        logger.warning(
            "Forge CLI not available or not logged in — skipping the forwarder. "
            "Jira/Confluence site CONNECT still works in the dashboard, but no "
            "events flow until an operator runs:\n"
            "    npm i -g @forge/cli && forge login\n"
            "    python scripts/deploy_forge_atlassian.py --stage %s --region %s",
            stage, region,
        )
        return
    try:
        forge.deploy(stage, region, dry_run=dry_run)
    except (SystemExit, subprocess.CalledProcessError) as exc:
        # The fleet deploy must not fail on the one non-AWS artifact; surface the
        # fix and continue (receivers stay inert until delivery works anyway).
        logger.warning(
            "Forge forwarder deploy failed (%s). Re-run it directly:\n"
            "    python scripts/deploy_forge_atlassian.py --stage %s --region %s",
            exc, stage, region,
        )


# --- orchestration -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deploy the fleet base platform.")
    ap.add_argument("--stage", default="staging", choices=["dev", "staging", "prod"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--skip-foundation", action="store_true")
    ap.add_argument("--skip-source", action="store_true",
                    help="skip uploading the agent build source (agents/ unchanged)")
    ap.add_argument("--skip-dashboard", action="store_true")
    ap.add_argument("--skip-forge", action="store_true",
                    help="skip deploying the atlassian-events Forge forwarder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--auto-approve", action="store_true",
        help="skip the sam changeset confirmation prompt (unattended runs); by "
             "default the foundation changeset is printed and must be confirmed",
    )
    ap.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE",
        help="foundation stack parameter override (repeatable), e.g. "
             "--param DeployDashboard=true --param GatewayPolicyEnforcement=ACTIVE. "
             "On a new stack these merge with Stage; on a redeploy they override "
             "the preserved value for that key.",
    )
    ap.add_argument(
        "--full", action="store_true",
        help="convenience: deploy the FULL solution — dashboard + gateway on "
             "(GatewayPolicyEnforcement=LOG_ONLY, approval gate on, Mantle "
             "project on). Equivalent to the matching --param flags; any "
             "explicit --param wins over these defaults.",
    )
    args = ap.parse_args(argv)

    # Assemble the foundation parameter overrides for this invocation. --full
    # sets the full-solution baseline; explicit --param entries override it.
    param_overrides: dict[str, str] = {}
    if args.full:
        param_overrides.update({
            "DeployDashboard": "true",
            "DeployGateway": "true",
            "GatewayPolicyEnforcement": "LOG_ONLY",
            "DeployMantleProject": "true",
            "RequireAgentApproval": "true",
            # Atlassian gateway targets: the Jira/Confluence tool grants are
            # merged (fleet_policy.AGENT_TOOL_GRANTS), so the full solution
            # ships them — sites stay inert until connected in the dashboard.
            "DeployJiraTarget": "true",
            "DeployConfluenceTarget": "true",
        })
    for entry in args.param:
        if "=" not in entry:
            ap.error(f"--param must be KEY=VALUE (got {entry!r})")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            ap.error(f"--param key must be non-empty (got {entry!r})")
        param_overrides[key] = value
    # DeployGateway=true requires DeployDashboard=true (template Rule
    # GatewayRequiresDashboard); catch it here so the deploy fails fast at the
    # CLI rather than mid-changeset.
    if param_overrides.get("DeployGateway") == "true" and \
            param_overrides.get("DeployDashboard") != "true":
        ap.error(
            "DeployGateway=true requires DeployDashboard=true (the admin API "
            "owns the Gateway Cedar policy sync) — pass --param DeployDashboard=true "
            "or use --full"
        )

    runner = Runner(args.dry_run)
    stack = f"sdlc-agents-{args.stage}"
    logger.info(
        "Deploying fleet base: stage=%s region=%s%s",
        args.stage, args.region, " [DRY-RUN]" if args.dry_run else "",
    )

    if not args.skip_foundation:
        deploy_foundation(
            runner, args.stage, args.region, args.auto_approve, param_overrides
        )
    elif param_overrides:
        logger.warning(
            "--param/--full given with --skip-foundation — parameter overrides "
            "are ignored (the foundation stack is not being deployed this run)"
        )

    # Resolve outputs once the foundation exists (source upload + dashboard need them).
    if args.dry_run:
        outputs = {
            "CapabilitySourceBucketName": "<source-bucket>",
            "FleetConfigTableName": "<fleet-config-table>",
            "DashboardSiteBucketName": "<bucket>", "DashboardDistributionId": "<dist>",
            "DashboardApiEndpoint": "<api>", "DashboardUserPoolId": "<pool>",
            "DashboardUserPoolClientId": "<client>", "DashboardLoginDomain": "<login>",
            "DashboardUrl": "<url>",
        }
    else:
        outputs = _stack_outputs(_describe_stack(args.region, stack))

    if not args.skip_source:
        upload_build_source(runner, outputs)
        # After the source that defines them exists, seed the built-in agents so
        # they appear in the dashboard as fixed, enable/disable-only capabilities.
        seed_builtin_capabilities(runner, outputs)

    if not args.skip_dashboard:
        deploy_dashboard(runner, args.region, outputs)

    if not args.skip_forge:
        deploy_forge_forwarder(args.stage, args.region, args.dry_run)

    logger.info("Fleet base deploy complete%s. Onboard agents in the dashboard Admin view.",
                " (dry-run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
