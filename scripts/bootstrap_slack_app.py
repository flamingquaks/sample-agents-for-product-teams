"""Idempotent Slack app setup for the SDLC Agent Fleet.

Generates a Slack app manifest for a single bot covering the entire fleet,
walks the operator through creating the app in the Slack console, then
stores the bot token and signing secret in SSM Parameter Store.

Usage:

    python scripts/bootstrap_slack_app.py \\
        --stage dev \\
        --region us-west-2

The script:
1. Reads .dispatch/agents.yaml to determine which agents advertise Slack
   triggers (only those get slash commands in the manifest).
2. Prints a YAML app manifest to stdout.
3. Instructs the operator to create the app at https://api.slack.com/apps.
4. Prompts for the bot token (xoxb-...) and signing secret, then writes
   both to SSM as SecureString at the canonical paths:
     /sdlc-agents/slack-bot-token
     /sdlc-agents/slack-signing-secret
5. Looks up the SlackEventsEndpoint and SlackCommandsEndpoint outputs from
   the foundation CloudFormation stack so the operator can paste them back
   into the app config.

Requires operator AWS credentials with:
- ssm:PutParameter on both Slack parameter paths
- cloudformation:DescribeStacks on the foundation stack

No IAM role manipulation is needed — there is no handshake protocol for Slack
(unlike Asana). The signing secret is a static value the operator copies from
the Slack app's Basic Information page.
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

SLACK_BOT_TOKEN_PARAM = "/sdlc-agents/slack-bot-token"
SLACK_SIGNING_SECRET_PARAM = "/sdlc-agents/slack-signing-secret"

AGENTS_YAML = Path(__file__).resolve().parents[1] / ".dispatch" / "agents.yaml"


def load_registry() -> dict:
    with open(AGENTS_YAML) as f:
        return yaml.safe_load(f)


def agents_with_slack_triggers(registry: dict) -> list[str]:
    """Return agent names that advertise any Slack trigger in the registry."""
    result = []
    for name, config in registry.get("agents", {}).items():
        triggers = config.get("triggers", {})
        if triggers.get("slack"):
            result.append(name)
    return sorted(result)


def build_manifest(slack_agents: list[str], events_url_placeholder: str, commands_url_placeholder: str) -> dict:
    """Build a Slack app manifest dict.

    Slash commands are only emitted for agents that advertise Slack triggers.
    The request URLs are placeholders — the operator pastes the real URLs
    from CloudFormation outputs after the app is created.
    """
    slash_commands = [
        {
            "command": f"/{agent}",
            "description": f"Invoke the {agent} SDLC agent",
            "usage_hint": "[instruction]",
            "should_escape": False,
            "url": commands_url_placeholder,
        }
        for agent in slack_agents
    ]

    return {
        "display_information": {
            "name": "SDLC Agents",
            "description": "Autonomous agents for the software development lifecycle",
            "background_color": "#2c3e50",
        },
        "features": {
            "bot_user": {
                "display_name": "SDLC Agents",
                "always_online": True,
            },
            "slash_commands": slash_commands,
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "chat:write",
                    "commands",
                ],
            },
        },
        "settings": {
            "event_subscriptions": {
                "request_url": events_url_placeholder,
                "bot_events": ["app_mention"],
            },
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def get_stack_outputs(cfn_client, stack_name: str) -> dict[str, str]:
    try:
        stacks = cfn_client.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as exc:
        print(f"Could not describe stack {stack_name}: {exc}", file=sys.stderr)
        return {}
    outputs = stacks[0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def write_ssm_param(ssm_client, name: str, value: str, description: str) -> None:
    ssm_client.put_parameter(
        Name=name,
        Value=value,
        Type="SecureString",
        Description=description,
        Overwrite=True,
    )
    print(f"  Stored {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", default="dev", help="Deployment stage (default: dev)")
    parser.add_argument("--region", default="us-west-2", help="AWS region of the fleet")
    parser.add_argument(
        "--stack-name",
        default=None,
        help="Override CloudFormation stack name (default: sdlc-agents-foundation-${stage})",
    )
    args = parser.parse_args()

    stack_name = args.stack_name or f"sdlc-agents-foundation-{args.stage}"
    session = boto3.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    ssm = session.client("ssm")

    # --- 1. Load registry and determine which agents get slash commands -------
    try:
        registry = load_registry()
    except FileNotFoundError:
        print(f"Registry not found at {AGENTS_YAML}", file=sys.stderr)
        return 1

    slack_agents = agents_with_slack_triggers(registry)
    if not slack_agents:
        print("No agents in .dispatch/agents.yaml advertise Slack triggers. Nothing to do.")
        return 0

    print(f"Agents with Slack triggers: {', '.join(slack_agents)}")

    # --- 2. Look up endpoint URLs from the foundation stack -------------------
    print(f"\nLooking up endpoint URLs from CloudFormation stack: {stack_name}")
    outputs = get_stack_outputs(cfn, stack_name)

    events_url = outputs.get("SlackEventsEndpoint", "<paste SlackEventsEndpoint here>")
    commands_url = outputs.get("SlackCommandsEndpoint", "<paste SlackCommandsEndpoint here>")

    if "<paste" in events_url or "<paste" in commands_url:
        print(
            "\nStack outputs not found. The manifest will use placeholder URLs.\n"
            "Deploy the foundation stack first, or paste the real URLs into\n"
            "the app config after creation."
        )

    # --- 3. Print the app manifest -------------------------------------------
    manifest = build_manifest(
        slack_agents,
        events_url_placeholder=events_url,
        commands_url_placeholder=commands_url,
    )
    manifest_yaml = yaml.dump(manifest, sort_keys=False, default_flow_style=False)

    print("\n" + "=" * 70)
    print("SLACK APP MANIFEST")
    print("=" * 70)
    print(manifest_yaml)
    print("=" * 70)

    # --- 4. Walk the operator through app creation ---------------------------
    print(
        "\nNext steps:\n"
        "  1. Go to https://api.slack.com/apps and click 'Create New App'.\n"
        "  2. Choose 'From an app manifest'.\n"
        "  3. Select your Slack workspace.\n"
        "  4. Paste the YAML manifest above (switch to the YAML tab).\n"
        "  5. Review and click 'Create'.\n"
        "  6. On the app's 'Install App' page, click 'Install to Workspace'.\n"
        "     Copy the 'Bot User OAuth Token' (starts with xoxb-).\n"
        "  7. On 'Basic Information' → 'App Credentials', copy the\n"
        "     'Signing Secret'.\n"
    )

    # --- 5. Prompt for credentials and store in SSM --------------------------
    print("Enter the credentials from the Slack app console.\n")

    try:
        bot_token = getpass.getpass("Bot User OAuth Token (xoxb-...): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if not bot_token.startswith("xoxb-"):
        print("Warning: token does not start with 'xoxb-'. Continuing anyway.", file=sys.stderr)

    try:
        signing_secret = getpass.getpass("Signing Secret: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if not signing_secret:
        print("Signing secret is empty — aborting. The webhook Lambda fails closed on empty secrets.", file=sys.stderr)
        return 1

    print("\nStoring credentials in SSM Parameter Store...")
    try:
        write_ssm_param(ssm, SLACK_BOT_TOKEN_PARAM, bot_token, "SDLC Agents Slack bot token (xoxb-)")
        write_ssm_param(ssm, SLACK_SIGNING_SECRET_PARAM, signing_secret, "SDLC Agents Slack signing secret")
    except ClientError as exc:
        print(f"Failed to write SSM parameters: {exc}", file=sys.stderr)
        return 3

    # --- 6. Print endpoint URLs for the operator to configure ----------------
    print(
        f"\nCredentials stored. Now configure the Slack app:\n"
        f"\n"
        f"  Event Subscriptions → Request URL:\n"
        f"    {events_url}\n"
        f"\n"
        f"  Slash Commands → each command's Request URL:\n"
        f"    {commands_url}\n"
        f"\n"
        f"If the manifest already contained these URLs (stack was deployed\n"
        f"before you ran this script), you're done — no further URL changes.\n"
        f"\n"
        f"If the manifest used placeholder URLs, paste the real URLs above\n"
        f"into the app config, then re-save (Slack re-verifies on save).\n"
        f"\n"
        f"Re-install the app to your workspace if you changed any OAuth\n"
        f"scopes after the initial install."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
