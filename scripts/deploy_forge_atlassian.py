"""Deploy the shared ``atlassian-events`` Forge forwarder (atlassian-connector
spec §A5).

Wraps ``forge deploy`` + environment wiring so the forwarder ships ONCE per
fleet stage — NOT per site. The app derives each event's site (cloud id) from
the Forge invocation context, so a single deployment serves every site an admin
later connects in the dashboard: connecting a new site needs no re-deploy, only
installing the app on that site from its private install link (surfaced on the
dashboard's Connectors → Atlassian page). This is the ONE Forge artifact outside
the SAM stack; everything else ships in the foundation template and is inert
until a site is onboarded.

What it does:
  1. Resolve the fleet webhook base URL from the foundation stack outputs
     (``JiraWebhookEndpoint`` → strip the ``/jira/webhook/{site}`` suffix).
  2. ``forge variables set`` FLEET_WEBHOOK_BASE for the stage's environment.
  3. ``forge deploy`` to that environment.
  4. Record the app id + install link in SSM
     (``/sdlc-agents/<stage>/atlassian/forge-app-id`` / ``forge-install-link``)
     so the admin UI can show the install card without operator hand-off.

Runs automatically from ``scripts/deploy_fleet.py`` when the Forge CLI is
available and authenticated; run it directly to (re)deploy just the forwarder.

Idempotent. ``--dry-run`` prints every command without executing. Requires the
Forge CLI (``npm i -g @forge/cli``) authenticated to the fleet's Atlassian
developer account (``forge login``); this script never touches Atlassian auth
itself. On FIRST deploy the CLI registers the app (``forge register``) if the
manifest still carries the REPLACE_AT_DEPLOY placeholder id.

Usage:
    python scripts/deploy_forge_atlassian.py --stage dev --region us-east-1
    python scripts/deploy_forge_atlassian.py --stage dev --dry-run
"""

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("deploy_forge_atlassian")

_APP_DIR = Path(__file__).resolve().parents[1] / "forge" / "atlassian-events"
_FORGE_ENV_BY_STAGE = {"dev": "development", "staging": "development",
                       "gamma": "staging", "prod": "production"}
_PLACEHOLDER_APP_ID = "REPLACE_AT_DEPLOY"


def _run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False,
         capture: bool = False, check: bool = True) -> str:
    printable = " ".join(cmd)
    logger.info("$ %s", printable)
    if dry_run:
        return ""
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check,
        text=True, capture_output=capture,
    )
    return (result.stdout or "").strip() if capture else ""


def _webhook_base(stage: str, region: str) -> str:
    """Resolve ``https://<api>/<stage>`` from the stack's JiraWebhookEndpoint
    output by stripping the ``/jira/webhook/{site}`` suffix."""
    import boto3

    cf = boto3.client("cloudformation", region_name=region)
    stacks = cf.describe_stacks(StackName=f"sdlc-agents-{stage}")["Stacks"]
    for out in stacks[0].get("Outputs", []):
        if out["OutputKey"] == "JiraWebhookEndpoint":
            return out["OutputValue"].replace("/jira/webhook/{site}", "")
    raise SystemExit(
        "JiraWebhookEndpoint not found in stack outputs — deploy the foundation "
        "stack first (scripts/deploy_fleet.py)."
    )


def forge_cli_ready(dry_run: bool = False) -> bool:
    """Whether the Forge CLI is installed AND logged in. deploy_fleet uses this
    to decide between running the deploy and printing the one manual step."""
    if dry_run:
        return True
    try:
        result = subprocess.run(
            ["forge", "whoami"], capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_registered(dry_run: bool) -> None:
    """First-deploy registration: if the manifest still carries the placeholder
    app id, ``forge register`` rewrites it with a real ari under the logged-in
    developer account. Subsequent deploys skip this (idempotent)."""
    manifest = _APP_DIR / "manifest.yml"
    if _PLACEHOLDER_APP_ID not in manifest.read_text():
        return
    logger.info("manifest carries the placeholder app id — registering the app")
    _run(["forge", "register", "sdlc-agents-atlassian-events", "--non-interactive"],
         cwd=_APP_DIR, dry_run=dry_run)


def _app_id() -> str:
    """The registered app ari from the manifest ('' if still the placeholder)."""
    m = re.search(r'id:\s*"?(ari:cloud:ecosystem::app/[^"\s]+)"?',
                  (_APP_DIR / "manifest.yml").read_text())
    ari = m.group(1) if m else ""
    return "" if _PLACEHOLDER_APP_ID in ari else ari


def _record_in_ssm(stage: str, region: str, forge_env: str, dry_run: bool) -> None:
    """Publish the app id + per-product install links to SSM so the admin API
    can render the install card (GET /admin/atlassian/forge-status) — the admin
    connects sites end-to-end in the UI with no operator hand-off."""
    app_id = _app_id()
    if not app_id:
        logger.warning("app id unresolved — skipping SSM publication")
        return
    # The generic installation URL: developer.atlassian.com's install flow for a
    # specific app+environment. Forge prints per-run links too, but this stable
    # form works for any site the admin points it at.
    install_link = (
        f"https://developer.atlassian.com/console/install/"
        f"{app_id.rsplit('/', 1)[-1]}?signature=none&product=jira&environment={forge_env}"
    )
    if dry_run:
        logger.info("DRY-RUN would write SSM forge-app-id=%s install-link=%s",
                    app_id, install_link)
        return
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    for name, value in (
        (f"/sdlc-agents/{stage}/atlassian/forge-app-id", app_id),
        (f"/sdlc-agents/{stage}/atlassian/forge-install-link", install_link),
    ):
        ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        logger.info("wrote %s", name)


def deploy(stage: str, region: str, *, webhook_base: str | None = None,
           forge_env: str | None = None, dry_run: bool = False) -> None:
    """The full forwarder deploy (importable — deploy_fleet.py calls this)."""
    if not _APP_DIR.exists():
        raise SystemExit(f"Forge app not found at {_APP_DIR}")
    webhook_base = webhook_base or _webhook_base(stage, region)
    env = forge_env or _FORGE_ENV_BY_STAGE.get(stage, "development")
    logger.info("Fleet webhook base: %s", webhook_base)
    logger.info("Forge environment:  %s", env)

    _run(["npm", "install"], cwd=_APP_DIR, dry_run=dry_run)
    _ensure_registered(dry_run)
    _run(["forge", "variables", "set", "--environment", env,
          "FLEET_WEBHOOK_BASE", webhook_base],
         cwd=_APP_DIR, dry_run=dry_run)
    _run(["forge", "deploy", "--environment", env, "--non-interactive"],
         cwd=_APP_DIR, dry_run=dry_run)
    _record_in_ssm(stage, region, env, dry_run)

    logger.info(
        "\n✅ Forwarder deployed for stage %s. Admins connect sites entirely in "
        "the dashboard (Connectors → Atlassian): the Sites tab shows the app "
        "install link; one install per site covers both products.",
        stage,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--webhook-base", help="override the resolved webhook base URL")
    parser.add_argument("--forge-env", help="override the Forge environment name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    deploy(args.stage, args.region, webhook_base=args.webhook_base,
           forge_env=args.forge_env, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
