#!/usr/bin/env python3
"""Bootstrap a Slack app for the SDLC Agent Fleet.

Guides the operator through:
1. Creating a Slack app from the manifest (or using an existing one)
2. Installing the app to a workspace
3. Capturing the Bot Token and Signing Secret
4. Storing both in AWS SSM Parameter Store
5. Outputting the Events URL for Slack configuration

Usage:
    python scripts/bootstrap_slack_app.py --region us-west-2 --stage dev

Prerequisites:
    - AWS CLI configured with appropriate credentials
    - The infra/foundation stack deployed (provides the API Gateway endpoint)
    - Slack workspace admin access
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def get_slack_endpoints(region: str, stage: str) -> tuple[str | None, str | None]:
    """Return (events_url, slash_url) from the foundation stack outputs.

    Reads the SlackEventsEndpoint / SlackSlashEndpoint outputs directly — these
    are full https URLs. Do NOT match on "WebhookApi": the only output whose key
    contains that substring is WebhookApiId, whose value is the bare REST API id
    (e.g. "abc123"), not a URL — using it produced an invalid "abc123/slack/events".
    """
    cf = boto3.client("cloudformation", region_name=region)
    stack_name = f"sdlc-agents-foundation-{stage}"

    events_url = slash_url = None
    try:
        response = cf.describe_stacks(StackName=stack_name)
        outputs = {o["OutputKey"]: o["OutputValue"]
                   for o in response["Stacks"][0].get("Outputs", [])}
        events_url = outputs.get("SlackEventsEndpoint")
        slash_url = outputs.get("SlackSlashEndpoint")
    except ClientError:
        pass
    return events_url, slash_url


def store_secret(region: str, name: str, value: str):
    """Store a secret in SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=region)
    try:
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="SecureString",
            Overwrite=True,
            Description="SDLC Agent Fleet Slack integration",
        )
        print(f"  Stored: {name}")
    except ClientError as e:
        print(f"  ERROR storing {name}: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Slack app for SDLC Agent Fleet")
    parser.add_argument("--region", required=True, help="AWS region (e.g., us-west-2)")
    parser.add_argument("--stage", default="dev", help="Deployment stage (dev/staging/prod)")
    args = parser.parse_args()

    print("=" * 60)
    print("  SDLC Agent Fleet — Slack App Bootstrap")
    print("=" * 60)
    print()

    # Read the Slack endpoint URLs straight from the foundation stack outputs.
    events_url, slash_url = get_slack_endpoints(args.region, args.stage)
    if events_url and slash_url:
        print(f"Found Slack Events URL: {events_url}")
        print(f"Found Slack Slash URL:  {slash_url}")
    else:
        print("Could not read SlackEventsEndpoint/SlackSlashEndpoint from CloudFormation.")
        print("Deploy the foundation stack first (sdlc-agents-foundation-<stage>).")
        events_url = events_url or "<deploy infra first>"
        slash_url = slash_url or "<deploy infra first>"

    print()
    print("Step 1: Create your Slack app")
    print("-" * 40)
    print()
    print("Option A — From manifest (recommended):")
    print("  1. Go to https://api.slack.com/apps")
    print("  2. Click 'Create New App' → 'From a manifest'")
    print("  3. Select your workspace")
    print("  4. Paste the contents of infra/slack-app-manifest.yaml")
    print(f"  5. Replace <EVENTS_URL> with: {events_url}")
    print(f"  6. Replace <SLASH_URL> with:  {slash_url}")
    print("  7. Click 'Create'")
    print()
    print("Option B — Use an existing app:")
    print("  Make sure it has all required scopes and event subscriptions")
    print("  (see infra/slack-app-manifest.yaml for the full list)")
    print()

    input("Press Enter when your app is created...")
    print()

    print("Step 2: Install the app to your workspace")
    print("-" * 40)
    print()
    print("  1. In your app settings, go to 'OAuth & Permissions'")
    print("  2. Click 'Install to Workspace'")
    print("  3. Authorize the requested permissions")
    print()

    input("Press Enter when the app is installed...")
    print()

    print("Step 3: Capture credentials")
    print("-" * 40)
    print()

    bot_token = input("  Bot User OAuth Token (xoxb-...): ").strip()
    if not bot_token.startswith("xoxb-"):
        print("  WARNING: Token should start with 'xoxb-'. Proceeding anyway.")

    print()
    print("  Find the Signing Secret in your app settings under")
    print("  'Basic Information' → 'App Credentials' → 'Signing Secret'")
    signing_secret = input("  Signing Secret: ").strip()

    if not bot_token or not signing_secret:
        print("\n  ERROR: Both token and signing secret are required.")
        sys.exit(1)

    print()
    print("Step 4: Storing credentials in SSM")
    print("-" * 40)
    print()

    store_secret(args.region, "/sdlc-agents/slack-bot-token", bot_token)
    store_secret(args.region, "/sdlc-agents/slack-signing-secret", signing_secret)

    print()
    print("Step 5: Configure Event Subscriptions")
    print("-" * 40)
    print()
    print("  In your Slack app settings, go to 'Event Subscriptions':")
    print(f"  Request URL: {events_url}")
    print()
    print("  Subscribe to bot events:")
    print("    - app_home_opened")
    print("    - app_mention")
    print("    - assistant_thread_started")
    print("    - message.im")
    print()
    print("  In 'Slash Commands', verify all commands point to:")
    print(f"  {slash_url}")
    print()

    print("=" * 60)
    print("  Bootstrap complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print(f"  1. Deploy the Slack events Lambda: cd infra/foundation && sam deploy --region {args.region}")
    print("  2. Verify the Event Subscriptions URL in Slack (it should show 'Verified')")
    print("  3. Add authorized Slack user IDs to .dispatch/agents.yaml")
    print("  4. Run scripts/sync_registry.py to push the updated registry")
    print("  5. Test: @mention the bot in a channel or use /workitems")
    print()


if __name__ == "__main__":
    main()
