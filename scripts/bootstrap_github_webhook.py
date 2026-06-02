#!/usr/bin/env python3
"""One-shot GitHub webhook registration for the SDLC Agent Fleet.

Unlike Asana (which generates the shared secret itself during a handshake and
POSTs it to the receiver), GitHub lets the *creator* of a webhook choose the
secret. So this operator-run script:

    1. Generates a strong random webhook secret.
    2. Stores it in SSM at ``/sdlc-agents/github-webhook-secret`` as a
       SecureString — written with the OPERATOR's credentials.
    3. Registers a repo- or org-level webhook via the GitHub REST API,
       pointing config.url at the fleet's API Gateway ``/github/webhook``
       endpoint (discovered from the foundation CloudFormation stack outputs),
       with content_type=json, the shared secret, and the requested events.
    4. Prints the created hook id.

Note on IAM hygiene (cf. scripts/bootstrap_asana_webhook.py threat T-9):
    The Asana bootstrap has to attach a TEMPORARY ssm:PutParameter grant to the
    Lambda's role because the Asana *Lambda* is the party that writes the
    handshake secret to SSM. Here the secret is written by THIS operator script
    using the operator's own credentials, and the github-webhook Lambda only
    ever needs ssm:GetParameter in steady state. There is therefore no Lambda
    role to temporarily elevate, so the temporary-inline-policy dance is not
    needed. The Lambda never holds ssm:PutParameter at all.

Usage:

    # Repo-level webhook
    python scripts/bootstrap_github_webhook.py \\
        --region us-west-2 \\
        --stage dev \\
        --repo my-org/my-repo

    # Org-level webhook
    python scripts/bootstrap_github_webhook.py \\
        --region us-west-2 \\
        --stage dev \\
        --org my-org

Requires:
    - Operator AWS credentials with ssm:PutParameter on
      ``/sdlc-agents/github-webhook-secret``.
    - The infra/foundation stack deployed (provides the API Gateway endpoint).
    - A GitHub token with admin:repo_hook (repo) or admin:org_hook (org) scope,
      read from SSM ``/sdlc-agents/github-mcp-token`` or the GH_TOKEN env var.
"""

import argparse
import os
import secrets
import sys

import boto3
import requests
from botocore.exceptions import ClientError

GITHUB_API = "https://api.github.com"
GITHUB_WEBHOOK_SECRET_PARAM = "/sdlc-agents/github-webhook-secret"  # nosec B105 — SSM parameter name, not a credential
GITHUB_TOKEN_PARAM = "/sdlc-agents/github-mcp-token"  # nosec B105 — SSM parameter name, not a credential
DEFAULT_EVENTS = "issues,projects_v2_item,issue_comment"


def get_api_gateway_url(region: str, stage: str) -> str | None:
    """Find the webhook API Gateway base URL from CloudFormation outputs.

    Mirrors scripts/bootstrap_slack_app.py: read the foundation stack outputs
    and pull the WebhookApi-derived endpoint. We prefer the explicit
    GitHubWebhookEndpoint output (full /github/webhook URL); if an older stack
    only exposes a generic WebhookApi output we fall back to that.
    """
    cf = boto3.client("cloudformation", region_name=region)
    stack_name = f"sdlc-agents-foundation-{stage}"
    try:
        outputs = cf.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
    except ClientError as exc:
        print(f"  Could not read stack {stack_name}: {exc}", file=sys.stderr)
        return None

    # Preferred: the dedicated GitHub endpoint output.
    for output in outputs:
        if output.get("OutputKey") == "GitHubWebhookEndpoint":
            return output["OutputValue"]
    # Fallback: any WebhookApi-ish output, to which we append the path.
    for output in outputs:
        if "WebhookApi" in output.get("OutputKey", ""):
            base = output["OutputValue"].rstrip("/")
            return f"{base}/github/webhook"
    return None


def get_github_token() -> str:
    """Read the GitHub token from GH_TOKEN env var, else from SSM."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    ssm = boto3.client("ssm")
    try:
        return ssm.get_parameter(Name=GITHUB_TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"]
    except ClientError as exc:
        print(
            f"  No GH_TOKEN env var and could not read {GITHUB_TOKEN_PARAM} from SSM: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def store_secret(region: str, value: str) -> None:
    """Store the webhook secret in SSM Parameter Store as a SecureString."""
    ssm = boto3.client("ssm", region_name=region)
    try:
        ssm.put_parameter(
            Name=GITHUB_WEBHOOK_SECRET_PARAM,
            Value=value,
            Type="SecureString",
            Overwrite=True,
            Description="SDLC Agent Fleet GitHub webhook signing secret",
        )
        print(f"  Stored secret: {GITHUB_WEBHOOK_SECRET_PARAM}")
    except ClientError as exc:
        print(f"  ERROR storing {GITHUB_WEBHOOK_SECRET_PARAM}: {exc}", file=sys.stderr)
        sys.exit(1)


def register_webhook(token: str, hooks_url: str, target_url: str, secret: str, events: list[str]) -> dict:
    """Create a webhook via the GitHub REST API. Returns the created hook JSON."""
    resp = requests.post(
        hooks_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "name": "web",
            "active": True,
            "events": events,
            "config": {
                "url": target_url,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  GitHub API error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", required=True, help="AWS region of the fleet")
    parser.add_argument("--stage", default="dev", help="Deployment stage (dev/staging/prod)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="owner/repo for a repository-level webhook")
    target.add_argument("--org", help="organization login for an org-level webhook")
    parser.add_argument(
        "--events",
        default=DEFAULT_EVENTS,
        help=f"Comma-separated GitHub events (default: {DEFAULT_EVENTS})",
    )
    args = parser.parse_args()

    events = [e.strip() for e in args.events.split(",") if e.strip()]

    print("=" * 60)
    print("  SDLC Agent Fleet — GitHub Webhook Bootstrap")
    print("=" * 60)
    print()

    # --- Discover the API Gateway endpoint ---
    target_url = get_api_gateway_url(args.region, args.stage)
    if not target_url:
        print(
            "Could not find the GitHub webhook endpoint from CloudFormation. "
            "Deploy the infra/foundation stack first.",
            file=sys.stderr,
        )
        return 1
    print(f"Webhook target URL : {target_url}")

    if args.repo:
        hooks_url = f"{GITHUB_API}/repos/{args.repo}/hooks"
        print(f"Registering on repo: {args.repo}")
    else:
        hooks_url = f"{GITHUB_API}/orgs/{args.org}/hooks"
        print(f"Registering on org : {args.org}")
    print(f"Events             : {', '.join(events)}")
    print()

    # --- Generate + store the shared secret ---
    secret = secrets.token_hex(32)
    store_secret(args.region, secret)

    # --- Register the webhook with GitHub ---
    token = get_github_token()
    try:
        hook = register_webhook(token, hooks_url, target_url, secret, events)
    except requests.RequestException as exc:
        print(f"  Webhook registration failed: {exc}", file=sys.stderr)
        return 2

    hook_id = hook.get("id", "<unknown>")
    print()
    print(f"  Created webhook id: {hook_id}")
    print()
    print("=" * 60)
    print("  Bootstrap complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Confirm the hook shows a green check in the repo/org settings")
    print("     (GitHub sends a `ping` on creation; the Lambda returns 200).")
    print("  2. Set the bot-login env vars (WORKITEMS_GH_BOT_LOGIN, etc.) on the")
    print("     github-webhook Lambda so issue-assignment triggers resolve.")
    print("  3. Add authorized GitHub logins to .dispatch/agents.yaml and run")
    print("     scripts/sync_registry.py.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
